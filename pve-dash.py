#!/usr/bin/env python3
"""Proxmox Dashboard - a live Proxmox VE dashboard agent.

Single file, standard library only. Runs as root on every node, serves index.html
plus a JSON API, samples hardware from the kernel, and (on one elected node per
cluster) evaluates alert rules and sends them through Pushover.
"""
import base64
import copy
import glob
import hashlib
import hmac
import json
import math
import logging
import os
import random
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import error as urlerr
from urllib import parse as urlparse
from urllib import request as urlreq

VERSION = "1.0.0"


def _env(name, default, cast=str):
    try:
        return cast(os.environ.get(name, default))
    except (TypeError, ValueError):
        return cast(default)


PORT = _env("PVE_DASH_PORT", 8765, int)
BIND = _env("PVE_DASH_BIND", "0.0.0.0")
INTERVAL = max(0.5, _env("PVE_DASH_INTERVAL", 3, float))
HISTORY = max(10, _env("PVE_DASH_HISTORY", 200, int))
DEMO = os.environ.get("PVE_DASH_DEMO") == "1"
PRIV = os.environ.get("PVE_DASH_PRIV") or ("./demo-data" if DEMO else "/etc/pve/priv")
HERE = os.path.dirname(os.path.abspath(__file__))
GUEST_HIST = 60
log = logging.getLogger("pve-dash")


# --------------------------------------------------------------------------- utils
def rd(path, default=None):
    try:
        with open(path, "r", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return default


def rd_num(path, default=None):
    v = rd(path)
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def run(cmd, timeout=20):
    """Run a command, returning (returncode, stdout). 127 when the binary is missing."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout
    except FileNotFoundError:
        return 127, ""
    except subprocess.TimeoutExpired:
        return 124, ""


def pvesh(path, timeout=20):
    rc, out = run(["pvesh", "get", path, "--output-format", "json"], timeout)
    if rc != 0:
        raise RuntimeError("pvesh get %s failed (rc=%s)" % (path, rc))
    return json.loads(out)


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def atomic_write(path, data, mode=0o600):
    """Write to a temp file in the same directory, then rename over the target."""
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=".pvetmp-", dir=d)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data if isinstance(data, bytes) else data.encode())
        try:
            os.chmod(tmp, mode)
        except OSError:
            pass
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_json(path, default=None):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


# ------------------------------------------------------------- hardware sampling
CPU_CHIPS = {"coretemp", "k10temp", "zenpower"}
GPU_CHIPS = {"amdgpu", "nouveau", "radeon", "i915", "xe"}
NIC_CHIPS = {"i40e", "ixgbe", "igc", "igb", "r8169", "atlantic", "bnxt_en", "mlx5", "mlx4", "tg3", "iwlwifi", "cxgb4", "sfc"}


def classify_chip(chip):
    c = (chip or "").lower()
    if c in CPU_CHIPS:
        return "cpu"
    if c.startswith("nvme"):
        return "nvme"
    if c == "drivetemp":
        return "drive"
    if c in GPU_CHIPS:
        return "gpu"
    if c in NIC_CHIPS or c.startswith("mlx"):
        return "nic"
    return "board"


def primary_cpu_temp(temps):
    cpu = [t for t in temps if t["kind"] == "cpu"]
    if not cpu:
        return None
    for prefix in ("Package id", "Tctl", "Tdie"):
        for t in cpu:
            if t["label"].startswith(prefix):
                return t["value"]
    return max(t["value"] for t in cpu)


def read_hwmon(root="/sys/class/hwmon"):
    temps, fans = [], []
    for hw in sorted(glob.glob(os.path.join(root, "hwmon*"))):
        chip = rd(os.path.join(hw, "name"), "unknown")
        kind = classify_chip(chip)
        dev = model = None
        devlink = os.path.join(hw, "device")
        if kind == "nvme":
            model = rd(os.path.join(devlink, "model"))
            dev = os.path.basename(os.path.realpath(devlink))
        elif kind == "drive":
            model = rd(os.path.join(devlink, "model"))
            try:
                dev = sorted(os.listdir(os.path.join(devlink, "block")))[0]
            except (OSError, IndexError):
                dev = None
        for f in sorted(glob.glob(os.path.join(hw, "temp*_input"))):
            m = re.search(r"temp(\d+)_input$", f)
            v = rd_num(f)
            if not m or v is None:
                continue
            idx = m.group(1)
            hi = rd_num(os.path.join(hw, "temp%s_max" % idx))
            cr = rd_num(os.path.join(hw, "temp%s_crit" % idx))
            temps.append({
                "chip": chip, "label": rd(os.path.join(hw, "temp%s_label" % idx)) or chip, "kind": kind,
                "value": round(v / 1000.0, 1), "high": None if hi is None else round(hi / 1000.0, 1),
                "crit": None if cr is None else round(cr / 1000.0, 1), "model": model, "dev": dev,
            })
        for f in sorted(glob.glob(os.path.join(hw, "fan*_input"))):
            m = re.search(r"fan(\d+)_input$", f)
            v = rd_num(f)
            if not m or v is None:
                continue
            fans.append({"chip": chip, "label": rd(os.path.join(hw, "fan%s_label" % m.group(1))) or "Fan " + m.group(1), "rpm": int(v)})
    return temps, fans


class LocalSampler:
    """Reads live hardware numbers from /proc and /sys, diffing between samples."""
    DISK_RE = re.compile(r"^(sd[a-z]+|nvme\d+n\d+|vd[a-z]+)$")

    def __init__(self):
        self.prev = None
        self.slow = {"smart": {"available": None, "disks": []}, "zfs": {"available": None, "pools": [], "ok": True, "text": ""}}
        self._slow_lock = threading.Lock()
        self._started = False

    # --- raw readers
    @staticmethod
    def read_stat():
        out = {}
        for line in (rd("/proc/stat") or "").splitlines():
            if not line.startswith("cpu"):
                continue
            p = line.split()
            v = [int(x) for x in p[1:9]]
            out[p[0]] = (v[3] + v[4], sum(v), v[4])  # idle+iowait, total, iowait
        return out

    @staticmethod
    def read_net():
        out = {}
        for line in (rd("/proc/net/dev") or "").splitlines()[2:]:
            name, _, rest = line.partition(":")
            name = name.strip()
            if not name or not os.path.exists("/sys/class/net/%s/device" % name):
                continue
            f = rest.split()
            out[name] = (int(f[0]), int(f[8]))
        return out

    def read_disks(self):
        out = {}
        for line in (rd("/proc/diskstats") or "").splitlines():
            f = line.split()
            if len(f) < 14 or not self.DISK_RE.match(f[2]):
                continue
            out[f[2]] = (int(f[5]), int(f[9]), int(f[12]))  # sectors read, written, io_ticks(ms)
        return out

    @staticmethod
    def meminfo():
        m = {}
        for line in (rd("/proc/meminfo") or "").splitlines():
            k, _, v = line.partition(":")
            try:
                m[k] = int(v.split()[0]) * 1024
            except (ValueError, IndexError):
                pass
        return m

    @staticmethod
    def arc_size():
        txt = rd("/proc/spl/kstat/zfs/arcstats")
        if not txt:
            return None
        for line in txt.splitlines():
            p = line.split()
            if len(p) == 3 and p[0] == "size":
                return int(p[2])
        return None

    @staticmethod
    def clock():
        mhz = [float(m) for m in re.findall(r"^cpu MHz\s*:\s*([\d.]+)", rd("/proc/cpuinfo") or "", re.M)]
        mx = rd_num("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq")
        return (round(sum(mhz) / len(mhz)) if mhz else None), (round(mx / 1000) if mx else None)

    # --- sampling
    def prime(self):
        self.prev = (time.time(), self.read_stat(), self.read_net(), self.read_disks())

    def sample(self):
        if self.prev is None:
            self.prime()
            time.sleep(0.3)
        t = time.time()
        st, net, dk = self.read_stat(), self.read_net(), self.read_disks()
        pt, pst, pnet, pdk = self.prev
        dt = max(0.05, t - pt)
        cores, cpu_pct, iowait = [], 0.0, 0.0
        for name in sorted((k for k in st if k != "cpu"), key=lambda s: int(s[3:])):
            if name in pst:
                di, dtot = st[name][0] - pst[name][0], st[name][1] - pst[name][1]
                cores.append(round(clamp(1 - di / dtot, 0, 1), 3) if dtot > 0 else 0.0)
        if "cpu" in st and "cpu" in pst:
            dtot = st["cpu"][1] - pst["cpu"][1]
            if dtot > 0:
                cpu_pct = clamp(1 - (st["cpu"][0] - pst["cpu"][0]) / dtot, 0, 1)
                iowait = clamp((st["cpu"][2] - pst["cpu"][2]) / dtot, 0, 1)
        nics = []
        for name, (rx, tx) in sorted(net.items()):
            sp = rd_num("/sys/class/net/%s/speed" % name)
            prx, ptx = pnet.get(name, (rx, tx))
            nics.append({"name": name, "up": rd("/sys/class/net/%s/operstate" % name) == "up",
                         "speed": int(sp) if sp and sp > 0 else 0,
                         "rx": max(0, rx - prx) / dt, "tx": max(0, tx - ptx) / dt})
        disks = []
        for name, (r, w, io) in sorted(dk.items()):
            pr, pw, pio = pdk.get(name, (r, w, io))
            rot = rd("/sys/block/%s/queue/rotational" % name)
            size = rd_num("/sys/block/%s/size" % name, 0) * 512
            disks.append({"name": name, "model": rd("/sys/block/%s/device/model" % name), "ssd": rot == "0", "size": size,
                          "r": max(0, r - pr) * 512 / dt, "w": max(0, w - pw) * 512 / dt,
                          "util": round(clamp((io - pio) / (dt * 1000.0), 0, 1), 3)})
        self.prev = (t, st, net, dk)
        temps, fans = read_hwmon()
        mem = self.meminfo()
        mhz, mhz_max = self.clock()
        try:
            load = [float(x) for x in rd("/proc/loadavg").split()[:3]]
        except (AttributeError, ValueError):
            load = None
        up = rd("/proc/uptime")
        with self._slow_lock:
            slow = copy.deepcopy(self.slow)
        return {
            "agent_version": VERSION, "ts": t, "node": None, "maxcpu": len(cores), "cpu_pct": cpu_pct, "cores": cores,
            "iowait": iowait, "mem_total": mem.get("MemTotal"),
            "mem_used": (mem["MemTotal"] - mem["MemAvailable"]) if "MemTotal" in mem and "MemAvailable" in mem else None,
            "arc": self.arc_size(), "swap_total": mem.get("SwapTotal", 0),
            "swap_used": mem.get("SwapTotal", 0) - mem.get("SwapFree", 0), "load": load,
            "uptime": int(float(up.split()[0])) if up else None, "mhz": mhz, "mhz_max": mhz_max,
            "nics": nics, "disks": disks, "temps": temps, "fans": fans, "cpu_temp": primary_cpu_temp(temps),
            "smart": slow["smart"], "zfs": slow["zfs"],
        }

    # --- slow checks (run in their own thread so sampling never blocks)
    def start_slow(self):
        if self._started:
            return
        self._started = True
        threading.Thread(target=self._slow_loop, daemon=True, name="slow-checks").start()

    def _slow_loop(self):
        next_zfs, next_smart = 0, time.time() + 20
        while True:
            now = time.time()
            try:
                if now >= next_zfs:
                    self._set("zfs", self.scan_zfs())
                    next_zfs = now + 300
                if now >= next_smart:
                    self._set("smart", self.scan_smart())
                    next_smart = now + 1800
            except Exception:
                log.exception("slow check failed")
            time.sleep(5)

    def _set(self, k, v):
        with self._slow_lock:
            self.slow[k] = v

    @staticmethod
    def scan_zfs():
        rc, out = run(["zpool", "list", "-H", "-o", "name,health"], 20)
        if rc == 127:
            return {"available": False, "pools": [], "ok": True, "text": ""}
        pools = [{"name": p[0], "health": p[1]} for p in (l.split() for l in out.splitlines()) if len(p) >= 2]
        _, st = run(["zpool", "status", "-x"], 20)
        ok = all(p["health"] == "ONLINE" for p in pools) and ("all pools are healthy" in st.lower() or not pools)
        return {"available": True, "pools": pools, "ok": ok, "text": st.strip()[:600]}

    def scan_smart(self):
        disks = []
        names = sorted(self.read_disks())
        for name in names:
            rc, out = run(["smartctl", "-H", "-A", "-j", "/dev/" + name], 60)
            if rc == 127:
                return {"available": False, "disks": []}
            try:
                d = json.loads(out)
            except ValueError:
                continue
            attrs = {a.get("name"): (a.get("raw") or {}).get("value") for a in (d.get("ata_smart_attributes") or {}).get("table", [])}
            nv = d.get("nvme_smart_health_information_log") or {}
            disks.append({"dev": name, "passed": (d.get("smart_status") or {}).get("passed"),
                          "realloc": attrs.get("Reallocated_Sector_Ct"), "pending": attrs.get("Current_Pending_Sector"),
                          "media_errors": nv.get("media_errors")})
        return {"available": True, "disks": disks}


# ------------------------------------------------------------------ rate tracking
class RateTracker:
    """pmxcfs refreshes guest counters only every ~10s. Compute a rate only when the
    value changes (delta / time since last change), hold it for `hold` seconds, and
    treat a counter reset as 0."""

    def __init__(self, hold=25.0):
        self.hold = hold
        self.s = {}

    def update(self, key, val, ts):
        if val is None:
            return 0.0
        st = self.s.get(key)
        if st is None:
            self.s[key] = {"v": val, "t": ts, "rate": 0.0, "rt": ts}
            return 0.0
        if val != st["v"]:
            if val < st["v"]:
                st["rate"] = 0.0
            else:
                st["rate"] = (val - st["v"]) / max(0.001, ts - st["t"])
            st["v"], st["t"], st["rt"] = val, ts, ts
        elif ts - st["rt"] > self.hold:
            st["rate"] = 0.0
        return st["rate"]


# ----------------------------------------------------------------- 24h aggregates
class Agg:
    """Rolling 24h aggregate in 1-minute buckets (for the daily digest)."""

    def __init__(self):
        self.buckets = deque(maxlen=1440)
        self.cur = None
        self.lock = threading.Lock()

    def add(self, ts, nodes, guests):
        m = int(ts // 60)
        with self.lock:
            if self.cur is None or self.cur["m"] != m:
                if self.cur is not None:
                    self.buckets.append(self.cur)
                self.cur = {"m": m, "temp": {}, "g": {}}
            c = self.cur
            for n in nodes:
                if n.get("online") and n.get("cpu_temp") is not None:
                    c["temp"][n["name"]] = max(c["temp"].get(n["name"], -1e9), n["cpu_temp"])
            for g in guests:
                if g["status"] == "running":
                    e = c["g"].setdefault(g["vmid"], [g["name"], g["node"], 0.0, 0])
                    e[2] += g["cores_used"]
                    e[3] += 1

    def summary(self, node, now=None):
        now = now or time.time()
        peak, guests = None, {}
        with self.lock:
            allb = list(self.buckets) + ([self.cur] if self.cur else [])
        for b in allb:
            if b["m"] * 60 < now - 86400:
                continue
            t = b["temp"].get(node)
            if t is not None:
                peak = t if peak is None else max(peak, t)
            for vmid, (name, nd, s, c) in b["g"].items():
                if nd == node:
                    e = guests.setdefault(vmid, [name, 0.0, 0])
                    e[1] += s
                    e[2] += c
        top = sorted(((v[0], vmid, v[1] / v[2]) for vmid, v in guests.items() if v[2]), key=lambda x: -x[2])[:3]
        return {"peak_temp": peak, "top_guests": top}


# ------------------------------------------------------------------- collector
def fetch_agent(ip, port=None, timeout=1.5):
    try:
        with urlreq.urlopen("http://%s:%d/api/local" % (ip, port or PORT), timeout=timeout) as r:
            d = json.loads(r.read())
        return d if isinstance(d, dict) and "cores" in d else None
    except Exception:
        return None


def parse_node_status(d):
    """Reduce /nodes/{n}/status to what the dashboard shows."""
    ci = d.get("cpuinfo") or {}
    rf = d.get("rootfs") or {}
    pv = d.get("pveversion") or ""
    kv = (d.get("kversion") or "").split()
    return {"model": ci.get("model"), "sockets": ci.get("sockets"),
            "pve": pv.split("/")[1] if pv.count("/") >= 1 else pv or None,
            "kernel": kv[1] if len(kv) > 1 else None,
            "rootfs_used": rf.get("used"), "rootfs_total": rf.get("total")}


AGENT_FIELDS = ("maxcpu", "cpu_pct", "cores", "iowait", "mem_total", "mem_used", "arc", "swap_total", "swap_used", "load",
                "uptime", "mhz", "mhz_max", "nics", "disks", "temps", "fans", "cpu_temp", "agent_version", "smart", "zfs")


class Collector:
    def __init__(self, sampler=None, demo=None, interval=None, history=None):
        self.sampler = sampler
        self.demo = demo
        self.interval = interval or INTERVAL
        self.history = history or HISTORY
        self.lock = threading.Lock()
        self.state = None
        self.local_sample = None
        self.ok = False
        self.error = "starting"
        self.node_hist = {}
        self.guest_hist = {}
        self.rates = RateTracker()
        self.nstatus_cache = {}
        self.agg = Agg()
        self.on_state = None  # callback(state) after each good sample
        self._cache_key = None
        self._cache_bytes = b""

    # --- pure(ish) assembly, shared by real and demo sources
    def build_state(self, res, cstat, samples, nstatus, now):
        citem = next((i for i in cstat if i.get("type") == "cluster"), None)
        cn = {i["name"]: i for i in cstat if i.get("type") == "node"}
        local = next((n for n, i in cn.items() if i.get("local")), None) or socket.gethostname().split(".")[0]
        nodes = []
        for r in sorted((r for r in res if r.get("type") == "node"), key=lambda r: r["node"]):
            name = r["node"]
            online = r.get("status") == "online"
            smp = samples.get(name) if online else None
            ns = nstatus.get(name) or {}
            n = {"name": name, "online": online, "local": name == local, "ip": (cn.get(name) or {}).get("ip"),
                 "maxcpu": r.get("maxcpu") or 0, "cpu_pct": r.get("cpu") or 0.0, "mem_used": r.get("mem"),
                 "mem_total": r.get("maxmem"), "uptime": r.get("uptime"),
                 "model": ns.get("model"), "sockets": ns.get("sockets"), "pve": ns.get("pve"), "kernel": ns.get("kernel"),
                 "rootfs_used": ns.get("rootfs_used", r.get("disk")), "rootfs_total": ns.get("rootfs_total", r.get("maxdisk")),
                 "agent": smp is not None,
                 "temps": [], "fans": [], "cores": [], "nics": [], "disks": [], "cpu_temp": None, "load": None,
                 "agent_version": None, "arc": None, "swap_used": None, "swap_total": None, "mhz": None, "mhz_max": None,
                 "iowait": None}
            if smp:
                for k in AGENT_FIELDS:
                    if k in smp and smp[k] is not None:
                        n[k] = smp[k]
            h = self.node_hist.setdefault(name, deque(maxlen=self.history))
            if online:
                mf = n["mem_used"] / n["mem_total"] if n["mem_total"] and n["mem_used"] is not None else None
                h.append([int(now), round(n["cpu_pct"], 4), None if mf is None else round(mf, 4), n["cpu_temp"]])
            n["history"] = list(h)
            nodes.append(n)
        online_names = {n["name"] for n in nodes if n["online"]}
        guests = []
        for r in res:
            if r.get("type") not in ("qemu", "lxc") or r.get("template"):
                continue
            vmid, running = r["vmid"], r.get("status") == "running"
            cpu = (r.get("cpu") or 0.0) if running else 0.0
            maxcpu = r.get("maxcpu") or 1
            rate = lambda k: self.rates.update((vmid, k), r.get(k), now) if running else 0.0
            g = {"vmid": vmid, "name": r.get("name") or str(vmid), "type": r["type"], "node": r.get("node"),
                 "status": r.get("status"), "tags": r.get("tags") or None, "cpu": cpu, "maxcpu": maxcpu,
                 "cores_used": cpu * maxcpu, "mem": (r.get("mem") or 0) if running else 0, "maxmem": r.get("maxmem") or 0,
                 "disk": r.get("disk") or 0, "maxdisk": r.get("maxdisk") or 0,
                 "net_in": rate("netin"), "net_out": rate("netout"), "disk_r": rate("diskread"), "disk_w": rate("diskwrite"),
                 "uptime": (r.get("uptime") or 0) if running else 0}
            gh = self.guest_hist.setdefault(vmid, deque(maxlen=GUEST_HIST))
            gh.append(round(g["cores_used"], 3))
            g["hist"] = list(gh)
            guests.append(g)
        guests.sort(key=lambda g: g["vmid"])
        live = {g["vmid"] for g in guests}
        for k in [k for k in self.guest_hist if k not in live]:
            del self.guest_hist[k]
        storage, shared_seen = [], set()
        for r in sorted((r for r in res if r.get("type") == "storage"), key=lambda r: (r.get("storage", ""), r.get("node", ""))):
            shared = bool(r.get("shared"))
            if shared:
                if r["storage"] in shared_seen:
                    continue
                shared_seen.add(r["storage"])
            storage.append({"id": r["storage"], "node": None if shared else r.get("node"), "type": r.get("plugintype"),
                            "used": r.get("disk") or 0, "total": r.get("maxdisk") or 0, "shared": shared,
                            "status": r.get("status") or "available"})
        cluster = {"name": (citem or {}).get("name") or local, "quorate": bool((citem or {}).get("quorate", 1)),
                   "nodes_total": len(nodes), "nodes_online": len(online_names)}
        self.agg.add(now, nodes, guests)
        return {"ok": True, "error": None, "ts": now, "interval": self.interval, "local": local, "version": VERSION,
                "demo": bool(self.demo), "cluster": cluster, "nodes": nodes, "guests": guests, "storage": storage,
                "sender": None}

    # --- real sources
    def fetch_real(self):
        local = self.sampler.sample()
        with ThreadPoolExecutor(8) as ex:
            f1, f2 = ex.submit(pvesh, "/cluster/resources"), ex.submit(pvesh, "/cluster/status")
            try:
                res, cstat = f1.result(), f2.result()
            except Exception:
                with self.lock:
                    local["node"] = socket.gethostname().split(".")[0]
                    self.local_sample = local
                raise
            cn = {i["name"]: i for i in cstat if i.get("type") == "node"}
            lname = next((n for n, i in cn.items() if i.get("local")), None) or socket.gethostname().split(".")[0]
            local["node"] = lname
            with self.lock:
                self.local_sample = local
            online = [r["node"] for r in res if r.get("type") == "node" and r.get("status") == "online"]
            rem = {n: ex.submit(fetch_agent, cn[n]["ip"]) for n in online if n != lname and (cn.get(n) or {}).get("ip")}
            now = time.time()
            stale = [n for n in online if now - self.nstatus_cache.get(n, (0, None))[0] > 30]
            nst = {n: ex.submit(lambda n=n: parse_node_status(pvesh("/nodes/%s/status" % n))) for n in stale}
            samples = {lname: local}
            for n, f in rem.items():
                s = f.result()
                if s:
                    samples[n] = s
            for n, f in nst.items():
                try:
                    self.nstatus_cache[n] = (now, f.result())
                except Exception:
                    self.nstatus_cache.setdefault(n, (now, None))
        return res, cstat, samples, {n: v[1] for n, v in self.nstatus_cache.items()}

    # --- loop
    def step(self):
        now = time.time()
        res, cstat, samples, nst = self.demo.fetch(now) if self.demo else self.fetch_real()
        st = self.build_state(res, cstat, samples, nst, now)
        with self.lock:
            self.state, self.ok, self.error = st, True, None
        if self.demo:
            with self.lock:
                self.local_sample = samples.get(st["local"])
        if self.on_state:
            try:
                self.on_state(st)
            except Exception:
                log.exception("on_state failed")

    def prefill(self):
        """Demo only: back-fill history so charts aren't empty on first load."""
        n = min(self.history, 120)
        for i in range(n, 0, -1):
            t = time.time() - i * self.interval
            res, cstat, samples, nst = self.demo.fetch(t)
            self.build_state(res, cstat, samples, nst, t)

    def run(self):
        if self.sampler:
            self.sampler.prime()
            self.sampler.start_slow()
        if self.demo:
            self.prefill()
        while True:
            t0 = time.time()
            try:
                self.step()
            except Exception as e:
                log.warning("sample failed: %s", e)
                with self.lock:
                    self.ok, self.error = False, str(e)[:200]
            time.sleep(max(0.05, self.interval - (time.time() - t0)))

    def get_state_bytes(self):
        with self.lock:
            st, ok, err = self.state, self.ok, self.error
        if st is None:
            return json.dumps({"ok": False, "error": err or "no sample yet", "nodes": None}).encode()
        key = (st["ts"], ok, err)
        if key != self._cache_key:
            out = dict(st)
            out["ok"], out["error"] = ok, err
            self._cache_bytes, self._cache_key = json.dumps(out).encode(), key
        return self._cache_bytes

    def get_local_bytes(self):
        with self.lock:
            s = self.local_sample
        return json.dumps(s if s else {"error": "no sample yet"}).encode()


# ------------------------------------------------------------------------ demo
class DemoSource:
    """Generates a plausible fake 3-node cluster, shaped like the real pvesh/agent data."""
    NODES = [
        ("pve-alpha", "10.0.0.11", 16, 64, "AMD Ryzen 9 5950X 16-Core Processor", "k10temp", 0.55, 62),
        ("pve-bravo", "10.0.0.12", 8, 32, "Intel(R) Core(TM) i7-10700 CPU @ 2.90GHz", "coretemp", 0.25, 48),
        ("pve-charlie", "10.0.0.13", 4, 16, "Intel(R) Core(TM) i5-6500T CPU @ 2.50GHz", "coretemp", 0.15, 42),
    ]
    GUESTS = [  # vmid, name, type, node, vcpu, mem GiB, mean util, mean mem frac
        (100, "opnsense", "qemu", "pve-alpha", 4, 4, 0.08, .55), (101, "jellyfin", "lxc", "pve-alpha", 6, 8, 0.4, .55),
        (102, "win11-gaming", "qemu", "pve-alpha", 8, 24, 0.35, .85), (103, "pterodactyl", "qemu", "pve-alpha", 6, 12, 0.45, .7),
        (104, "home-assistant", "qemu", "pve-bravo", 2, 4, 0.11, .5), (105, "teamspeak", "lxc", "pve-bravo", 1, 1, 0.05, .3),
        (106, "rustdesk", "lxc", "pve-bravo", 1, 1, 0.03, .25), (107, "arr-stack", "lxc", "pve-bravo", 4, 6, 0.24, .62),
        (108, "tailscale-sr", "lxc", "pve-charlie", 1, .5, 0.01, .4), (109, "pihole", "lxc", "pve-charlie", 1, .5, 0.03, .45),
        (110, "scrypted", "lxc", "pve-charlie", 2, 4, 0.13, .57), (111, "dev-ubuntu", "qemu", "pve-alpha", 4, 8, 0, 0),
    ]
    GiB = 1024 ** 3

    def __init__(self, seed=None):
        self.rnd = random.Random(seed)
        self.pub = {}  # vmid -> (counters published to "pmxcfs", bucket)
        self.cum = {g[0]: [0.0, 0.0, 0.0, 0.0] for g in self.GUESTS}
        self.last_t = None
        self.t0 = time.time()

    @staticmethod
    def wave(t, period, phase=0.0):
        return 0.5 + 0.5 * math.sin(2 * math.pi * (t / period) + phase)

    def fetch(self, now):
        w, G = self.wave, self.GiB
        dt = 3.0 if self.last_t is None else max(0.0, min(30.0, now - self.last_t))
        self.last_t = now
        samples, res, nstatus = {}, [], {}
        cstat = [{"type": "cluster", "name": "homelab", "quorate": 1, "nodes": 3}]
        for i, (name, ip, thr, gib, model, chip, base, tbase) in enumerate(self.NODES):
            cpu = clamp(base * (0.55 + 0.9 * w(now, 90 + i * 17, i)) + self.rnd.uniform(-.03, .03), 0.01, 0.98)
            cores = [round(clamp(cpu * (0.4 + 1.2 * self.rnd.random()), 0, 1), 3) for _ in range(thr)]
            memf = clamp(0.5 + 0.12 * w(now, 400, i * 2) + self.rnd.uniform(-.005, .005), 0.1, 0.95)
            temp = round(tbase + 20 * cpu + self.rnd.uniform(-.8, .8), 1)
            lbl = "Tctl" if chip == "k10temp" else "Package id 0"
            temps = [{"chip": chip, "label": lbl, "kind": "cpu", "value": temp, "high": None if chip == "k10temp" else 80.0,
                      "crit": 95.0 if chip == "k10temp" else 100.0, "model": None, "dev": None}]
            for c in range(min(thr, 4) if chip == "coretemp" else 2):
                temps.append({"chip": chip, "label": ("Core %d" % c) if chip == "coretemp" else "Tccd%d" % (c + 1), "kind": "cpu",
                              "value": round(temp - 2 - c * .6 + self.rnd.uniform(-.5, .5), 1), "high": 80.0 if chip == "coretemp" else None,
                              "crit": 100.0 if chip == "coretemp" else None, "model": None, "dev": None})
            temps += [{"chip": "nvme", "label": "Composite", "kind": "nvme", "value": round(44 + 8 * cpu + self.rnd.uniform(-.4, .4), 1),
                       "high": 82.8, "crit": 84.8, "model": "Samsung SSD 980 PRO 2TB", "dev": "nvme0"},
                      {"chip": "drivetemp", "label": "drivetemp", "kind": "drive", "value": 36.0, "high": 60.0, "crit": 65.0,
                       "model": "WDC WD80EFZZ-68BTXN0", "dev": "sda"},
                      {"chip": "acpitz", "label": "acpitz", "kind": "board", "value": 27.8, "high": None, "crit": 105.0, "model": None, "dev": None}]
            samples[name] = {
                "agent_version": VERSION, "ts": now, "node": name, "maxcpu": thr, "cpu_pct": cpu, "cores": cores, "iowait": 0.01,
                "mem_total": gib * G, "mem_used": int(gib * G * memf), "arc": int(gib * G * 0.12), "swap_total": 8 * G,
                "swap_used": int(0.6 * G), "load": [round(cpu * thr * f, 2) for f in (1.1, 1.0, .9)], "uptime": 864069 + int(now - self.t0),
                "mhz": int(3300 + 1200 * cpu), "mhz_max": 4900,
                "nics": [{"name": "enp1s0", "up": True, "speed": 2500, "rx": 1.8e7 * (.6 + .8 * self.rnd.random()), "tx": 1.0e7 * (.6 + .8 * self.rnd.random())},
                         {"name": "enp2s0", "up": True, "speed": 1000, "rx": 2.7e6 * (.6 + .8 * self.rnd.random()), "tx": 1.6e6 * (.6 + .8 * self.rnd.random())}],
                "disks": [{"name": "nvme0n1", "model": "Samsung SSD 980 PRO 2TB", "ssd": True, "size": 2e12, "r": 6e6 * (.3 + 1.5 * self.rnd.random()),
                           "w": 5e5 * (.3 + 3 * self.rnd.random()), "util": round(.2 + .3 * self.rnd.random(), 3)},
                          {"name": "sda", "model": "WDC WD80EFZZ-68BTXN0", "ssd": False, "size": 8e12, "r": 1.3e7 * self.rnd.random(),
                           "w": 8e5 * self.rnd.random(), "util": round(.1 + .2 * self.rnd.random(), 3)}],
                "temps": temps, "fans": [{"chip": "nct6798", "label": "CPU fan", "rpm": int(800 + 900 * cpu)}, {"chip": "nct6798", "label": "Chassis 1", "rpm": 820}],
                "cpu_temp": temp, "smart": {"available": True, "disks": []}, "zfs": {"available": True, "pools": [{"name": "rpool", "health": "ONLINE"}], "ok": True, "text": ""},
            }
            res.append({"type": "node", "node": name, "status": "online", "cpu": cpu, "maxcpu": thr, "mem": int(gib * G * memf),
                        "maxmem": gib * G, "uptime": samples[name]["uptime"], "disk": 33285996544, "maxdisk": 100931731456})
            cstat.append({"type": "node", "name": name, "ip": ip, "online": 1, "local": 1 if i == 0 else 0})
            nstatus[name] = {"model": model, "sockets": 1, "pve": "9.0.11", "kernel": "6.14.11-4-pve",
                             "rootfs_used": 33285996544, "rootfs_total": 100931731456}
        bucket = int(now // 10)
        for j, (vmid, name, typ, node, vcpu, mem, util, mf) in enumerate(self.GUESTS):
            running = util > 0
            cpu = clamp(util * (0.5 + w(now, 120 + j * 13, j)) + self.rnd.uniform(-.02, .02), 0.0, 1.0) if running else 0
            netr = [util * 2e8 + 2e5, util * 3e7 + 4e4, util * 3e7 + 2e4, util * 1e7 + 1e4] if running else [0, 0, 0, 0]
            for k in range(4):
                self.cum[vmid][k] += netr[k] * (.6 + .8 * self.rnd.random()) * dt
            if vmid not in self.pub or self.pub[vmid][1] != bucket:
                self.pub[vmid] = (list(self.cum[vmid]), bucket)  # pmxcfs-style: counters refresh every ~10s
            c = self.pub[vmid][0]
            res.append({"type": typ, "vmid": vmid, "name": name, "node": node, "status": "running" if running else "stopped",
                        "cpu": cpu, "maxcpu": vcpu, "mem": int(mem * G * clamp(mf + self.rnd.uniform(-.01, .01), 0, 1)) if running else 0,
                        "maxmem": int(mem * G), "disk": 8589934592, "maxdisk": 34359738368, "uptime": 300069 + int(now - self.t0) if running else 0,
                        "netin": int(c[0]), "netout": int(c[1]), "diskread": int(c[2]), "diskwrite": int(c[3])})
        for node, used, total in (("pve-alpha", 1100e9, 1800e9), ("pve-bravo", 300e9, 900e9), ("pve-charlie", 300e9, 900e9)):
            res.append({"type": "storage", "storage": "local-zfs", "node": node, "plugintype": "zfspool", "disk": int(used), "maxdisk": int(total),
                        "shared": 0, "status": "available"})
            res.append({"type": "storage", "storage": "nas-backups", "node": node, "plugintype": "nfs", "disk": int(5.6e12), "maxdisk": int(8e12),
                        "shared": 1, "status": "available"})
        return res, cstat, samples, nstatus


# -------------------------------------------------------------------- settings
DEFAULT_NAME = "Proxmox Dashboard"
KEY_RE = re.compile(r"^[A-Za-z0-9]{30}$")
HM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
SOUNDS = ["", "pushover", "bike", "bugle", "cashregister", "classical", "cosmic", "falling", "gamelan", "incoming",
          "intermission", "magic", "mechanical", "pianobar", "siren", "spacealarm", "tugboat", "alien", "climb",
          "persistent", "echo", "updown", "vibrate", "none"]


def P(k, d, lo, hi, step=1):
    return (k, d, lo, hi, step)


def T(k, d):
    return (k, d, None, None, None)


# (id, enabled by default, default priority, params) - mirrors GROUPS in the UI
RULE_DEFS = [
    ("cpu_temp", True, 0, [P("warn", 80, 40, 110), P("crit", 90, 40, 115)]),
    ("disk_temp", True, 0, [P("warn", 55, 30, 90), P("crit", 65, 30, 95)]),
    ("fan", True, 1, [P("for", 30, 5, 600, 5)]),
    ("nic", True, 0, []),
    ("smart", True, 1, []),
    ("storage", True, 0, [P("warn", 85, 50, 99), P("crit", 95, 50, 100)]),
    ("rootfs", True, 0, [P("warn", 90, 50, 99)]),
    ("memory", False, 0, [P("pct", 92, 50, 99), P("for", 5, 1, 60)]),
    ("swap", False, -1, [P("pct", 50, 5, 100, 5)]),
    ("cpu_sustained", False, 0, [P("pct", 90, 50, 100), P("for", 10, 1, 120)]),
    ("guest_hog", True, 0, [P("pct", 75, 20, 100, 5), P("for", 10, 1, 120)]),
    ("iowait", False, 0, [P("pct", 20, 5, 90), P("for", 5, 1, 60)]),
    ("node_down", True, 1, [P("for", 60, 15, 900, 15)]),
    ("quorum", True, 1, []),
    ("guest_down", True, 0, []),
    ("guest_restart", False, 0, []),
    ("agent", True, -1, [P("for", 3, 1, 60)]),
    ("backup", True, 1, []),
    ("zfs", True, 1, []),
    ("updates", False, -1, []),
    ("cert", False, 0, [P("days", 14, 1, 60)]),
    ("digest", False, -1, [T("at", "08:00")]),
]
RULE_IDS = [r[0] for r in RULE_DEFS]


def default_config():
    rules = {}
    for rid, on, pr, params in RULE_DEFS:
        rules[rid] = {"enabled": on, "priority": pr, "to": None}
        for k, d, *_ in params:
            rules[rid][k] = d
    return {"branding": {"name": DEFAULT_NAME, "show_name": True}, "destinations": [], "rules": rules,
            "delivery": {"quiet": {"enabled": False, "from": "22:00", "to": "07:00", "allow_high": True}, "repeat": 60, "resolved": True}}


def _num(v, default):
    try:
        f = float(v)
        return f if f == f and abs(f) != float("inf") else default
    except (TypeError, ValueError):
        return default


def sanitize_config(body, current=None, strict=True):
    """Validate and normalise a settings document. Raises ValueError in strict mode."""
    body = body if isinstance(body, dict) else {}
    cur_d = {d["id"]: d for d in (current or {}).get("destinations", []) if isinstance(d, dict) and "id" in d}
    out = default_config()
    # branding
    b = body.get("branding") if isinstance(body.get("branding"), dict) else {}
    name = str(b.get("name", "") or "").strip()[:40]
    out["branding"] = {"name": name or DEFAULT_NAME, "show_name": bool(b.get("show_name", True))}
    ln = ((current or {}).get("branding") or {}).get("logo_name")
    if ln:
        out["branding"]["logo_name"] = ln
    # destinations
    dests, ids = [], set()
    for d in (body.get("destinations") or [])[:50]:
        if not isinstance(d, dict):
            continue
        did = str(d.get("id") or "")
        if not ID_RE.match(did) or did in ids:
            did = "d" + secrets.token_hex(4)
        ids.add(did)
        old = cur_d.get(did, {})
        token = str(d["token"]).strip() if d.get("token") else old.get("token", "")
        user = str(d["user"]).strip() if d.get("user") else old.get("user", "")
        if strict and not KEY_RE.match(token):
            raise ValueError("Destination '%s': application token must be 30 letters and numbers" % str(d.get("label", ""))[:40])
        if strict and not KEY_RE.match(user):
            raise ValueError("Destination '%s': user or group key must be 30 letters and numbers" % str(d.get("label", ""))[:40])
        sound = str(d.get("sound") or "")
        color = str(d.get("color") or "")
        dests.append({"id": did, "label": (str(d.get("label") or "").strip()[:40] or "Destination"),
                      "color": color if COLOR_RE.match(color) else "#007aff", "enabled": bool(d.get("enabled", True)),
                      "token": token, "user": user, "device": str(d.get("device") or "").strip()[:100],
                      "sound": sound if sound in SOUNDS else ""})
    out["destinations"] = dests
    # rules
    src_rules = body.get("rules") if isinstance(body.get("rules"), dict) else {}
    for rid, on, pr, params in RULE_DEFS:
        s = src_rules.get(rid) if isinstance(src_rules.get(rid), dict) else {}
        r = out["rules"][rid]
        r["enabled"] = bool(s.get("enabled", on))
        try:
            p = int(s.get("priority", pr))
        except (TypeError, ValueError):
            p = pr
        r["priority"] = p if p in (-1, 0, 1, 2) else pr
        to = s.get("to")
        if isinstance(to, list):
            to = [str(x) for x in to if str(x) in ids]
            r["to"] = to or None
        for k, d, lo, hi, step in params:
            v = s.get(k, d)
            if lo is None:  # time of day
                r[k] = v if isinstance(v, str) and HM_RE.match(v) else d
            else:
                f = clamp(round(_num(v, d) / step) * step, lo, hi)
                r[k] = int(f) if float(step).is_integer() else f
        if "warn" in r and "crit" in r and r["crit"] <= r["warn"]:
            hi = [p for p in params if p[0] == "crit"][0][3]
            r["crit"] = min(hi, r["warn"] + 5)
            if r["crit"] <= r["warn"]:
                r["warn"] = r["crit"] - 1
    # delivery
    dv = body.get("delivery") if isinstance(body.get("delivery"), dict) else {}
    q = dv.get("quiet") if isinstance(dv.get("quiet"), dict) else {}
    dq = out["delivery"]["quiet"]
    out["delivery"] = {"quiet": {"enabled": bool(q.get("enabled", dq["enabled"])),
                                 "from": q.get("from") if HM_RE.match(str(q.get("from", ""))) else dq["from"],
                                 "to": q.get("to") if HM_RE.match(str(q.get("to", ""))) else dq["to"],
                                 "allow_high": bool(q.get("allow_high", dq["allow_high"]))},
                       "repeat": int(clamp(_num(dv.get("repeat"), 60), 0, 10080)), "resolved": bool(dv.get("resolved", True))}
    return out


def mask_config(cfg):
    out = copy.deepcopy(cfg)
    for d in out["destinations"]:
        d["token_hint"] = "•••" + d.pop("token", "")[-4:]
        d["user_hint"] = "•••" + d.pop("user", "")[-4:]
    return out


class Settings:
    """Cluster-wide config in /etc/pve/priv (pmxcfs), written atomically."""

    def __init__(self, path):
        self.path = path
        self._sig = None
        self._cfg = None
        self.lock = threading.Lock()

    def get(self):
        with self.lock:
            try:
                st = os.stat(self.path)
                sig = (st.st_mtime_ns, st.st_size)
            except OSError:
                sig = None
            if self._cfg is None or sig != self._sig:
                raw = read_json(self.path, {}) if sig else {}
                self._cfg, self._sig = sanitize_config(raw, raw, strict=False), sig
            return copy.deepcopy(self._cfg)

    def put(self, body):
        with self.lock:
            cur = read_json(self.path, {}) if os.path.exists(self.path) else {}
        cfg = sanitize_config(body, sanitize_config(cur, cur, strict=False), strict=True)
        self.write(cfg)
        return cfg

    def write(self, cfg):
        with self.lock:
            atomic_write(self.path, json.dumps(cfg, indent=1))
            self._cfg = None

    def set_logo_name(self, name):
        cfg = self.get()
        if name:
            cfg["branding"]["logo_name"] = name
        else:
            cfg["branding"].pop("logo_name", None)
        self.write(cfg)


class JsonState:
    def __init__(self, path):
        self.path = path

    def load(self):
        return read_json(self.path, None)

    def save(self, d):
        atomic_write(self.path, json.dumps(d))


# ------------------------------------------------------------------------ logo
LOGO_TYPES = {"image/png": "png", "image/svg+xml": "svg", "image/webp": "webp", "image/jpeg": "jpg"}
LOGO_MAX = 1024 * 1024


def sniff_logo(data):
    """Detect the real image type from magic bytes. Returns a content type or None."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    head = data[:2048].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if (head.startswith(b"<svg") or head.startswith(b"<?xml") or head.startswith(b"<!--") or head.startswith(b"<!doctype svg")) and b"<svg" in data[:4096].lower():
        return "image/svg+xml"
    return None


SVG_BAD = [re.compile(p, re.I) for p in (
    r"<\s*script", r"<\s*foreignobject", r"<\s*iframe", r"<\s*embed", r"<\s*object", r"<!entity", r"\bon\w+\s*=",
    r"javascript:", r"@import", r"(?:xlink:)?href\s*=\s*(?![\"']?\s*#)", r"url\(\s*(?![\"']?\s*(?:#|data:))")]


def svg_is_safe(data):
    try:
        txt = data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return not any(p.search(txt) for p in SVG_BAD)


class LogoStore:
    def __init__(self, directory):
        self.dir = directory

    def _files(self):
        return glob.glob(os.path.join(self.dir, "pve-dash-logo.*"))

    def find(self):
        for f in self._files():
            ext = f.rsplit(".", 1)[-1]
            ctype = next((c for c, e in LOGO_TYPES.items() if e == ext), None)
            if ctype:
                return f, ctype
        return None

    def url(self):
        f = self.find()
        if not f:
            return None
        with open(f[0], "rb") as fh:
            return "logo?v=" + hashlib.sha1(fh.read()).hexdigest()[:10]

    def put(self, data, ctype):
        if ctype not in LOGO_TYPES:
            raise ValueError("Logo must be PNG, SVG, WebP or JPEG")
        if len(data) > LOGO_MAX:
            raise ValueError("Logo is over 1 MB")
        real = sniff_logo(data)
        if real != ctype:
            raise ValueError("File contents don't look like %s" % ctype)
        if real == "image/svg+xml" and not svg_is_safe(data):
            raise ValueError("SVG contains scripts, event handlers or external references")
        for f in self._files():
            try:
                os.unlink(f)
            except OSError:
                pass
        atomic_write(os.path.join(self.dir, "pve-dash-logo." + LOGO_TYPES[ctype]), data, 0o644)
        return self.url()

    def delete(self):
        for f in self._files():
            try:
                os.unlink(f)
            except OSError:
                pass


# ------------------------------------------------------------------------ auth
class Auth:
    COOKIE = "pvesession"
    DAYS = 30

    def __init__(self, path):
        self.path = path

    def data(self):
        return read_json(self.path, None)

    def configured(self):
        d = self.data()
        return bool(d and d.get("hash") and d.get("secret"))

    def set_password(self, password):
        salt = secrets.token_bytes(16)
        it = 200000
        h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, it)
        old = self.data() or {}
        atomic_write(self.path, json.dumps({"salt": base64.b64encode(salt).decode(), "iterations": it, "hash": h.hex(),
                                            "secret": old.get("secret") or secrets.token_hex(32)}))

    def verify(self, password):
        d = self.data()
        if not d:
            return False
        h = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(d["salt"]), int(d["iterations"]))
        return hmac.compare_digest(h.hex(), d["hash"])

    def _sig(self, payload, secret):
        return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()

    def issue(self, now=None):
        d = self.data()
        exp = int((now or time.time()) + self.DAYS * 86400)
        payload = "%d.%s" % (exp, secrets.token_hex(8))
        return "%s.%s" % (payload, self._sig(payload, d["secret"]))

    def valid(self, token, now=None):
        d = self.data()
        if not d or not token:
            return False
        try:
            exp, nonce, sig = token.split(".")
            return hmac.compare_digest(sig, self._sig("%s.%s" % (exp, nonce), d["secret"])) and int(exp) > (now or time.time())
        except ValueError:
            return False

    def cookie(self, token):
        return "%s=%s; Path=/; HttpOnly; SameSite=Strict; Max-Age=%d" % (self.COOKIE, token, self.DAYS * 86400)

    def clear_cookie(self):
        return "%s=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0" % self.COOKIE


# -------------------------------------------------------------------- pushover
class PushoverTransport:
    URL = "https://api.pushover.net/1/messages.json"

    def __init__(self, sleep=time.sleep, opener=urlreq.urlopen):
        self.sleep, self.opener = sleep, opener

    def __call__(self, payload):
        data = urlparse.urlencode(payload).encode()
        delay, last = 1, "request failed"
        for attempt in range(3):
            try:
                req = urlreq.Request(self.URL, data=data, method="POST")
                with self.opener(req, timeout=10) as r:
                    body = json.loads(r.read() or b"{}")
                if body.get("status") == 1:
                    return True, None
                return False, "; ".join(body.get("errors") or ["Pushover rejected the message"])
            except urlerr.HTTPError as e:
                try:
                    errs = json.loads(e.read()).get("errors")
                except Exception:
                    errs = None
                if 400 <= e.code < 500:  # permanent: bad token/user, don't retry
                    return False, "; ".join(errs) if errs else "HTTP %d" % e.code
                last = "HTTP %d" % e.code
            except Exception as e:
                last = type(e).__name__
            if attempt < 2:
                self.sleep(delay)
                delay *= 3
        return False, last


def log_transport(payload):
    """Demo mode: show what would have been sent. Never logs token or user."""
    log.info("[demo] pushover p%s: %s | %s", payload.get("priority"), payload.get("title"), (payload.get("message") or "").replace("\n", " / ")[:300])
    return True, None


# ---------------------------------------------------------------------- engine
DOWN_TASKS = {"qmstop", "qmshutdown", "vzstop", "vzshutdown", "qmigrate", "vzmigrate", "vzdump"}
RESTART_TASKS = DOWN_TASKS | {"qmreboot", "vzreboot", "qmreset"}


class SysAPI:
    """Things the engine asks the cluster for, behind one interface so tests can fake it."""

    def tasks(self):
        try:
            return pvesh("/cluster/tasks")
        except Exception:
            return []

    def task_log(self, node, upid):
        try:
            return [x.get("t", "") for x in pvesh("/nodes/%s/tasks/%s/log?limit=500" % (node, urlparse.quote(upid, safe="")))]
        except Exception:
            return []

    def apt_updates(self, node):
        try:
            return pvesh("/nodes/%s/apt/update" % node)
        except Exception:
            return None

    def cert_days(self, node):
        out = []
        for fn in ("pve-ssl.pem", "pveproxy-ssl.pem"):
            path = "/etc/pve/nodes/%s/%s" % (node, fn)
            if not os.path.exists(path):
                continue
            rc, txt = run(["openssl", "x509", "-enddate", "-noout", "-in", path], 10)
            m = re.search(r"notAfter=(.+)", txt)
            if rc == 0 and m:
                try:
                    end = datetime.strptime(m.group(1).strip(), "%b %d %H:%M:%S %Y %Z")
                    out.append((fn, int((end - datetime.utcnow()).total_seconds() // 86400)))
                except ValueError:
                    pass
        return out


class NullSys:
    def tasks(self):
        return []

    def task_log(self, node, upid):
        return []

    def apt_updates(self, node):
        return ["pkg"] * 7

    def cert_days(self, node):
        return [("pve-ssl.pem", 300)]


def pick_sender(snap):
    """The online node with the lowest name that is running the agent."""
    names = sorted(n["name"] for n in snap.get("nodes", []) if n.get("online") and n.get("agent"))
    return names[0] if names else None


def hm_min(s):
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def in_window(minute, start, end):
    """Is `minute` (of day) in [start, end)? Handles windows that cross midnight."""
    if start == end:
        return False
    if start < end:
        return start <= minute < end
    return minute >= start or minute < end


def dur_text(sec):
    sec = int(max(0, sec))
    if sec < 30:
        return "just started"
    if sec < 3600:
        return "%d min" % max(1, round(sec / 60))
    h, m = divmod(sec // 60, 60)
    return "%d h %d min" % (h, m) if m else "%d h" % h


class Engine:
    """Alert rule engine. Runs on the elected sender node only."""
    ORDER = [r for r in RULE_IDS if r not in ("guest_down", "guest_restart", "backup", "updates", "digest")]

    def __init__(self, settings, state_store, transport, sysapi=None, clock=time.time, url=None):
        self.settings, self.sst, self.transport = settings, state_store, transport
        self.sys = sysapi or SysAPI()
        self.clock = clock
        self.url = url
        self.st = self._blank()
        self.loaded = False
        self.fan_seen, self.nic_seen_up, self.nic_max = set(), set(), {}
        self.prev_guest = None
        self.timers = {}
        self._tasks_cache = (0, [])
        self.dirty = False

    @staticmethod
    def _blank():
        return {"alerts": {}, "held": [], "fired": [], "seen_upids": [], "backup_init": False, "smart_base": {},
                "last_updates": {}, "cert": {}, "cert_checked": 0, "digest_day": ""}

    # ----- entry point
    def tick(self, snap, agg=None):
        self.now = self.clock()
        self.snap, self.agg = snap, agg
        if pick_sender(snap) != snap.get("local"):
            self.loaded, self.prev_guest = False, None  # someone else sends; forget local memory
            return False
        self.cfg = self.settings.get()
        if not self.loaded:
            loaded = self.sst.load() or {}
            self.st = self._blank()
            self.st.update(loaded)
            self.loaded = True
        self.dirty = False
        self.active = set()
        nodes = snap["nodes"]
        self.offline = {n["name"] for n in nodes if not n["online"] or not n.get("agent")}
        R = self.cfg["rules"]
        self._learn()
        for rid in self.ORDER:
            if R[rid]["enabled"]:
                getattr(self, "chk_" + rid)(R[rid])
        self._guest_events()
        for rid in ("backup", "updates", "digest"):
            if R[rid]["enabled"]:
                try:
                    getattr(self, "chk_" + rid)(R[rid])
                except Exception:
                    log.exception("rule %s failed", rid)
        self._sweep()
        self._flush_held()
        self.st["fired"] = [f for f in self.st["fired"] if f[0] > self.now - 86400]
        if self.dirty:
            self.sst.save(self.st)
        return True

    # ----- helpers
    def _key(self, rule, node, subject):
        return "%s:%s:%s" % (rule, node, subject)

    def _cluster(self):
        return self.snap["cluster"]["name"]

    def _live(self):
        return [n for n in self.snap["nodes"] if n["online"] and n.get("agent")]

    def _online(self):
        return [n for n in self.snap["nodes"] if n["online"]]

    def _level(self, key, val, warn, crit, delta):
        """Threshold level 0/1/2 with hysteresis once an alert is firing."""
        a = self.st["alerts"].get(key)
        cur = a["level"] if a and a.get("fired") else 0
        if crit is not None and val >= (crit - delta if cur >= 2 else crit):
            return 2
        if val >= (warn - delta if cur >= 1 else warn):
            return 1
        return 0

    def _prio(self, rule, level=1):
        base = int(self.cfg["rules"][rule]["priority"])
        return min(2, base + 1) if level >= 2 else base

    def _top_guests(self, node, n=3):
        gs = sorted((g for g in self.snap["guests"] if g["node"] == node and g["status"] == "running"), key=lambda g: -g["cores_used"])[:n]
        if not gs:
            return None
        return "Top guests by CPU: " + ", ".join("%s (#%s) %.2f cores" % (g["name"], g["vmid"], g["cores_used"]) for g in gs)

    def _thresh(self, rule, node, subject, val, warn, crit, unit, head, reading, for_s=0, guests=False, label=True, ok=None):
        key = self._key(rule, node, subject)
        lvl = self._level(key, val, warn, crit, 3 if unit == "°C" else 2)
        if not lvl:
            return
        if crit is not None:
            th = "Threshold: warning at %g%s, critical at %g%s" % (warn, unit, crit, unit)
        else:
            th = "Threshold: %g%s" % (warn, unit)
        sfx = (" (critical)" if lvl == 2 else " (warning)") if label and crit is not None else ""
        self._obs(key, rule, node, subject, lvl, head.format(val) + sfx, [reading, th], for_s, ok, guests=guests)

    # ----- alert state machine
    def _obs(self, key, rule, node, subject, level, headline, lines, for_s=0, ok_text=None, always=False, guests=False):
        self.active.add(key)
        now = self.now
        alerts = self.st["alerts"]
        a = alerts.get(key)
        if a is None:
            a = alerts[key] = {"rule": rule, "node": node, "subject": subject, "since": now, "fired": False, "level": 0, "sent": 0}
            self.dirty = True
        a.update({"headline": headline, "ok": ok_text or "Resolved: " + headline, "always": always})
        if not a["fired"]:
            if now - a["since"] >= for_s:
                a.update({"fired": True, "level": level, "sent": now})
                self._send_alert(a, lines, level, guests, "fire")
                self.dirty = True
            return
        if level > a["level"]:
            a.update({"level": level, "sent": now})
            self._send_alert(a, lines, level, guests, "escalate")
            self.dirty = True
        elif level < a["level"]:
            a["level"] = level  # critical -> warning: no re-send
            self.dirty = True
        else:
            rep = self.cfg["delivery"]["repeat"]
            if rep > 0 and now - a["sent"] >= rep * 60:
                a["sent"] = now
                self._send_alert(a, lines, level, guests, "repeat")
                self.dirty = True

    def _send_alert(self, a, lines, level, guests, kind):
        body = list(lines) + ["Active for: " + dur_text(self.now - a["since"])]
        if guests and a["node"] != "cluster":
            tg = self._top_guests(a["node"])
            if tg:
                body.append(tg)
        title = "[%s] %s: %s" % (self._cluster(), a["node"], a["headline"])
        self._dispatch(a["rule"], title, "\n".join(body), self._prio(a["rule"], level))
        self.st["fired"].append([self.now, a["node"], a["headline"]])

    def _sweep(self):
        R = self.cfg["rules"]
        for key, a in list(self.st["alerts"].items()):
            if key in self.active:
                continue
            if a["rule"] not in R or not R[a["rule"]]["enabled"]:
                del self.st["alerts"][key]
                self.dirty = True
                continue
            if a["node"] in self.offline and a["rule"] not in ("node_down", "agent"):
                continue  # can't evaluate right now; don't claim it resolved
            if a["fired"] and (self.cfg["delivery"]["resolved"] or a.get("always")):
                title = "[%s] %s: %s" % (self._cluster(), a["node"], a["ok"])
                self._dispatch(a["rule"], title, "Was active for %s." % dur_text(self.now - a["since"]), -1)
            del self.st["alerts"][key]
            self.dirty = True

    def _event(self, rule, node, headline, lines, guests=False, priority=None):
        body = list(lines)
        if guests:
            tg = self._top_guests(node)
            if tg:
                body.append(tg)
        title = "[%s] %s: %s" % (self._cluster(), node, headline)
        self._dispatch(rule, title, "\n".join(body), self._prio(rule) if priority is None else priority)
        self.st["fired"].append([self.now, node, headline])
        self.dirty = True

    # ----- delivery
    def in_quiet(self, now):
        q = self.cfg["delivery"]["quiet"]
        if not q["enabled"]:
            return False
        d = datetime.fromtimestamp(now)
        return in_window(d.hour * 60 + d.minute, hm_min(q["from"]), hm_min(q["to"]))

    def _dest_ids(self, rule):
        to = self.cfg["rules"][rule].get("to")
        return [d["id"] for d in self.cfg["destinations"] if d["enabled"] and (to is None or d["id"] in to)]

    def _dispatch(self, rule, title, body, priority):
        ids = self._dest_ids(rule)
        if not ids:
            return
        q = self.cfg["delivery"]["quiet"]
        if self.in_quiet(self.now) and not (priority >= 1 and q["allow_high"]):
            self.st["held"].append({"ts": self.now, "title": title, "body": body, "priority": priority, "dests": ids})
            self.dirty = True
            return
        self._deliver(ids, title, body, priority)

    def _deliver(self, ids, title, body, priority, ts=None):
        for d in self.cfg["destinations"]:
            if d["id"] not in ids or not d["enabled"]:
                continue
            ok, err = self.transport(build_payload(d, title, body, priority, self.url or self._dash_url(), ts or self.now))
            if not ok:
                log.warning("pushover send to '%s' failed: %s", d["label"], err)

    def _dash_url(self):
        ip = next((n.get("ip") for n in self.snap["nodes"] if n.get("local")), None)
        return "http://%s:%d/" % (ip or self.snap.get("local"), PORT)

    def _flush_held(self):
        held = self.st["held"]
        if not held or self.in_quiet(self.now):
            return
        for d in self.cfg["destinations"]:
            items = [h for h in held if d["id"] in h["dests"]]
            if not items or not d["enabled"]:
                continue
            lines = []
            for h in items:
                first = h["body"].split("\n")[0]
                lines.append("%s %s - %s" % (datetime.fromtimestamp(h["ts"]).strftime("%H:%M"), h["title"], first))
            title = "[%s] %d alert%s held during quiet hours" % (self._cluster(), len(items), "" if len(items) == 1 else "s")
            self._deliver([d["id"]], title, "\n".join(lines)[:1000], min(0, max(h["priority"] for h in items)))
        self.st["held"] = []
        self.dirty = True

    # ----- learning (per-run memory)
    def _learn(self):
        for n in self._live():
            for f in n.get("fans") or []:
                if f["rpm"] > 0:
                    self.fan_seen.add("%s|%s|%s" % (n["name"], f["chip"], f["label"]))
            for x in n.get("nics") or []:
                k = (n["name"], x["name"])
                if x["up"]:
                    self.nic_seen_up.add(k)
                    self.nic_max[k] = max(self.nic_max.get(k, 0), x.get("speed") or 0)

    # ----- rules: hardware
    def chk_cpu_temp(self, p):
        for n in self._live():
            t = n.get("cpu_temp")
            if t is not None:
                self._thresh("cpu_temp", n["name"], "cpu", t, p["warn"], p["crit"], "°C", "CPU {:.0f}°C", "CPU temperature %.1f°C" % t,
                             guests=True, ok="CPU temperature back to normal")

    def chk_disk_temp(self, p):
        for n in self._live():
            for t in n.get("temps") or []:
                if t["kind"] not in ("nvme", "drive"):
                    continue
                sub = t.get("dev") or t.get("model") or t["chip"]
                if t["label"] not in ("Composite", t["chip"]):
                    sub += ":" + t["label"]
                self._thresh("disk_temp", n["name"], sub, t["value"], p["warn"], p["crit"], "°C", sub + " {:.0f}°C",
                             "%s %.1f°C%s" % (sub, t["value"], " (%s)" % t["model"] if t.get("model") else ""),
                             ok="%s temperature back to normal" % sub)

    def chk_fan(self, p):
        for n in self._live():
            for f in n.get("fans") or []:
                if "%s|%s|%s" % (n["name"], f["chip"], f["label"]) in self.fan_seen and f["rpm"] == 0:
                    key = self._key("fan", n["name"], f["chip"] + "/" + f["label"])
                    self._obs(key, "fan", n["name"], f["chip"] + "/" + f["label"], 1, "%s stopped" % f["label"],
                              ["%s reads 0 RPM (it was spinning earlier)" % f["label"], "Threshold: 0 RPM for %ds" % p["for"]], p["for"],
                              "%s spinning again" % f["label"])

    def chk_nic(self, p):
        for n in self._live():
            for x in n.get("nics") or []:
                k = (n["name"], x["name"])
                if k not in self.nic_seen_up:
                    continue
                key = self._key("nic", n["name"], x["name"])
                if not x["up"]:
                    self._obs(key, "nic", n["name"], x["name"], 1, "%s link down" % x["name"], ["Interface %s is down" % x["name"]], 0,
                              "%s link restored" % x["name"])
                elif x.get("speed") and x["speed"] < self.nic_max.get(k, 0):
                    self._obs(key, "nic", n["name"], x["name"], 1, "%s link slowed to %d Mb/s" % (x["name"], x["speed"]),
                              ["Link speed is %d Mb/s, usually %d Mb/s" % (x["speed"], self.nic_max[k])], 0, "%s link speed restored" % x["name"])

    def chk_smart(self, p):
        base = self.st["smart_base"]
        for n in self._live():
            sm = n.get("smart") or {}
            if not sm.get("available"):
                continue
            for d in sm.get("disks") or []:
                dev = d["dev"]
                if d.get("passed") is False:
                    self._obs(self._key("smart", n["name"], dev), "smart", n["name"], dev, 1, "%s failed its SMART health check" % dev,
                              ["Drive /dev/%s reports SMART status: FAILED" % dev], 0, "%s SMART check passing again" % dev)
                bk = "%s:%s" % (n["name"], dev)
                cur = {"realloc": d.get("realloc"), "pending": d.get("pending"), "media": d.get("media_errors")}
                old = base.get(bk)
                if old is not None:
                    names = {"realloc": "reallocated sectors", "pending": "pending sectors", "media": "NVMe media errors"}
                    grew = [(names[k], old.get(k), v) for k, v in cur.items() if v is not None and old.get(k) is not None and v > old[k]]
                    if grew:
                        self._event("smart", n["name"], "%s SMART: %s" % (dev, grew[0][0]) + " rising",
                                    ["%s: %s %s -> %s" % (dev, g[0], g[1], g[2]) for g in grew])
                if old != cur:
                    base[bk] = cur
                    self.dirty = True

    # ----- rules: capacity
    def chk_storage(self, p):
        online = {n["name"] for n in self._online()}
        for s in self.snap["storage"]:
            node = s.get("node")
            if not s.get("total") or s.get("status") not in (None, "available") or (node and node not in online):
                continue
            f = s["used"] / s["total"] * 100
            self._thresh("storage", node or "cluster", s["id"], f, p["warn"], p["crit"], "%", s["id"] + " {:.0f}% full",
                         "Storage %s is %.1f%% full (%.0f of %.0f GiB)" % (s["id"], f, s["used"] / 2 ** 30, s["total"] / 2 ** 30),
                         ok="%s usage back to normal" % s["id"])

    def chk_rootfs(self, p):
        for n in self._online():
            if n.get("rootfs_total"):
                f = n["rootfs_used"] / n["rootfs_total"] * 100
                self._thresh("rootfs", n["name"], "/", f, p["warn"], None, "%", "Root disk {:.0f}% full", "Root filesystem is %.1f%% full" % f,
                             label=False, ok="Root disk usage back to normal")

    def chk_memory(self, p):
        for n in self._online():
            if n.get("mem_total") and n.get("mem_used") is not None:
                f = (n["mem_used"] - (n.get("arc") or 0)) / n["mem_total"] * 100
                self._thresh("memory", n["name"], "ram", f, p["pct"], None, "%", "Memory {:.0f}% in use", "Memory %.1f%% in use (ZFS ARC excluded)" % f,
                             for_s=p["for"] * 60, guests=True, label=False, ok="Memory usage back to normal")

    def chk_swap(self, p):
        for n in self._live():
            if n.get("swap_total"):
                f = n["swap_used"] / n["swap_total"] * 100
                self._thresh("swap", n["name"], "swap", f, p["pct"], None, "%", "Swap {:.0f}% in use", "Swap is %.1f%% in use" % f, label=False,
                             ok="Swap usage back to normal")

    # ----- rules: load
    def chk_cpu_sustained(self, p):
        for n in self._online():
            f = (n.get("cpu_pct") or 0) * 100
            self._thresh("cpu_sustained", n["name"], "cpu", f, p["pct"], None, "%", "CPU {:.0f}% sustained", "Node CPU at %.1f%%" % f,
                         for_s=p["for"] * 60, guests=True, label=False, ok="CPU load back to normal")

    def chk_guest_hog(self, p):
        nodes = {n["name"]: n for n in self._online()}
        for g in self.snap["guests"]:
            n = nodes.get(g["node"])
            if g["status"] != "running" or not n or not n.get("maxcpu"):
                continue
            f = g["cores_used"] / n["maxcpu"] * 100
            self._thresh("guest_hog", n["name"], str(g["vmid"]), f, p["pct"], None, "%", "%s (#%s) using {:.0f}%% of host CPU" % (g["name"], g["vmid"]),
                         "%s #%s uses %.2f of %d cores (%.0f%% of host)" % (g["name"], g["vmid"], g["cores_used"], n["maxcpu"], f),
                         for_s=p["for"] * 60, label=False, ok="%s no longer hogging CPU" % g["name"])

    def chk_iowait(self, p):
        for n in self._live():
            if n.get("iowait") is not None:
                f = n["iowait"] * 100
                self._thresh("iowait", n["name"], "iowait", f, p["pct"], None, "%", "IO wait {:.0f}%", "CPU IO wait at %.1f%%" % f,
                             for_s=p["for"] * 60, guests=True, label=False, ok="IO wait back to normal")

    # ----- rules: availability
    def chk_node_down(self, p):
        for n in self.snap["nodes"]:
            if not n["online"]:
                self._obs(self._key("node_down", n["name"], n["name"]), "node_down", n["name"], n["name"], 1, "node offline",
                          ["%s is not responding to the cluster" % n["name"]], p["for"], "node back online", always=True)

    def chk_quorum(self, p):
        if not self.snap["cluster"]["quorate"]:
            self._obs(self._key("quorum", "cluster", "quorum"), "quorum", "cluster", "quorum", 1, "cluster quorum lost",
                      ["The cluster has lost quorum", "Guests can't start or migrate until it returns"], 0, "cluster quorum restored")

    def chk_agent(self, p):
        for n in self.snap["nodes"]:
            if n["online"] and not n.get("agent"):
                self._obs(self._key("agent", n["name"], n["name"]), "agent", n["name"], n["name"], 1, "dashboard agent unreachable",
                          ["%s is online but its dashboard agent doesn't answer" % n["name"], "Temperatures and hardware can't be watched there"],
                          p["for"] * 60, "dashboard agent reachable again")

    def _recent_task(self, vmid, types):
        if self.now - self._tasks_cache[0] > 10:
            try:
                self._tasks_cache = (self.now, self.sys.tasks() or [])
            except Exception:
                self._tasks_cache = (self.now, [])
        for t in self._tasks_cache[1]:
            if str(t.get("id")) == str(vmid) and t.get("type") in types and (t.get("endtime") or t.get("starttime") or self.now) >= self.now - 120:
                return True
        return False

    def _guest_events(self):
        R = self.cfg["rules"]
        cur = {g["vmid"]: (g["status"], g.get("uptime") or 0) for g in self.snap["guests"]}
        prev, self.prev_guest = self.prev_guest, cur
        if prev is None:
            return
        online = {n["name"] for n in self._online()}
        for g in self.snap["guests"]:
            old = prev.get(g["vmid"])
            if not old or g["node"] not in online:
                continue
            if R["guest_down"]["enabled"] and old[0] == "running" and g["status"] == "stopped" and not self._recent_task(g["vmid"], DOWN_TASKS):
                self._event("guest_down", g["node"], "%s (#%s) stopped unexpectedly" % (g["name"], g["vmid"]),
                            ["%s was running and stopped with no stop, shutdown or migration task" % g["name"]])
            elif (R["guest_restart"]["enabled"] and old[0] == "running" and g["status"] == "running" and (g.get("uptime") or 0) + 5 < old[1]
                  and not self._recent_task(g["vmid"], RESTART_TASKS)):
                self._event("guest_restart", g["node"], "%s (#%s) rebooted" % (g["name"], g["vmid"]),
                            ["Uptime reset from %s to %s without Proxmox stopping it (crash or guest-initiated reboot)" % (dur_text(old[1]), dur_text(g.get("uptime") or 0))])

    # ----- rules: Proxmox
    def chk_zfs(self, p):
        for n in self._live():
            z = n.get("zfs") or {}
            if not z.get("available"):
                continue
            bad = [x for x in z.get("pools") or [] if x["health"] != "ONLINE"]
            for x in bad:
                self._obs(self._key("zfs", n["name"], x["name"]), "zfs", n["name"], x["name"], 1, "ZFS pool %s is %s" % (x["name"], x["health"]),
                          ["Pool %s health: %s" % (x["name"], x["health"]), (z.get("text") or "")[:300]], 0, "ZFS pool %s is ONLINE" % x["name"])
            if not bad and not z.get("ok", True):
                self._obs(self._key("zfs", n["name"], "status"), "zfs", n["name"], "status", 1, "ZFS reports a problem",
                          ["zpool status -x: " + (z.get("text") or "not healthy")[:300]], 0, "ZFS healthy again")

    def _due(self, name, every):
        if self.now - self.timers.get(name, 0) >= every:
            self.timers[name] = self.now
            return True
        return False

    def chk_backup(self, p):
        if not self._due("backup", 60):
            return
        tasks = self.sys.tasks() or []
        seen = self.st["seen_upids"]
        first = not self.st["backup_init"]
        for t in tasks:
            if t.get("type") != "vzdump" or not t.get("endtime") or t["upid"] in seen:
                continue
            seen.append(t["upid"])
            self.dirty = True
            if first or t.get("status") == "OK":
                continue
            lines = self.sys.task_log(t["node"], t["upid"]) or []
            vmids = sorted(set(re.findall(r"(?:VM|CT)\s+(\d+)\s+failed", "\n".join(lines))) | ({str(t["id"])} if t.get("id") else set()))
            err = next((l for l in lines if "ERROR" in l), None) or t.get("status", "")
            err = re.sub(r"^.*?ERROR:\s*", "", err)
            self._event("backup", t["node"], "backup failed" + (" (%s)" % ", ".join(vmids) if vmids else ""),
                        ["Backup job status: %s" % t.get("status"), "VM/CT: %s" % (", ".join(vmids) or "unknown"), "Error: %s" % err[:300]])
        self.st["seen_upids"] = seen[-300:]
        if first:
            self.st["backup_init"] = True
            self.dirty = True

    def chk_updates(self, p):
        for n in self._online():
            if self.now - self.st["last_updates"].get(n["name"], 0) < 86400:
                continue
            self.st["last_updates"][n["name"]] = self.now
            self.dirty = True
            ups = self.sys.apt_updates(n["name"])
            if ups:
                self._event("updates", n["name"], "%d update%s available" % (len(ups), "" if len(ups) == 1 else "s"),
                            ["%d package%s can be upgraded on %s" % (len(ups), "" if len(ups) == 1 else "s", n["name"])])

    def chk_cert(self, p):
        if self.now - self.st["cert_checked"] >= 86400:
            self.st["cert_checked"] = self.now
            for n in self._online():
                self.st["cert"][n["name"]] = [list(x) for x in self.sys.cert_days(n["name"])]
            self.dirty = True
        for n in self._online():
            for fn, days in self.st["cert"].get(n["name"], []):
                if days <= p["days"]:
                    self._obs(self._key("cert", n["name"], fn), "cert", n["name"], fn, 1,
                              "certificate expires in %d days" % days if days >= 0 else "certificate expired",
                              ["%s expires in %d days" % (fn, days), "Threshold: within %d days" % p["days"]], 0, "certificate renewed")

    def chk_digest(self, p):
        d = datetime.fromtimestamp(self.now)
        today = d.strftime("%Y-%m-%d")
        at = hm_min(p["at"])
        minute = d.hour * 60 + d.minute
        if self.st["digest_day"] == today or not (at <= minute < at + 120):
            return
        self.st["digest_day"] = today
        self.dirty = True
        cutoff = self.now - 86400
        for n in self._online():
            s = self.agg.summary(n["name"], self.now) if self.agg else {"peak_temp": None, "top_guests": []}
            lines = ["Peak CPU temperature (24h): " + ("%.0f°C" % s["peak_temp"] if s["peak_temp"] is not None else "n/a")]
            if s["top_guests"]:
                lines.append("Busiest guests (avg CPU): " + ", ".join("%s %.2f cores" % (g[0], g[2]) for g in s["top_guests"]))
            st = [x for x in self.snap["storage"] if x.get("total") and (x.get("node") in (None, n["name"]))]
            if st:
                f = max(st, key=lambda x: x["used"] / x["total"])
                lines.append("Fullest storage: %s %.0f%%" % (f["id"], f["used"] / f["total"] * 100))
            fired = [x for x in self.st["fired"] if x[1] == n["name"] and x[0] > cutoff]
            lines.append("Alerts in the last 24h: %d" % len(fired) + ("".join("\n- " + x[2] for x in fired[-5:]) if fired else ""))
            self._event("digest", n["name"], "daily summary", lines)


def build_payload(dest, title, body, priority, url, ts):
    p = {"token": dest["token"], "user": dest["user"], "title": title[:250], "message": (body or "-")[:1024], "priority": int(priority),
         "url": url, "url_title": "Open dashboard", "timestamp": int(ts)}
    if dest.get("sound"):
        p["sound"] = dest["sound"]
    if dest.get("device"):
        p["device"] = dest["device"]
    if int(priority) == 2:
        p["retry"], p["expire"] = 60, 3600
    return p


class EngineRunner:
    """Runs the engine off the collector thread so slow Pushover retries never delay sampling."""

    def __init__(self, engine, agg):
        self.engine, self.agg = engine, agg
        self.ev = threading.Event()
        self.snap = None
        threading.Thread(target=self._loop, daemon=True, name="engine").start()

    def submit(self, snap):
        self.snap = snap
        self.ev.set()

    def _loop(self):
        while True:
            self.ev.wait()
            self.ev.clear()
            try:
                self.engine.tick(self.snap, self.agg)
            except Exception:
                log.exception("engine tick failed")


# ------------------------------------------------------------------------ http
class App:
    def __init__(self, collector, settings, auth, logos, transport, demo):
        self.collector, self.settings, self.auth, self.logos, self.transport, self.demo = collector, settings, auth, logos, transport, demo

    def branding(self, cfg=None):
        b = (cfg or self.settings.get())["branding"]
        return {"name": b["name"], "show_name": b["show_name"], "logo_url": self.logos.url()}

    def settings_view(self, cfg=None):
        cfg = cfg or self.settings.get()
        out = mask_config(cfg)
        out["branding"] = dict(self.branding(cfg), **({"logo_name": cfg["branding"]["logo_name"]} if cfg["branding"].get("logo_name") else {}))
        st = self.collector.state or {}
        out["sender"] = st.get("sender")
        smart = ((self.collector.local_sample or {}).get("smart") or {}).get("available")
        out["capabilities"] = {"smartctl": smart is not False}
        return out

    def send_test(self, body):
        cfg = self.settings.get()
        dest = {"token": "", "user": "", "device": "", "sound": "", "label": "Test"}
        if body.get("id"):
            found = next((d for d in cfg["destinations"] if d["id"] == body["id"]), None)
            if not found:
                return {"ok": False, "error": "Unknown destination"}
            dest.update(found)
        for k in ("token", "user"):
            if body.get(k):
                dest[k] = str(body[k]).strip()
        for k in ("device", "sound"):
            if k in body and body[k] is not None:
                dest[k] = str(body[k])
        if not KEY_RE.match(dest["token"]) or not KEY_RE.match(dest["user"]):
            return {"ok": False, "error": "Token and user key must each be 30 letters and numbers"}
        st = self.collector.state or {}
        title = "Test from %s" % cfg["branding"]["name"]
        msg = "This is a test notification sent from the dashboard%s." % (" on " + st["local"] if st.get("local") else "")
        ok, err = self.transport(build_payload(dest, title, msg, 0, "http://%s:%d/" % (st.get("local") or "localhost", PORT), time.time()))
        return {"ok": ok, "error": err}


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = "pve-dash/" + VERSION
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        # --- plumbing
        def reply(self, code, body=b"", ctype="application/json", headers=None):
            if not isinstance(body, bytes):
                body = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            h = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
            h.update(headers or {})
            for k, v in h.items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def read_body(self, limit):
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if n < 0 or n > limit:
                self.reply(413, {"error": "Request too large"})
                self.close_connection = True
                return None
            return self.rfile.read(n)

        def json_body(self):
            raw = self.read_body(256 * 1024)
            if raw is None:
                return None
            try:
                d = json.loads(raw or b"{}")
                return d if isinstance(d, dict) else {}
            except ValueError:
                self.reply(400, {"error": "Invalid JSON"})
                return None

        def authed(self):
            if not app.auth.configured():
                if app.demo:
                    return True
                self.reply(401, {"error": "No password is set. Run install.sh --set-password on the node.", "setup": True})
                return False
            for part in (self.headers.get("Cookie") or "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == Auth.COOKIE and app.auth.valid(v):
                    return True
            self.reply(401, {"error": "Password required"})
            return False

        # --- GET
        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            path = urlparse.urlparse(self.path).path
            try:
                if path in ("/", "/index.html"):
                    with open(os.path.join(HERE, "index.html"), "rb") as f:
                        return self.reply(200, f.read(), "text/html; charset=utf-8")
                if path == "/healthz":
                    return self.reply(200, b"ok\n", "text/plain")
                if path == "/api/state":
                    return self.reply(200, app.collector.get_state_bytes())
                if path == "/api/local":
                    return self.reply(200, app.collector.get_local_bytes())
                if path == "/api/branding":
                    return self.reply(200, app.branding())
                if path == "/logo":
                    f = app.logos.find()
                    if not f:
                        return self.reply(404, {"error": "No logo"})
                    with open(f[0], "rb") as fh:
                        data = fh.read()
                    h = {"Cache-Control": "public, max-age=31536000, immutable"}
                    if f[1] == "image/svg+xml":
                        h["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'"
                    return self.reply(200, data, f[1], h)
                if path == "/api/auth":
                    ok = app.auth.configured() and any(
                        k == Auth.COOKIE and app.auth.valid(v) for k, _, v in (p.strip().partition("=") for p in (self.headers.get("Cookie") or "").split(";")))
                    return self.reply(200, {"configured": app.auth.configured() or app.demo, "authed": ok or (app.demo and not app.auth.configured())})
                if path == "/api/settings":
                    if self.authed():
                        self.reply(200, app.settings_view())
                    return
                self.reply(404, {"error": "Not found"})
            except Exception:
                log.exception("GET %s failed", path)
                self.reply(500, {"error": "Internal error"})

        # --- PUT
        def do_PUT(self):
            path = urlparse.urlparse(self.path).path
            try:
                if path == "/api/settings":
                    if not self.authed():
                        return
                    body = self.json_body()
                    if body is None:
                        return
                    try:
                        cfg = app.settings.put(body)
                    except ValueError as e:
                        return self.reply(400, {"error": str(e)})
                    return self.reply(200, app.settings_view(cfg))
                if path == "/api/settings/logo":
                    if not self.authed():
                        return
                    ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                    if ctype not in LOGO_TYPES:
                        return self.reply(415, {"error": "Content-Type must be image/png, image/svg+xml, image/webp or image/jpeg"})
                    data = self.read_body(LOGO_MAX)
                    if data is None:
                        return
                    try:
                        url = app.logos.put(data, ctype)
                    except ValueError as e:
                        return self.reply(400, {"error": str(e)})
                    name = urlparse.unquote(self.headers.get("X-Filename") or "")[:100]
                    app.settings.set_logo_name(re.sub(r"[\x00-\x1f]", "", name) or "logo")
                    return self.reply(200, {"logo_url": url})
                self.reply(404, {"error": "Not found"})
            except Exception:
                log.exception("PUT %s failed", path)
                self.reply(500, {"error": "Internal error"})

        def do_DELETE(self):
            if urlparse.urlparse(self.path).path != "/api/settings/logo":
                return self.reply(404, {"error": "Not found"})
            if not self.authed():
                return
            app.logos.delete()
            app.settings.set_logo_name(None)
            self.reply(200, {"logo_url": None})

        # --- POST
        def do_POST(self):
            path = urlparse.urlparse(self.path).path
            try:
                if path == "/api/settings/test":
                    if not self.authed():
                        return
                    body = self.json_body()
                    if body is not None:
                        self.reply(200, app.send_test(body))
                    return
                if path == "/api/auth/login":
                    body = self.json_body()
                    if body is None:
                        return
                    if not app.auth.configured():
                        return self.reply(400, {"error": "No password is set. Run install.sh --set-password on the node."})
                    if not app.auth.verify(str(body.get("password") or "")):
                        time.sleep(1)
                        return self.reply(401, {"error": "Wrong password"})
                    return self.reply(200, {"ok": True}, headers={"Set-Cookie": app.auth.cookie(app.auth.issue())})
                if path == "/api/auth/logout":
                    return self.reply(200, {"ok": True}, headers={"Set-Cookie": app.auth.clear_cookie()})
                self.reply(404, {"error": "Not found"})
            except Exception:
                log.exception("POST %s failed", path)
                self.reply(500, {"error": "Internal error"})

    return Handler


# ------------------------------------------------------------------------ main
def set_password_cli():
    import getpass
    auth = Auth(os.path.join(PRIV, "pve-dash-auth.json"))
    pw = os.environ.get("PVE_DASH_PASSWORD")
    if not pw:
        pw, again = getpass.getpass("Dashboard settings password: "), None
        again = getpass.getpass("Repeat password: ")
        if pw != again:
            sys.exit("Passwords don't match.")
    if len(pw) < 8:
        sys.exit("Use at least 8 characters.")
    os.makedirs(PRIV, exist_ok=True)
    auth.set_password(pw)
    print("Password saved to", auth.path)


def build_app():
    os.makedirs(PRIV, exist_ok=True) if DEMO else None
    settings = Settings(os.path.join(PRIV, "pve-dash.json"))
    auth = Auth(os.path.join(PRIV, "pve-dash-auth.json"))
    logos = LogoStore(PRIV)
    demo_log_only = DEMO and os.environ.get("PVE_DASH_DEMO_SEND") != "1"
    transport = log_transport if demo_log_only else PushoverTransport()
    collector = Collector(sampler=None if DEMO else LocalSampler(), demo=DemoSource() if DEMO else None)
    engine = Engine(settings, JsonState(os.path.join(PRIV, "pve-dash-state.json")), transport,
                    NullSys() if DEMO else SysAPI(), url=os.environ.get("PVE_DASH_URL"))
    runner = EngineRunner(engine, collector.agg)

    def on_state(st):
        st["sender"] = pick_sender(st)
        runner.submit(st)
    collector.on_state = on_state
    return App(collector, settings, auth, logos, transport, DEMO), collector


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if "--set-password" in sys.argv:
        return set_password_cli()
    app, collector = build_app()
    threading.Thread(target=collector.run, daemon=True, name="collector").start()
    srv = ThreadingHTTPServer((BIND, PORT), make_handler(app))
    srv.daemon_threads = True
    log.info("L2 Tech Dashboard %s on %s:%d%s", VERSION, BIND, PORT, " (DEMO)" if DEMO else "")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
