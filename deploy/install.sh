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
VERSION=$(tar -xzf "$TARBALL" ./VERSION -O 2>/dev/null || tar -xzf "$TARBALL" VERSION -O 2>/dev/null || echo "unknown")
echo ""
echo "=== ECH Install — v${VERSION} ==="
echo ""

# Ensure install directory and data directory exist with correct ownership
sudo mkdir -p "$INSTALL_DIR"
sudo mkdir -p /var/lib/ech
sudo chown ech:ech /var/lib/ech 2>/dev/null || true

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
  # Set DB path to absolute /opt/ech/ech.db so it's on the main drive regardless of WorkingDirectory
  sudo sed -i 's|^  path: "ech.db"|  path: /opt/ech/ech.db|' /etc/ech/config.yaml
  sudo chown ech:ech /etc/ech/config.yaml
  echo "  Edit /etc/ech/config.yaml to set your callsign, coordinates, and adapters."
else
  echo "Existing /etc/ech/config.yaml preserved."
fi

# Always ensure config files are owned by the ech service user so the UI can save settings
sudo chown ech:ech /etc/ech/config.yaml 2>/dev/null || true

# Add pskreporter section if missing (required for correct User-Agent to avoid 503s)
sudo /opt/ech/.venv/bin/python3 - <<'PYEOF'
import yaml, sys
path = "/etc/ech/config.yaml"
try:
    with open(path) as f:
        cfg = yaml.safe_load(f) or {}
    if "pskreporter" not in cfg:
        cfg["pskreporter"] = {"contact_email": ""}
        with open(path, "w") as f:
            yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)
        print("  pskreporter section added to /etc/ech/config.yaml — set contact_email!")
except Exception as e:
    print(f"  WARNING: could not patch pskreporter config: {e}", file=sys.stderr)
PYEOF

# Lower stale mesh_bot cooldown defaults (only touches values still at first-install defaults)
sudo /opt/ech/.venv/bin/python3 - <<'PYEOF'
import yaml, sys
path = "/etc/ech/config.yaml"
try:
    with open(path) as f:
        cfg = yaml.safe_load(f) or {}
    bot = cfg.setdefault("mesh_bot", {})
    changed = False
    if bot.get("per_user_cooldown_sec", 30) >= 30:
        bot["per_user_cooldown_sec"] = 5
        changed = True
    if bot.get("global_cooldown_sec", 5) >= 5:
        bot["global_cooldown_sec"] = 2
        changed = True
    if bot.get("max_reply_len", 200) >= 200:
        bot["max_reply_len"] = 160
        changed = True
    if changed:
        with open(path, "w") as f:
            yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)
        print("  mesh_bot cooldown defaults updated in /etc/ech/config.yaml")
    else:
        print("  mesh_bot config already customised, no changes made")
except Exception as e:
    print(f"  WARNING: could not patch mesh_bot config: {e}", file=sys.stderr)
PYEOF

# Copy config-sim.yaml on first install only
if [ ! -f /etc/ech/config-sim.yaml ] && [ -f "$INSTALL_DIR/config-sim.yaml" ]; then
  echo "First install: copying config-sim.yaml to /etc/ech/config-sim.yaml ..."
  sudo cp "$INSTALL_DIR/config-sim.yaml" /etc/ech/config-sim.yaml
  sudo chown ech:ech /etc/ech/config-sim.yaml
  echo "  Simulation instance will run on port 8780."
else
  echo "Existing /etc/ech/config-sim.yaml preserved (or not found in package)."
  sudo chown ech:ech /etc/ech/config-sim.yaml 2>/dev/null || true
fi

# Deploy ech.service — always update so KillMode/TimeoutStopSec changes land
if [ -f "$INSTALL_DIR/deploy/ech.service" ]; then
  echo "Installing/updating ech.service ..."
  sudo cp "$INSTALL_DIR/deploy/ech.service" /etc/systemd/system/ech.service
  sudo systemctl daemon-reload
  sudo systemctl enable ech
fi

# Deploy ech-sim.service — always update the unit file so KillMode/TimeoutStopSec
# changes land, but respect an operator's deliberate `systemctl disable ech-sim`:
# only auto-enable/(re)start it if it isn't already explicitly disabled.
if [ -f "$INSTALL_DIR/deploy/ech-sim.service" ]; then
  echo "Installing/updating ech-sim.service ..."
  sudo cp "$INSTALL_DIR/deploy/ech-sim.service" /etc/systemd/system/ech-sim.service
  sudo systemctl daemon-reload
  # Deliberately no `sudo` here: `is-enabled` is a read-only query that doesn't
  # need root, and running it under sudo INSIDE a command-substitution subshell
  # silently produced empty output on this host (confirmed: same sudo command
  # works fine standalone, only breaks captured via `$(...)`) — was not a
  # set -e/exit-code issue as first suspected, just don't sudo a query that
  # doesn't require it.
  sim_enabled_state=$(systemctl is-enabled ech-sim 2>/dev/null || true)
  if [ "$sim_enabled_state" = "disabled" ]; then
    echo "  ech-sim is disabled (deliberately, by an operator) — leaving it stopped."
  else
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

# Ensure numpy<2 is installed — numpy 2.x uses AVX2 which the Proxmox LXC CPU (SSE2 only) doesn't support.
# numpy>=2 will cause SIGILL in any thread that imports it, killing the ECH process.
NUMPY_VER=$(sudo /opt/ech/.venv/bin/python3 -c "import numpy; print(numpy.__version__)" 2>/dev/null || echo "missing")
NUMPY_MAJOR=$(echo "$NUMPY_VER" | cut -d. -f1)
if [ "$NUMPY_MAJOR" != "1" ]; then
  echo "Pinning numpy to 1.26.4 (installed: $NUMPY_VER — AVX2 incompatible with this CPU) ..."
  sudo /opt/ech/.venv/bin/pip install -q "numpy==1.26.4"
else
  echo "numpy $NUMPY_VER OK (< 2.0)"
fi

# Ensure the `adventure` package (Colossal Cave Adventure, used by the mesh bot's
# `mud` command) is installed. The deploy tarball only contains ech/ + config,
# not pyproject.toml, so a new dependency needs its own explicit install check
# here — the venv is never recreated from pyproject.toml on a normal deploy.
if ! sudo /opt/ech/.venv/bin/python3 -c "import adventure" 2>/dev/null; then
  echo "Installing adventure (mesh bot 'mud' command dependency) ..."
  sudo /opt/ech/.venv/bin/pip install -q "adventure>=1.7"
else
  echo "adventure package OK"
fi

# Sound-card support (cw_audio adapter): sounddevice needs the PortAudio system
# library on Linux — the pip wheel does not bundle it there.
if ! dpkg -s libportaudio2 >/dev/null 2>&1; then
  echo "Installing libportaudio2 (sound card support) ..."
  sudo apt-get install -y -q libportaudio2 || echo "  WARNING: libportaudio2 install failed — cw_audio adapter will not connect"
fi
if ! sudo /opt/ech/.venv/bin/python3 -c "import sounddevice" 2>/dev/null; then
  echo "Installing sounddevice (cw_audio adapter dependency) ..."
  sudo /opt/ech/.venv/bin/pip install -q "sounddevice>=0.4"
else
  echo "sounddevice package OK"
fi

# Text-to-speech (Asterisk adapter's speak() — offline, no API key needed)
if ! command -v espeak-ng >/dev/null 2>&1; then
  echo "Installing espeak-ng (PBX text-to-speech support) ..."
  sudo apt-get install -y -q espeak-ng || echo "  WARNING: espeak-ng install failed — PBX speak() will not work"
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
