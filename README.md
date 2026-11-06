> Yes, this is AI-made. I needed a dashboard, Use it or don't, I don't care. 🙂

# Proxmox Dashboard

A live dashboard for Proxmox VE that runs on the Proxmox host itself. One Python 3 file (standard library only) plus `index.html`. No API token, no lm-sensors. It reads CPU, memory, NICs, disks, temperatures and fans straight from the kernel, guests and storage from `pvesh`, and can send alerts through Pushover.

## Install (one line)

On each Proxmox node, as root:

```sh
bash -c "$(curl -fsSL https://raw.githubusercontent.com/lachlanscott22/ProxmoxDash/main/install.sh)"
```

It asks once for a settings password, then starts the service. Open `http://<node-ip>:8765/`.

Run it on **every node**. There's nothing to configure: each node serves its own readings and the others pull them. A node without the agent still shows CPU and RAM, with a "No live agent" note for the rest.

## Install (manual)

On each node, as root:

```sh
apt install -y python3 curl            # python3 is already there on Proxmox
mkdir -p /opt/pve-dash && cd /opt/pve-dash
B=https://raw.githubusercontent.com/lachlanscott22/ProxmoxDash/main
curl -fsSLO $B/pve-dash.py -O $B/index.html
curl -fsSL  $B/pve-dash.service -o /etc/systemd/system/pve-dash.service
chmod +x pve-dash.py

# temperature drivers (use whichever your CPU needs; drivetemp is for SATA drives)
modprobe coretemp || modprobe k10temp
modprobe drivetemp
printf 'coretemp\nk10temp\ndrivetemp\n' > /etc/modules-load.d/pve-dash.conf   # remove lines that don't apply

python3 pve-dash.py --set-password     # password for changing settings; stored in /etc/pve/priv, shared by all nodes
systemctl daemon-reload
systemctl enable --now pve-dash
```

Optional: `apt install smartmontools` enables SMART alerts.

## Firewall

If the Proxmox firewall is on, allow TCP **8765** from your LAN and between the nodes. Datacenter → Firewall → Add (Direction `in`, Action `ACCEPT`, Protocol `tcp`, Dest. port `8765`), or in `/etc/pve/firewall/cluster.fw`:

```
[RULES]
IN ACCEPT -p tcp -dport 8765 -log nolog
```

The dashboard is plain HTTP with an open read-only view. Don't expose it to the internet; keep it behind a VPN or reverse proxy.

## Using it

- **Settings** (gear icon): name and logo, Pushover destinations, which alerts fire and where they go, quiet hours. It asks for the password you set at install. Change it any time with `python3 /opt/pve-dash/pve-dash.py --set-password`.
- **Alerts** are sent by one node only (the online node with the lowest name that runs the agent), so you never get duplicates. If it goes down, the next takes over.
- **Updates:** when a new version is released, a dot appears on the gear icon and Settings → Software offers **Update** (this node) or **Update all nodes**. Or re-run the install line.

## Configuration

Optional environment variables (put them in `/etc/default/pve-dash`, then `systemctl restart pve-dash`):

| Variable | Default | |
|---|---|---|
| `PVE_DASH_PORT` | `8765` | listen port |
| `PVE_DASH_BIND` | `0.0.0.0` | listen address |
| `PVE_DASH_INTERVAL` | `3` | seconds between samples |
| `PVE_DASH_HISTORY` | `200` | samples kept per node for charts |
| `PVE_DASH_URL` | auto | link used in Pushover messages |
| `PVE_DASH_REPO` | this repo | GitHub `owner/name` checked for updates |

## Try it without Proxmox

```sh
PVE_DASH_DEMO=1 python3 pve-dash.py     # http://localhost:8765/
```

Fake 3-node cluster; Pushover sends are logged, not sent (`PVE_DASH_DEMO_SEND=1` to send for real).

## Uninstall

```sh
systemctl disable --now pve-dash && rm -rf /opt/pve-dash /etc/systemd/system/pve-dash.service /etc/modules-load.d/pve-dash.conf
```

Settings stay in `/etc/pve/priv/pve-dash*.json` (shared by the cluster); delete them if you want them gone.
