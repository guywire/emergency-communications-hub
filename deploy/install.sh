#!/bin/bash
# ECH Install Script — runs on the server after build_and_scp.ps1 uploads the tarball.
# Do not run this manually; let build_and_scp.ps1 invoke it via ssh -t.
set -e

TARBALL="/tmp/ech_deploy.tar.gz"
INSTALL_DIR="/opt/ech"
SERVICE="ech"

if [ ! -f "$TARBALL" ]; then
  echo "ERROR: $TARBALL not found. Run build_and_scp.ps1 from Windows to upload it."
  exit 1
fi

# Read version from tarball without fully extracting
VERSION=$(tar -xzf "$TARBALL" VERSION -O 2>/dev/null || echo "unknown")
echo ""
echo "=== ECH Install — v${VERSION} ==="
echo ""

# Ensure install directory exists
sudo mkdir -p "$INSTALL_DIR"

echo "Extracting to $INSTALL_DIR ..."
sudo tar -xzf "$TARBALL" -C "$INSTALL_DIR" --overwrite

echo "Setting ownership ..."
sudo chown -R ech:ech "$INSTALL_DIR/ech" 2>/dev/null || true

# Write installed version marker so we can always tell what's on the server
sudo tee "$INSTALL_DIR/INSTALLED_VERSION" > /dev/null <<EOF
${VERSION}
EOF
echo "Version marker written: $INSTALL_DIR/INSTALLED_VERSION"

# Copy config.yaml to /etc/ech/ on first install only — never overwrite on updates
if [ ! -f /etc/ech/config.yaml ]; then
  echo "First install: copying config.yaml to /etc/ech/config.yaml ..."
  sudo mkdir -p /etc/ech
  sudo cp "$INSTALL_DIR/config.yaml" /etc/ech/config.yaml
  sudo chown ech:ech /etc/ech/config.yaml
  echo "  Edit /etc/ech/config.yaml to set your callsign, coordinates, and adapters."
else
  echo "Existing /etc/ech/config.yaml preserved."
fi

# Copy config-sim.yaml on first install only
if [ ! -f /etc/ech/config-sim.yaml ] && [ -f "$INSTALL_DIR/config-sim.yaml" ]; then
  echo "First install: copying config-sim.yaml to /etc/ech/config-sim.yaml ..."
  sudo cp "$INSTALL_DIR/config-sim.yaml" /etc/ech/config-sim.yaml
  sudo chown ech:ech /etc/ech/config-sim.yaml
  echo "  Simulation instance will run on port 8780."
else
  echo "Existing /etc/ech/config-sim.yaml preserved (or not found in package)."
fi

# Deploy ech.service — always update so KillMode/TimeoutStopSec changes land
if [ -f "$INSTALL_DIR/deploy/ech.service" ]; then
  echo "Installing/updating ech.service ..."
  sudo cp "$INSTALL_DIR/deploy/ech.service" /etc/systemd/system/ech.service
  sudo systemctl daemon-reload
  sudo systemctl enable ech
fi

# Deploy ech-sim.service — always update for same reason
if [ -f "$INSTALL_DIR/deploy/ech-sim.service" ]; then
  echo "Installing/updating ech-sim.service ..."
  sudo cp "$INSTALL_DIR/deploy/ech-sim.service" /etc/systemd/system/ech-sim.service
  sudo systemctl daemon-reload
  sudo systemctl enable ech-sim
  if ! sudo systemctl is-active --quiet ech-sim; then
    sudo systemctl start ech-sim
    echo "  Simulation instance started on port 8780."
  else
    echo "Restarting ech-sim ..."
    sudo systemctl stop ech-sim
    sudo pkill -9 -x ech 2>/dev/null || true
    sleep 2
    sudo systemctl start ech-sim
  fi
fi

# Clean up stale installs from wrong locations (only on first deploy after migration)
OLD_DIR="/home/mesh/tmp/ech"
if [ -d "$OLD_DIR/ech" ] && [ -d "$INSTALL_DIR/ech" ]; then
  echo "Removing old install at $OLD_DIR ..."
  sudo rm -rf "$OLD_DIR" 2>/dev/null || rm -rf "$OLD_DIR" 2>/dev/null || true
fi

# Allow ech user to set system clock from GPS (needed when time_sync: true in config)
SUDOERS_FILE="/etc/sudoers.d/ech-gps"
if [ ! -f "$SUDOERS_FILE" ]; then
  echo "Adding GPS clock-sync sudoers entry ..."
  echo 'ech ALL=(root) NOPASSWD: /usr/bin/date' | sudo tee "$SUDOERS_FILE" > /dev/null
  sudo chmod 0440 "$SUDOERS_FILE"
fi

echo "Restarting $SERVICE ..."
sudo systemctl stop "$SERVICE" 2>/dev/null || true
# Force-kill any survivor holding the port (systemd doesn't always wait long enough)
sudo pkill -9 -x ech 2>/dev/null || true
sleep 2
# Clear stale pycache so Python picks up updated bytecode as ech user
sudo find /opt/ech/ech -name '*.pyc' -delete 2>/dev/null || true
sudo systemctl start "$SERVICE"

sleep 3
STATUS=$(sudo systemctl is-active "$SERVICE" 2>/dev/null || echo "unknown")
if [ "$STATUS" = "active" ]; then
  echo ""
  echo "=== ECH v${VERSION} is running ==="
  echo "    Web UI: http://$(hostname -I | awk '{print $1}'):8765"
  echo ""
else
  echo ""
  echo "=== WARNING: service may not have started. Check: ==="
  echo "    sudo journalctl -u ech -n 30"
  echo ""
fi

# Clean up tmp files
rm -f /tmp/ech_deploy.tar.gz /tmp/install.sh
echo "Cleaned up /tmp deploy files."
