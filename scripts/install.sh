#!/usr/bin/env bash
# ECH install script — Debian 12 / Ubuntu 22.04 (LXC container or bare metal)
# Run as root or with sudo.
# Usage: bash install.sh [--no-systemd]

set -euo pipefail

ECH_USER="ech"
ECH_DIR="/opt/ech"
ECH_CONFIG="/etc/ech/config.yaml"
PYTHON_MIN="3.11"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
log()  { echo -e "${GREEN}[ECH]${NC} $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
err()  { echo -e "${RED}[ERR]${NC} $*"; exit 1; }

[[ $EUID -ne 0 ]] && err "Run as root: sudo bash install.sh"

USE_SYSTEMD=true
[[ "${1:-}" == "--no-systemd" ]] && USE_SYSTEMD=false

log "=== Emergency Communications Hub installer ==="
log "Target: $ECH_DIR"
log "Config: $ECH_CONFIG"

# ── System packages ────────────────────────────────────────────────────────
log "Installing system packages…"
apt-get update -qq
apt-get install -y --no-install-recommends \
  python3 python3-pip python3-venv python3-dev \
  git curl ca-certificates \
  libusb-1.0-0 libudev-dev \
  avahi-daemon avahi-utils \
  usbutils \
  mosquitto mosquitto-clients \
  bluez 2>/dev/null || warn "bluez not available — BLE transports disabled"

# ── Python version check ───────────────────────────────────────────────────
PYVER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
log "Python version: $PYVER"
python3 -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)" \
  || err "Python 3.11+ required. Installed: $PYVER"

# ── ECH system user ────────────────────────────────────────────────────────
if ! id "$ECH_USER" &>/dev/null; then
  log "Creating system user '$ECH_USER'…"
  useradd --system --create-home --shell /bin/bash \
    --groups dialout,plugdev "$ECH_USER" 2>/dev/null || true
fi
usermod -aG dialout,plugdev "$ECH_USER" 2>/dev/null || true

# ── udev rules for USB serial devices ─────────────────────────────────────
log "Installing udev rules…"
cat > /etc/udev/rules.d/99-ech-serial.rules << 'EOF'
# ECH: grant ech user access to USB serial adapters
SUBSYSTEM=="tty", ATTRS{idVendor}=="10c4", ATTRS{idProduct}=="ea60", \
  SYMLINK+="ttyMESHTASTIC", GROUP="dialout", MODE="0664"
SUBSYSTEM=="tty", ATTRS{idVendor}=="1a86", ATTRS{idProduct}=="7523", \
  SYMLINK+="ttyMESHCORE", GROUP="dialout", MODE="0664"
SUBSYSTEM=="tty", ATTRS{idVendor}=="0403", ATTRS{idProduct}=="6001", \
  SYMLINK+="ttyTNC", GROUP="dialout", MODE="0664"
EOF
udevadm control --reload-rules 2>/dev/null || true

# ── Install ECH ────────────────────────────────────────────────────────────
log "Installing ECH to $ECH_DIR…"
mkdir -p "$ECH_DIR"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -f "$SCRIPT_DIR/pyproject.toml" ]]; then
  log "Installing from local source…"
  cp -r "$SCRIPT_DIR/." "$ECH_DIR/"
else
  err "Run install.sh from the ECH project directory"
fi

# ── Python virtual environment ─────────────────────────────────────────────
log "Creating Python virtual environment…"
python3 -m venv "$ECH_DIR/.venv"
HOME=/root "$ECH_DIR/.venv/bin/pip" install --upgrade pip -q
HOME=/root "$ECH_DIR/.venv/bin/pip" install -e "$ECH_DIR[dev]" -q
log "Python dependencies installed"

# ── Config ────────────────────────────────────────────────────────────────
mkdir -p /etc/ech
# ── Mosquitto (MQTT broker) ───────────────────────────────────────────────
if ! systemctl is-active mosquitto &>/dev/null 2>&1; then
  log "Enabling Mosquitto MQTT broker..."
  # Allow local connections without auth by default
  cat > /etc/mosquitto/conf.d/ech.conf << 'MQTTEOF'
listener 1883 127.0.0.1
allow_anonymous true
MQTTEOF
  systemctl enable mosquitto
  systemctl start mosquitto
  log "Mosquitto started on port 1883 (localhost only)"
fi

if [[ ! -f "$ECH_CONFIG" ]]; then
  log "Creating default config at $ECH_CONFIG…"
  cp "$ECH_DIR/config.yaml" "$ECH_CONFIG"
  warn "Edit $ECH_CONFIG to configure your adapters and callsign before starting."
else
  log "Config already exists at $ECH_CONFIG, skipping."
fi

# ── Data directory ─────────────────────────────────────────────────────────
mkdir -p /var/lib/ech /var/log/ech
chown "$ECH_USER:$ECH_USER" /var/lib/ech /var/log/ech

# ── Systemd service ────────────────────────────────────────────────────────
if $USE_SYSTEMD && command -v systemctl &>/dev/null; then
  log "Installing systemd service…"
  cat > /etc/systemd/system/ech.service << EOF
[Unit]
Description=Emergency Communications Hub
After=network.target
Wants=network.target

[Service]
Type=simple
User=$ECH_USER
Group=$ECH_USER
WorkingDirectory=/var/lib/ech
ExecStart=$ECH_DIR/.venv/bin/ech --config $ECH_CONFIG
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable ech
  log "Systemd service installed. Start with: systemctl start ech"
  log "View logs with: journalctl -u ech -f"
else
  warn "Skipping systemd setup. Run manually with:"
  warn "  $ECH_DIR/.venv/bin/ech --config $ECH_CONFIG"
fi

# ── Avahi mDNS ─────────────────────────────────────────────────────────────
if command -v avahi-daemon &>/dev/null; then
  cat > /etc/avahi/services/ech.service << 'EOF'
<?xml version="1.0" standalone='no'?>
<!DOCTYPE service-group SYSTEM "avahi-service.dtd">
<service-group>
  <name>Emergency Communications Hub</name>
  <service>
    <type>_http._tcp</type>
    <port>8765</port>
  </service>
</service-group>
EOF
  systemctl restart avahi-daemon 2>/dev/null || true
  log "mDNS registered — accessible at http://ech.local:8765"
fi

# ── Passwordless sudo for service restarts ─────────────────────────────────
# The ECH web UI can restart individual services. Allow the ech user to run
# only the specific systemctl restart commands it needs — nothing else.
log "Configuring passwordless sudo for service restarts…"
cat > /etc/sudoers.d/ech-services << 'EOF'
# Allow the ech service account to restart ECH-related services only.
# Generated by ECH install.sh — edit with visudo /etc/sudoers.d/ech-services
ech ALL=(root) NOPASSWD: /bin/systemctl restart ech, \
                          /bin/systemctl restart pat, \
                          /bin/systemctl restart asterisk, \
                          /bin/systemctl restart mosquitto, \
                          /bin/systemctl restart prometheus, \
                          /bin/systemctl restart avahi-daemon, \
                          /usr/bin/systemctl restart ech, \
                          /usr/bin/systemctl restart pat, \
                          /usr/bin/systemctl restart asterisk, \
                          /usr/bin/systemctl restart mosquitto, \
                          /usr/bin/systemctl restart prometheus, \
                          /usr/bin/systemctl restart avahi-daemon
EOF
chmod 440 /etc/sudoers.d/ech-services
log "Sudoers entry written to /etc/sudoers.d/ech-services"

chown -R "$ECH_USER:$ECH_USER" "$ECH_DIR"

echo ""
log "=== Installation complete ==="
log "UI: http://$(hostname -I | awk '{print $1}'):8765"
log "Config: $ECH_CONFIG"
log "Database: /var/lib/ech/ech.db"
if $USE_SYSTEMD && command -v systemctl &>/dev/null; then
  log "Start: systemctl start ech"
fi
