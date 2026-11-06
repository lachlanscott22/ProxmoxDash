import copy
import importlib.util
import io
import os
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("l2", os.path.join(HERE, "..", "pve-dash.py"))
l2 = importlib.util.module_from_spec(spec)
sys.modules["l2"] = l2
spec.loader.exec_module(l2)

K1, K2, K3 = "A" * 30, "B" * 30, "C" * 30


def ts(h, m=0, day=6):
    return datetime(2026, 11, day, h, m).timestamp()


class FakeTransport:
    """Stands in for Pushover: records every payload instead of calling the network."""

    def __init__(self):
        self.sent = []

    def __call__(self, payload):
        self.sent.append(payload)
        return True, None

    def titles(self):
        return [p["title"] for p in self.sent]

    def clear(self):
        self.sent.clear()


class FakeSettings:
    def __init__(self):
        self.set()

    def set(self, rules=None, delivery=None, dests=None):
        self.body = {"destinations": dests if dests is not None else [
            {"id": "d1", "label": "Phone", "enabled": True, "token": K1, "user": K2},
            {"id": "d2", "label": "OnCall", "enabled": True, "token": K1, "user": K3}],
            "rules": rules or {}, "delivery": delivery or {}}

    def get(self):
        return l2.sanitize_config(copy.deepcopy(self.body), None, strict=True)


class MemState:
    def __init__(self):
        self.d = None

    def load(self):
        return copy.deepcopy(self.d)

    def save(self, d):
        self.d = copy.deepcopy(d)


class FakeSys:
    def __init__(self):
        self.t = []

    def tasks(self):
        return self.t

    def task_log(self, n, u):
        return ["ERROR: Backup of VM 104 failed - storage offline"]

    def apt_updates(self, n):
        return ["a", "b", "c"]

    def cert_days(self, n):
        return [("pve-ssl.pem", 5)]


def node(name, **kw):
    n = {"name": name, "online": True, "local": False, "ip": "10.0.0.1", "agent": True, "maxcpu": 8, "cpu_pct": 0.1,
         "mem_used": 4e9, "mem_total": 16e9, "arc": 0, "cpu_temp": 50.0, "temps": [], "fans": [], "nics": [], "disks": [],
         "cores": [0.1] * 8, "swap_used": 0, "swap_total": 0, "iowait": 0.0, "rootfs_used": 10, "rootfs_total": 100,
         "smart": {"available": True, "disks": []}, "zfs": {"available": True, "pools": [], "ok": True, "text": ""}}
    n.update(kw)
    return n


def snap(local="pve-alpha", nodes=None, guests=None, storage=None, quorate=True):
    nodes = nodes or [node("pve-alpha"), node("pve-bravo")]
    for n in nodes:
        n["local"] = n["name"] == local
    return {"local": local, "cluster": {"name": "homelab", "quorate": quorate}, "nodes": nodes,
            "guests": guests or [], "storage": storage or []}


def guest(vmid=100, name="vm", node="pve-alpha", status="running", cores=1.0, uptime=1000):
    return {"vmid": vmid, "name": name, "node": node, "status": status, "cores_used": cores, "uptime": uptime, "type": "qemu"}


class Base(unittest.TestCase):
    def setUp(self):
        self.now = ts(12)
        self.tr = FakeTransport()
        self.settings = FakeSettings()
        self.state = MemState()
        self.sysapi = FakeSys()
        self.eng = self.mk()

    def mk(self, state=None):
        return l2.Engine(self.settings, state or self.state, self.tr, self.sysapi, clock=lambda: self.now, url="http://x/")

    def tick(self, s=None, eng=None, **kw):
        (eng or self.eng).tick(s or snap(**kw))

    def adv(self, minutes=0, seconds=0):
        self.now += minutes * 60 + seconds


class ForDuration(Base):
    def test_fires_only_after_for_elapsed(self):
        self.settings.set(rules={"cpu_sustained": {"enabled": True, "pct": 90, "for": 5}})
        hot = lambda: snap(nodes=[node("pve-alpha", cpu_pct=0.97), node("pve-bravo")])
        self.tick(hot())
        self.adv(4, 59)
        self.tick(hot())
        self.assertEqual(self.tr.sent, [])
        self.adv(seconds=1)
        self.tick(hot())
        self.assertEqual(len(self.tr.sent), 2)  # two destinations
        self.assertIn("[homelab] pve-alpha: CPU 97% sustained", self.tr.titles()[0])

    def test_dip_resets_the_timer(self):
        self.settings.set(rules={"cpu_sustained": {"enabled": True, "pct": 90, "for": 5}})
        hot = lambda: snap(nodes=[node("pve-alpha", cpu_pct=0.97), node("pve-bravo")])
        self.tick(hot())
        self.adv(4)
        self.tick(hot())
        self.adv(1)
        self.tick(snap())
        self.adv(1)
        self.tick(hot())
        self.adv(4)
        self.tick(hot())
        self.assertEqual(self.tr.sent, [])

    def test_fan_needs_to_have_spun_and_stay_stopped_for_seconds(self):
        self.settings.set(rules={"fan": {"enabled": True, "for": 30}})
        fan = lambda rpm: snap(nodes=[node("pve-alpha", fans=[{"chip": "nct", "label": "CPU fan", "rpm": rpm}]), node("pve-bravo")])
        self.tick(fan(0))
        self.adv(seconds=60)
        self.tick(fan(0))
        self.assertEqual(self.tr.sent, [], "never seen spinning -> no alert")
        self.tick(fan(900))
        self.tick(fan(0))
        self.adv(seconds=29)
        self.tick(fan(0))
        self.assertEqual(self.tr.sent, [])
        self.adv(seconds=1)
        self.tick(fan(0))
        self.assertTrue(self.tr.sent and "CPU fan stopped" in self.tr.titles()[0])


class Hysteresis(Base):
    def hot(self, t):
        return snap(nodes=[node("pve-alpha", cpu_temp=t), node("pve-bravo")])

    def test_temperature_needs_three_degrees_to_clear(self):
        self.settings.set(rules={"cpu_temp": {"enabled": True, "warn": 80, "crit": 90}})
        self.tick(self.hot(81))
        self.assertEqual(len(self.tr.sent), 2)
        self.tr.clear()
        self.adv(1)
        self.tick(self.hot(78))  # below warn, but not 3 below
        self.adv(1)
        self.tick(self.hot(77.5))
        self.assertEqual(self.tr.sent, [], "still active, no resolved yet")
        self.adv(1)
        self.tick(self.hot(76.9))
        self.assertEqual(len(self.tr.sent), 2)
        self.assertEqual({p["priority"] for p in self.tr.sent}, {-1})
        self.assertIn("back to normal", self.tr.titles()[0])

    def test_percent_needs_two_points_to_clear(self):
        self.settings.set(rules={"rootfs": {"enabled": True, "warn": 90}})
        s = lambda used: snap(nodes=[node("pve-alpha", rootfs_used=used, rootfs_total=100), node("pve-bravo")])
        self.tick(s(91))
        self.tr.clear()
        self.adv(1)
        self.tick(s(88.5))
        self.assertEqual(self.tr.sent, [])
        self.adv(1)
        self.tick(s(87.9))
        self.assertEqual(len(self.tr.sent), 2)

    def test_resolved_can_be_disabled(self):
        self.settings.set(rules={"cpu_temp": {"enabled": True}}, delivery={"resolved": False})
        self.tick(self.hot(85))
        self.tr.clear()
        self.adv(1)
        self.tick(self.hot(50))
        self.assertEqual(self.tr.sent, [])


class Escalation(Base):
    def hot(self, t):
        return snap(nodes=[node("pve-alpha", cpu_temp=t), node("pve-bravo")])

    def test_warn_then_critical_steps_priority_up_once(self):
        self.settings.set(rules={"cpu_temp": {"enabled": True, "priority": 0, "warn": 80, "crit": 90}})
        self.tick(self.hot(82))
        self.assertEqual({p["priority"] for p in self.tr.sent}, {0})
        self.tr.clear()
        self.adv(1)
        self.tick(self.hot(92))
        self.assertEqual({p["priority"] for p in self.tr.sent}, {1})
        self.assertIn("(critical)", self.tr.titles()[0])
        self.tr.clear()
        self.adv(1)
        self.tick(self.hot(93))
        self.assertEqual(self.tr.sent, [], "no repeat spam")
        self.adv(1)
        self.tick(self.hot(84))
        self.assertEqual(self.tr.sent, [], "critical -> warn doesn't re-send")

    def test_priority_is_capped_at_emergency(self):
        self.settings.set(rules={"cpu_temp": {"enabled": True, "priority": 2}})
        self.tick(self.hot(95))
        self.assertEqual({p["priority"] for p in self.tr.sent}, {2})
        self.assertTrue(all(p["retry"] == 60 and p["expire"] == 3600 for p in self.tr.sent))


class Repeat(Base):
    def test_resend_every_repeat_minutes(self):
        self.settings.set(rules={"cpu_temp": {"enabled": True}}, delivery={"repeat": 60})
        s = lambda: snap(nodes=[node("pve-alpha", cpu_temp=85), node("pve-bravo")])
        self.tick(s())
        self.tr.clear()
        self.adv(59)
        self.tick(s())
        self.assertEqual(self.tr.sent, [])
        self.adv(1)
        self.tick(s())
        self.assertEqual(len(self.tr.sent), 2)
        self.tr.clear()
        self.adv(30)
        self.tick(s())
        self.assertEqual(self.tr.sent, [])

    def test_zero_means_never(self):
        self.settings.set(rules={"cpu_temp": {"enabled": True}}, delivery={"repeat": 0})
        s = lambda: snap(nodes=[node("pve-alpha", cpu_temp=85), node("pve-bravo")])
        self.tick(s())
        self.tr.clear()
        self.adv(60 * 24)
        self.tick(s())
        self.assertEqual(self.tr.sent, [])


class QuietHours(Base):
    Q = {"quiet": {"enabled": True, "from": "22:30", "to": "06:30", "allow_high": True}, "repeat": 0}

    def test_window_math_across_midnight(self):
        f = l2.in_window
        a, b = 22 * 60 + 30, 6 * 60 + 30
        self.assertTrue(f(23 * 60, a, b))
        self.assertTrue(f(0, a, b))
        self.assertTrue(f(6 * 60 + 29, a, b))
        self.assertFalse(f(6 * 60 + 30, a, b))
        self.assertFalse(f(12 * 60, a, b))
        self.assertTrue(f(22 * 60 + 30, a, b))
        self.assertTrue(f(13 * 60, 12 * 60, 14 * 60))
        self.assertFalse(f(15 * 60, 12 * 60, 14 * 60))
        self.assertFalse(f(5, 100, 100))

    def test_low_and_normal_held_then_one_digest_after(self):
        self.settings.set(rules={"cpu_temp": {"enabled": True, "priority": 0}, "rootfs": {"enabled": True, "priority": -1}}, delivery=self.Q)
        s = lambda: snap(nodes=[node("pve-alpha", cpu_temp=85, rootfs_used=95), node("pve-bravo")])
        self.now = ts(23, 30)
        self.tick(s())
        self.assertEqual(self.tr.sent, [], "held overnight")
        self.now = ts(3, 0, day=7)
        self.tick(s())
        self.assertEqual(self.tr.sent, [])
        self.now = ts(6, 31, day=7)
        self.tick(s())
        self.assertEqual(len(self.tr.sent), 2, "one digest per destination")
        self.assertIn("2 alerts held during quiet hours", self.tr.titles()[0])
        self.assertLessEqual(self.tr.sent[0]["priority"], 0)
        self.assertIn("CPU 85°C", self.tr.sent[0]["message"])
        self.tr.clear()
        self.now = ts(6, 32, day=7)
        self.tick(s())
        self.assertEqual(self.tr.sent, [], "digest sent once")

    def down(self):
        return snap(nodes=[node("pve-alpha"), node("pve-bravo", online=False, agent=False)])

    def test_high_goes_through_when_allowed(self):
        self.settings.set(rules={"node_down": {"enabled": True, "priority": 1, "for": 15}}, delivery=self.Q)
        self.now = ts(2, 0, day=7)
        self.tick(self.down())
        self.adv(seconds=15)
        self.tick(self.down())
        self.assertEqual(len(self.tr.sent), 2)
        self.assertEqual(self.tr.sent[0]["priority"], 1)

    def test_high_held_when_not_allowed(self):
        q = {"quiet": dict(self.Q["quiet"], allow_high=False)}
        self.settings.set(rules={"node_down": {"enabled": True, "priority": 1, "for": 15}}, delivery=q)
        self.now = ts(2, 0, day=7)
        self.tick(self.down())
        self.adv(seconds=15)
        self.tick(self.down())
        self.assertEqual(self.tr.sent, [])

    def test_held_items_are_persisted_for_a_new_sender(self):
        self.settings.set(rules={"cpu_temp": {"enabled": True}}, delivery=self.Q)
        self.now = ts(23, 0)
        self.tick(snap(nodes=[node("pve-alpha", cpu_temp=85), node("pve-bravo")]))
        self.assertEqual(len(self.state.d["held"]), 1)


class Failover(Base):
    def test_only_lowest_named_agent_node_sends_and_takeover_does_not_refire(self):
        self.settings.set(rules={"cpu_temp": {"enabled": True}}, delivery={"repeat": 0})
        alpha, bravo = self.mk(), self.mk()
        up = lambda local: snap(local=local, nodes=[node("pve-alpha", cpu_temp=88), node("pve-bravo", cpu_temp=40)])
        self.assertEqual(l2.pick_sender(up("pve-alpha")), "pve-alpha")
        self.assertFalse(bravo.tick(up("pve-bravo")))
        self.assertEqual(self.tr.sent, [])
        self.assertTrue(alpha.tick(up("pve-alpha")))
        self.assertEqual(len(self.tr.sent), 2)
        self.tr.clear()
        self.assertIn("cpu_temp:pve-alpha:cpu", self.state.d["alerts"])
        self.adv(1)  # alpha dies; bravo takes over and loads the shared state file
        gone = snap(local="pve-bravo", nodes=[node("pve-alpha", online=False, agent=False, cpu_temp=None), node("pve-bravo", cpu_temp=40)])
        self.assertTrue(bravo.tick(gone))
        self.assertEqual(self.tr.sent, [], "the new sender doesn't re-fire existing alerts or claim a false resolve")

    def test_agentless_node_is_never_sender(self):
        s = snap(nodes=[node("pve-alpha", agent=False), node("pve-bravo")], local="pve-bravo")
        self.assertEqual(l2.pick_sender(s), "pve-bravo")

    def test_non_sender_evaluates_nothing(self):
        self.settings.set(rules={"cpu_temp": {"enabled": True}})
        e = self.mk()
        self.assertFalse(e.tick(snap(local="pve-bravo", nodes=[node("pve-alpha", cpu_temp=99), node("pve-bravo", cpu_temp=99)])))
        self.assertEqual(self.tr.sent, [])


class Routing(Base):
    def test_null_goes_to_every_enabled_destination(self):
        self.settings.set(rules={"cpu_temp": {"enabled": True, "to": None}},
                          dests=[{"id": "d1", "label": "A", "enabled": True, "token": K1, "user": K2},
                                 {"id": "d2", "label": "B", "enabled": False, "token": K1, "user": K3},
                                 {"id": "d3", "label": "C", "enabled": True, "token": K1, "user": K1}])
        self.tick(nodes=[node("pve-alpha", cpu_temp=95), node("pve-bravo")])
        self.assertEqual(sorted(p["user"] for p in self.tr.sent), sorted([K2, K1]))

    def test_list_limits_to_chosen_and_skips_off(self):
        self.settings.set(rules={"cpu_temp": {"enabled": True, "to": ["d2", "d3"]}},
                          dests=[{"id": "d1", "label": "A", "enabled": True, "token": K1, "user": K2},
                                 {"id": "d2", "label": "B", "enabled": True, "token": K1, "user": K3},
                                 {"id": "d3", "label": "C", "enabled": False, "token": K1, "user": K1}])
        self.tick(nodes=[node("pve-alpha", cpu_temp=95), node("pve-bravo")])
        self.assertEqual([p["user"] for p in self.tr.sent], [K3])

    def test_payload_shape(self):
        self.settings.set(rules={"cpu_temp": {"enabled": True}},
                          dests=[{"id": "d1", "label": "A", "enabled": True, "token": K1, "user": K2, "device": "ipad", "sound": "siren"}])
        self.tick(nodes=[node("pve-alpha", cpu_temp=91), node("pve-bravo")], guests=[guest(1, "busy", cores=3.2), guest(2, "idle", cores=0.1)])
        p = self.tr.sent[0]
        self.assertEqual((p["device"], p["sound"], p["url"], p["token"]), ("ipad", "siren", "http://x/", K1))
        self.assertTrue(p["title"].startswith("[homelab] pve-alpha: CPU 91°C (critical)"))
        for want in ("CPU temperature 91.0", "critical at 90", "Active for", "busy (#1) 3.20 cores"):
            self.assertIn(want, p["message"])


class OtherRules(Base):
    def test_guest_down_only_without_task(self):
        self.settings.set(rules={"guest_down": {"enabled": True}})
        self.tick(guests=[guest(100, "a"), guest(101, "b")])
        self.sysapi.t = [{"id": "101", "type": "qmshutdown", "starttime": self.now, "endtime": self.now}]
        self.adv(seconds=3)
        self.tick(guests=[guest(100, "a", status="stopped"), guest(101, "b", status="stopped")])
        self.assertEqual(len(self.tr.sent), 2)
        self.assertTrue(all("a (#100) stopped unexpectedly" in t for t in self.tr.titles()))

    def test_guest_restart_on_uptime_regression(self):
        self.settings.set(rules={"guest_restart": {"enabled": True}})
        self.tick(guests=[guest(100, "a", uptime=5000)])
        self.adv(seconds=3)
        self.tick(guests=[guest(100, "a", uptime=4)])
        self.assertIn("rebooted", self.tr.titles()[0])

    def test_backup_failure_names_vmid_and_error(self):
        self.settings.set(rules={"backup": {"enabled": True}})
        self.sysapi.t = [{"upid": "u0", "type": "vzdump", "endtime": 1, "status": "ERROR: old", "node": "pve-alpha"}]
        self.tick()
        self.assertEqual(self.tr.sent, [], "old failures aren't replayed on first run")
        self.sysapi.t.append({"upid": "u1", "type": "vzdump", "endtime": 2, "status": "ERROR: job errors", "node": "pve-alpha", "id": ""})
        self.adv(2)
        self.tick()
        self.assertIn("backup failed (104)", self.tr.titles()[0])
        self.assertIn("storage offline", self.tr.sent[0]["message"])

    def test_node_down_and_back_online(self):
        self.settings.set(rules={"node_down": {"enabled": True, "for": 60}}, delivery={"resolved": False})
        down = lambda: snap(nodes=[node("pve-alpha"), node("pve-bravo", online=False, agent=False)])
        self.tick(down())
        self.adv(seconds=59)
        self.tick(down())
        self.assertEqual(self.tr.sent, [])
        self.adv(seconds=1)
        self.tick(down())
        self.assertIn("node offline", self.tr.titles()[0])
        self.tr.clear()
        self.adv(1)
        self.tick()
        self.assertIn("node back online", self.tr.titles()[0], "back-online is sent even with resolved messages off")

    def test_nic_slowdown_after_being_up(self):
        self.settings.set(rules={"nic": {"enabled": True}})
        nic = lambda up, sp: snap(nodes=[node("pve-alpha", nics=[{"name": "enp1", "up": up, "speed": sp, "rx": 0, "tx": 0}]), node("pve-bravo")])
        self.tick(nic(True, 2500))
        self.tick(nic(True, 1000))
        self.assertIn("slowed to 1000", self.tr.titles()[0])

    def test_smart_growth_and_failure(self):
        self.settings.set(rules={"smart": {"enabled": True}})
        d = lambda passed, re_: snap(nodes=[node("pve-alpha", smart={"available": True, "disks": [
            {"dev": "sda", "passed": passed, "realloc": re_, "pending": 0, "media_errors": None}]}), node("pve-bravo")])
        self.tick(d(True, 0))
        self.assertEqual(self.tr.sent, [])
        self.tick(d(True, 8))
        self.assertIn("reallocated sectors", self.tr.titles()[0])
        self.tr.clear()
        self.tick(d(False, 8))
        self.assertIn("failed its SMART", self.tr.titles()[0])

    def test_storage_levels(self):
        self.settings.set(rules={"storage": {"enabled": True, "warn": 85, "crit": 95}})
        self.tick(storage=[{"id": "nas", "node": None, "used": 96, "total": 100, "shared": True, "status": "available"}])
        self.assertEqual(len(self.tr.sent), 2)
        self.assertIn("[homelab] cluster: nas 96% full (critical)", self.tr.titles()[0])
        self.assertEqual(self.tr.sent[0]["priority"], 1)

    def test_digest_once_a_day(self):
        self.settings.set(rules={"digest": {"enabled": True, "at": "08:00"}})
        self.now = ts(8, 5)
        agg = type("A", (), {"summary": lambda s, n, now=None: {"peak_temp": 71.0, "top_guests": [("jf", 101, 2.5)]}})()
        s = lambda: snap(storage=[{"id": "z", "node": "pve-alpha", "used": 50, "total": 100, "shared": False, "status": "available"}])
        self.eng.tick(s(), agg)
        self.assertEqual(len(self.tr.sent), 4, "2 nodes x 2 destinations")
        self.assertIn("Peak CPU temperature (24h): 71°C", self.tr.sent[0]["message"])
        self.assertIn("z 50%", self.tr.sent[0]["message"])
        self.tr.clear()
        self.adv(10)
        self.eng.tick(s(), agg)
        self.assertEqual(self.tr.sent, [])


class Helpers(unittest.TestCase):
    def test_rate_tracker(self):
        r = l2.RateTracker(hold=25)
        self.assertEqual(r.update("k", 100, 0), 0)
        self.assertEqual(r.update("k", 100, 3), 0)
        self.assertEqual(r.update("k", 1100, 10), 100)  # delta / time since last change
        self.assertEqual(r.update("k", 1100, 20), 100)  # held
        self.assertEqual(r.update("k", 1100, 36), 0)    # more than 25s since last change
        self.assertEqual(r.update("k", 50, 40), 0)      # counter reset
        self.assertEqual(r.update("k", 150, 50), 10)

    def test_sanitize_clamps_and_keeps_secrets(self):
        cur = {"destinations": [{"id": "d1", "label": "x", "token": K1, "user": K2}]}
        out = l2.sanitize_config({"destinations": [{"id": "d1", "label": "Phone"}],
                                  "rules": {"cpu_temp": {"warn": 500, "crit": 10, "priority": 9}, "fan": {"for": 7}, "digest": {"at": "25:99"}},
                                  "delivery": {"repeat": -5}}, cur)
        d = out["destinations"][0]
        self.assertEqual((d["token"], d["user"]), (K1, K2))
        r = out["rules"]
        self.assertEqual((r["cpu_temp"]["warn"], r["cpu_temp"]["priority"]), (110, 0))
        self.assertGreater(r["cpu_temp"]["crit"], r["cpu_temp"]["warn"])
        self.assertEqual(r["fan"]["for"] % 5, 0)
        self.assertEqual(r["digest"]["at"], "08:00")
        self.assertEqual(out["delivery"]["repeat"], 0)
        with self.assertRaises(ValueError):
            l2.sanitize_config({"destinations": [{"label": "n", "token": "short", "user": K2}]}, None)
        m = l2.mask_config(out)
        self.assertNotIn("token", m["destinations"][0])
        self.assertEqual(m["destinations"][0]["token_hint"], "•••" + K1[-4:])

    def test_svg_and_magic_validation(self):
        good = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1"><rect width="1" height="1"/></svg>'
        self.assertEqual(l2.sniff_logo(good), "image/svg+xml")
        self.assertTrue(l2.svg_is_safe(good))
        for bad in (b"<svg><script>alert(1)</script></svg>", b'<svg onload="x()"></svg>', b'<svg><image href="http://e.com/a.png"/></svg>',
                    b'<svg><a xlink:href="https://e.com"/></svg>', b"<svg><style>@import 'x';</style></svg>"):
            self.assertFalse(l2.svg_is_safe(bad), bad)
        self.assertTrue(l2.svg_is_safe(b'<svg><use href="#a"/></svg>'))
        self.assertEqual(l2.sniff_logo(b"\x89PNG\r\n\x1a\n" + b"0" * 8), "image/png")
        self.assertIsNone(l2.sniff_logo(b"GIF89a"))
        with tempfile.TemporaryDirectory() as d:
            ls = l2.LogoStore(d)
            with self.assertRaises(ValueError):
                ls.put(b"not a png at all", "image/png")
            url = ls.put(good, "image/svg+xml")
            self.assertTrue(url.startswith("logo?v="))
            ls.delete()
            self.assertIsNone(ls.url())

    def test_auth_cookie(self):
        with tempfile.TemporaryDirectory() as d:
            a = l2.Auth(os.path.join(d, "auth.json"))
            self.assertFalse(a.configured())
            a.set_password("correct horse")
            self.assertTrue(a.verify("correct horse"))
            self.assertFalse(a.verify("nope"))
            tok = a.issue(now=1000)
            self.assertTrue(a.valid(tok, now=2000))
            self.assertFalse(a.valid(tok, now=1000 + 31 * 86400))
            self.assertFalse(a.valid(tok[:-2] + "00", now=2000))
            self.assertIn("HttpOnly", a.cookie(tok))

    def test_pushover_transport_retries_and_error_text(self):
        calls, sleeps = [], []

        def flaky(req, timeout=None):
            calls.append(timeout)
            if len(calls) < 3:
                raise urllib.error.URLError("down")
            return io.BytesIO(b'{"status":1}')

        t = l2.PushoverTransport(sleep=sleeps.append, opener=flaky)
        self.assertEqual(t({"token": K1}), (True, None))
        self.assertEqual(calls, [10, 10, 10])
        self.assertEqual(sleeps, [1, 3])

        def bad(req, timeout=None):
            raise urllib.error.HTTPError("u", 400, "bad", {}, io.BytesIO(b'{"status":0,"errors":["application token is invalid"]}'))

        self.assertEqual(l2.PushoverTransport(sleep=lambda s: None, opener=bad)({"token": "x"}), (False, "application token is invalid"))


if __name__ == "__main__":
    unittest.main()
