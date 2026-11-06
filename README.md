> Yes, this is AI-made. I needed a dashboard and I don't care. Use it or don't. 🙂

# Proxmox Dashboard (`pve-dash`)

A live dashboard for Proxmox VE that runs on the Proxmox host itself. One Python 3 file (standard library only) plus `index.html`. It reads CPU, memory, NICs, disks, temperatures and fans straight from the kernel, reads guests and storage from `pvesh`, and can send alerts through Pushover.

## Install

Copy this folder to the node, then as root:

```sh
./install.sh
```

This copies the files to `/opt/pve-dash`, tries to load `coretemp`, `k10temp` and `drivetemp` (and persists the ones that work in `/etc/modules-load.d/pve-dash.conf`), asks once for a settings password, and enables the `pve-dash` service. Then open `http://<node-ip>:8765/`.

Change the password later with `./install.sh --set-password`. The password hash lives in `/etc/pve/priv/pve-dash-auth.json`, so it is shared by every node. The read-only dashboard needs no password; only the Settings page does.

For SMART alerts, install `smartmontools` (`apt install smartmontools`). Without it, SMART checks are skipped and Settings says so.

## Every node

Run `install.sh` on **every** node. There is nothing to configure. Each agent serves its own raw readings at `/api/local`, and whichever node you open fetches the others' using the node IPs from `/cluster/status`. Nodes without an agent still show CPU and RAM from the cluster, with a "No live agent" message for temperatures, per-thread load, NICs and disks.

Settings (`/etc/pve/priv/pve-dash.json`) and alert state (`/etc/pve/priv/pve-dash-state.json`) live on pmxcfs, so they replicate to every node. Only one node sends alerts: the online node with the lowest name that is running the agent. If it goes down, the next one takes over without re-sending what is already active.

## Updates

Settings has a **Software** section. When a newer version is on GitHub, a dot appears on the gear icon and the section shows **Update** (this node) and **Update all nodes**. Updating downloads the tagged release, checks it compiles and matches the version, keeps the old files as `*.bak`, and restarts the service. Settings are untouched.

Point it at your repository with `PVE_DASH_REPO=owner/name` (set in `/etc/default/pve-dash`). `release.sh` fills the default in from your git remote the first time you publish, so you normally don't need to. The check runs every 6 hours, or press **Check now**.

## Publishing a new version

**Windows:** run `release.bat` (or `.
elease.ps1`) from this folder. The first run asks for your GitHub username (commits use `<username>@users.noreply.github.com`, so your real email stays private) and the repository URL, then it tests, commits, tags and pushes. Later runs: `release.bat minor "What changed"`. Put regexes (your name, email) in `.pushguard.local`, which is never committed, and it will refuse to push if any match.

**Linux/macOS/Git Bash:**

```sh
git remote add origin https://github.com/<owner>/<repo>.git   # once
./release.sh                       # patch bump: runs tests, commits, tags vX.Y.Z, pushes
./release.sh minor "What changed"  # or: major, or an exact version like 2.3.0
./release.sh --no-bump "Docs"      # push without offering an update to nodes
```

It refuses to run off `main`, with failing tests, or if `origin/main` has commits you don't have. Nodes find new versions through the `VERSION` file on `main` and download the matching `vX.Y.Z` tag.

## Firewall

If the Proxmox firewall is enabled, allow TCP 8765 from your LAN, and **between the nodes** (the agents call each other). In the web UI: Datacenter → Firewall → Add (Direction `in`, Action `ACCEPT`, Protocol `tcp`, Dest. port `8765`), or in `/etc/pve/firewall/cluster.fw`:

```
[RULES]
IN ACCEPT -p tcp -dport 8765 -log nolog
```

The dashboard is plain HTTP with an open read-only view. Don't expose port 8765 to the internet; put it behind your VPN or a reverse proxy.

## Configuration

Environment variables (put them in `/etc/default/pve-dash`, then `systemctl restart pve-dash`):

| Variable | Default | |
|---|---|---|
| `PVE_DASH_PORT` | `8765` | listen port |
| `PVE_DASH_BIND` | `0.0.0.0` | listen address |
| `PVE_DASH_INTERVAL` | `3` | seconds between samples |
| `PVE_DASH_HISTORY` | `200` | samples kept per node for charts |
| `PVE_DASH_URL` | auto | link used in Pushover messages |
| `PVE_DASH_DEMO` | unset | `1` = fake 3-node cluster, no Proxmox needed |

Branding (name, logo) and notifications are configured in the dashboard under the gear icon.

## Develop without Proxmox

```sh
PVE_DASH_DEMO=1 python3 pve-dash.py     # http://localhost:8765/
```

Demo mode stores its settings in `./demo-data` (override with `PVE_DASH_PRIV`) and **logs** Pushover sends instead of calling Pushover. Set `PVE_DASH_DEMO_SEND=1` to send for real.

Tests (rule engine, with a fake Pushover transport) and screenshots:

```sh
python -m unittest discover -s tests
pip install playwright && playwright install chromium
python tests/screenshots.py              # writes ./screenshots/*.png
```

## API

`GET /api/state`, `GET /api/local`, `GET /healthz`, `GET /api/branding` and `GET /logo` are open. `/api/settings` (GET/PUT), `/api/settings/logo` (PUT/DELETE) and `/api/settings/test` (POST) need the password (session cookie, 30 days). Every response is `Cache-Control: no-store`, except `/logo` which is cached for a year because its URL changes with the file.
