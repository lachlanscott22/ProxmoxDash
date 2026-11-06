#!/bin/sh
# Install Proxmox Dashboard on this Proxmox node. Run as root, once per node.
#   ./install.sh                 install / upgrade
#   ./install.sh --set-password  change the settings password only
set -e
[ "$(id -u)" -eq 0 ] || { echo "Run as root." >&2; exit 1; }
SRC="$(cd "$(dirname "$0")" && pwd)"
DEST=/opt/pve-dash
AUTH=/etc/pve/priv/pve-dash-auth.json

if [ "$1" = "--set-password" ]; then
  exec python3 "$SRC/pve-dash.py" --set-password
fi

command -v python3 >/dev/null || { echo "python3 is required." >&2; exit 1; }
mkdir -p "$DEST"
cp "$SRC/pve-dash.py" "$SRC/index.html" "$DEST/"
chmod 755 "$DEST/pve-dash.py"
cp "$SRC/pve-dash.service" /etc/systemd/system/pve-dash.service

# Temperature drivers (best effort). Not every board has all of them.
: > /etc/modules-load.d/pve-dash.conf.new
for m in coretemp k10temp drivetemp; do
  if modprobe "$m" 2>/dev/null; then echo "$m" >> /etc/modules-load.d/pve-dash.conf.new; echo "loaded $m"; else echo "skipped $m (not supported here)"; fi
done
mv /etc/modules-load.d/pve-dash.conf.new /etc/modules-load.d/pve-dash.conf

command -v smartctl >/dev/null || echo "Note: smartctl not found, SMART alerts are skipped. Install with: apt install smartmontools"

# The settings password lives in /etc/pve/priv, so it is shared by every node.
if [ ! -s "$AUTH" ]; then
  echo "Choose a password for changing dashboard settings (the dashboard itself stays read-only and open)."
  python3 "$DEST/pve-dash.py" --set-password
fi

systemctl daemon-reload
systemctl enable --now pve-dash.service
systemctl restart pve-dash.service
IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo "Done. Open http://${IP:-this-node}:8765/"
