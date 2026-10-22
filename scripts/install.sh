#!/usr/bin/env bash
# ECH install script — Debian 12 / Ubuntu 22.04 (LXC container or bare metal)
# Run as root or with sudo.
# Usage: bash install.sh [--no-systemd] [--with-mdns] [--with-prometheus]
#                        [--backup-disk DEVICE|DIR]
#                        [--with-speedtest | --no-speedtest] [--accept-legal]
#
#   --accept-legal     agree to the legal / licensing / interference terms
#                      (README.md) without the interactive prompt - required
#                      when there is no terminal to ask on
#   --with-speedtest   install the Ookla speedtest CLI and turn on the network
#                      probe (ISP speed tests + traceroutes). HIGH DATA USE:
#                      ~1.5 GB per test on gigabit, every 30 min by default =
#                      ~70 GB/day, ~2 TB/month. Asked interactively if neither
#                      flag is given; skipped without a terminal.
#   --no-speedtest     don't ask; leave speed tests off (can be added later by
#                      re-running with --with-speedtest)
#   --no-systemd       don't install/enable the ech systemd unit
#   --with-mdns        install avahi and advertise the hub as <hostname>.local
#                      (OFF by default - ECH works fine by IP address)
#   --with-prometheus  install a local Prometheus server scraping ECH's
#                      /metrics (OFF by default - ECH does not need it; it only
#                      keeps history for Grafana-style dashboards)
#   --backup-disk X    nightly backup of the database + config to a second disk
#                      or USB stick (e.g. /dev/sdb1, must already have a
#                      filesystem - it is never formatted) or to a directory.
#                      Keeps 14 archives. OFF by default. Can also be run later:
#                      sudo bash scripts/setup_backup.sh /dev/sdb1

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
WITH_MDNS=false
WITH_PROMETHEUS=false
BACKUP_DISK=""
SPEEDTEST=""          # yes / no / "" = ask
ACCEPT_LEGAL=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-systemd)      USE_SYSTEMD=false ;;
    --with-mdns)       WITH_MDNS=true ;;
    --with-prometheus) WITH_PROMETHEUS=true ;;
    --backup-disk)     [[ $# -ge 2 ]] || err "--backup-disk needs a device or directory"
                       BACKUP_DISK="$2"; shift ;;
    --with-speedtest)  SPEEDTEST=yes ;;
    --no-speedtest)    SPEEDTEST=no ;;
    --accept-legal)    ACCEPT_LEGAL=true ;;
    *) err "Unknown option: $1 (see usage at the top of this script)" ;;
  esac
  shift
done

log "=== Emergency Communications Hub installer ==="
log "Target: $ECH_DIR"
log "Config: $ECH_CONFIG"

# ── Legal agreement (before anything is installed) ─────────────────────────
# Same terms as "Legal, licensing and interference" in README.md.
cat << 'EOF'

========================================================================
 LEGAL, LICENSING AND INTERFERENCE - read before you install
========================================================================
SignalMatrix can TRANSMIT: on amateur-radio frequencies (APRS, packet,
Winlink, DMR, M17, JS8Call, CW/digital modes via CAT control), on
licence-free ISM bands (MeshCore, Meshtastic, LoRa), and across a
telephone/VoIP gateway - and it can do so AUTOMATICALLY AND UNATTENDED
(bots, beacons, APRS objects, bridge rules, auto-acks, bulletins).

 * You, the operator and station licensee, are responsible for
   everything your station sends, including anything this software
   sends on your behalf. Nothing here is legal advice.
 * Interference: a misconfigured or runaway station can block other
   users, including emergency traffic. Use simulation mode or a dummy
   load first, start at low power, listen before transmitting, and
   never transmit where you are not authorised.
 * Penalties: in the US, violating FCC rules can bring fines (often
   thousands of dollars per violation) and licence revocation. "The
   software did it" is not a defence.
 * Amateur transmissions need a valid licence and your own call sign
   (47 CFR 97.119); automatic control, bridging and third-party traffic
   have their own rules (97.109, 97.113, 97.115, 97.221) - no business
   traffic and no encrypted content on amateur bands.
 * Licence-free LoRa radios must use the correct regional settings and
   accept interference. Phone gateways, Winlink, APRS-IS, DAPNET, DMR/
   M17 networks and data providers have their own terms - get your own
   accounts and keys and honour rate limits.
 * Receive-only data (AIS, ADS-B, GOES, satellites): never transmit on
   those services; do not decode, store or publish private traffic.
 * Messages, positions, logs and the config file are personal and
   secret data - protect them.
 * SignalMatrix is a coordination aid. It is NOT certified, is NOT a
   substitute for 911/EAS or official channels, and may fail, lose or
   delay messages. It is provided as is, without warranty of any kind.

Full text: README.md, section "Legal, licensing and interference".
========================================================================
EOF
if $ACCEPT_LEGAL; then
  log "Legal terms accepted (--accept-legal)"
elif [[ -t 0 ]]; then
  read -r -p "Type I AGREE to accept these terms and continue: " _agree
  _agree=$(echo "$_agree" | tr '[:lower:]' '[:upper:]' | xargs)
  [[ "$_agree" == "I AGREE" ]] || err "Terms not accepted - nothing was installed."
else
  err "No terminal to ask on: re-run with --accept-legal to agree to the terms above."
fi
LEGAL_ACCEPTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# ── Speed tests (optional, HIGH DATA USE) ──────────────────────────────────
if [[ -z "$SPEEDTEST" ]]; then
  if [[ -t 0 ]]; then
    cat << 'EOF'

ISP speed tests (optional)
  SignalMatrix can run an Ookla speed test plus traceroutes on a schedule
  and graph them on the Analytics page, to catch ISP slowdowns and routing
  changes.
  HIGH DATA USE: one test moves about 1.5 GB on a gigabit line. At the
  default of every 30 minutes that is about 70 GB a day / 2 TB a month,
  which can exceed a data cap or trigger overage charges on metered,
  cellular or satellite connections.
  Uses the official Ookla CLI; enabling it accepts Ookla's licence
  (personal, non-commercial use). You can change the interval or turn it
  off at any time in Settings -> Network path probe.
EOF
    read -r -p "Set up speed tests? [y/N]: " _st
    if [[ "$_st" =~ ^[Yy] ]]; then SPEEDTEST=yes; else SPEEDTEST=no; fi
  else
    SPEEDTEST=no
  fi
fi

# ── System packages ────────────────────────────────────────────────────────
log "Installing system packages…"
apt-get update -qq
apt-get install -y --no-install-recommends \
  python3 python3-pip python3-venv python3-dev \
  git curl ca-certificates \
  libusb-1.0-0 libudev-dev \
  usbutils \
  mosquitto mosquitto-clients \
  libportaudio2 \
  bluez 2>/dev/null || warn "bluez not available — BLE transports disabled"
# libportaudio2 above is required for the cw_audio/rtty_audio/psk31_audio
# adapters (sounddevice's runtime lib) — README documents it as a manual step
# for Debian/Raspberry Pi OS; installed automatically here so a from-scratch
# `install.sh` run doesn't silently miss it.

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
# Record the installer's legal acceptance (the web UI then won't ask again)
# and the speed-test choice.
"$ECH_DIR/.venv/bin/python" - "$ECH_CONFIG" "$LEGAL_ACCEPTED_AT" "$SPEEDTEST" << 'PYEOF'
# Text edits, not a YAML re-dump, so the config's comments survive.
import re, sys, yaml
path, accepted_at, speedtest = sys.argv[1:4]
s = open(path, encoding="utf-8").read()
if not re.search(r"^legal_accepted:", s, re.M):
    s = s.rstrip("\n") + f'\n\n# Legal / licensing / interference terms (README.md) accepted:\nlegal_accepted: "{accepted_at} (installer)"\n'
if speedtest == "yes":
    s = re.sub(r"^(network_probe:\n(?:[ \t]*#.*\n)*  enabled:) *false", r"\1 true", s, count=1, flags=re.M)
yaml.safe_load(s)   # still valid YAML
open(path, "w", encoding="utf-8").write(s)
PYEOF

# ── Ookla speedtest CLI (only if speed tests were chosen) ──────────────────
if [[ "$SPEEDTEST" == "yes" ]]; then
  _st_arch=$(uname -m)
  if [[ "$_st_arch" == "x86_64" || "$_st_arch" == "aarch64" ]]; then
    log "Installing Ookla speedtest CLI…"
    _st_tmp=$(mktemp -d)
    if curl -sSfL -o "$_st_tmp/st.tgz" "https://install.speedtest.net/app/cli/ookla-speedtest-1.2.0-linux-${_st_arch}.tgz" \
       && tar xzf "$_st_tmp/st.tgz" -C "$_st_tmp" speedtest; then
      install -o root -g root -m 0755 -D "$_st_tmp/speedtest" "$ECH_DIR/bin/speedtest"
      apt-get install -y --no-install-recommends traceroute >/dev/null 2>&1 || warn "traceroute install failed"
      log "Speed tests ON: every 30 min (~70 GB/day). Change in Settings -> Network path probe."
    else
      warn "speedtest CLI download failed - the network probe will run traceroutes only"
    fi
    rm -rf "$_st_tmp"
  else
    warn "No Ookla CLI build for $_st_arch - the network probe will run traceroutes only"
  fi
else
  log "Speed tests not set up (re-run with --with-speedtest to add them)"
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

# ── Avahi mDNS (optional: --with-mdns) ─────────────────────────────────────
if $WITH_MDNS; then
  apt-get install -y --no-install-recommends avahi-daemon avahi-utils \
    || warn "avahi install failed - mDNS skipped"
fi
if $WITH_MDNS && command -v avahi-daemon &>/dev/null; then
  cat > /etc/avahi/services/ech.service << 'EOF'
<?xml version="1.0" standalone='no'?>
<!DOCTYPE service-group SYSTEM "avahi-service.dtd">
<service-group>
  <name>Emergency Communications Hub</name>
  <service>
    <type>_https._tcp</type>
    <port>8765</port>
  </service>
</service-group>
EOF
  systemctl enable --now avahi-daemon 2>/dev/null || true
  systemctl restart avahi-daemon 2>/dev/null || true
  log "mDNS registered - accessible at https://$(hostname).local:8765"
else
  log "mDNS not installed (use --with-mdns to enable; ECH is reachable by IP)"
fi

# ── Prometheus (optional: --with-prometheus) ───────────────────────────────
# ECH serves /metrics itself; this installs a local Prometheus that scrapes it
# and keeps history. Nothing in ECH reads from Prometheus.
if $WITH_PROMETHEUS; then
  apt-get install -y --no-install-recommends prometheus \
    || warn "prometheus install failed - skipped"
  if [[ -d /etc/prometheus ]]; then
    cat > /etc/prometheus/prometheus.yml << 'EOF'
global:
  scrape_interval: 30s
  evaluation_interval: 30s
scrape_configs:
  - job_name: ech
    scheme: https
    tls_config:
      insecure_skip_verify: true   # ECH uses a self-signed certificate by default
    metrics_path: /metrics
    static_configs:
      - targets: ['127.0.0.1:8765']
EOF
    systemctl enable --now prometheus 2>/dev/null || true
    systemctl restart prometheus 2>/dev/null || true
    log "Prometheus installed - http://localhost:9090 (scrapes https://127.0.0.1:8765/metrics)"
  fi
else
  log "Prometheus not installed (use --with-prometheus to enable)"
fi

# ── Passwordless sudo for service restarts, reboot, and package updates ────
# The ECH web UI can restart individual services, reboot the host, and run
# apt-get update/upgrade from Settings. Allow the ech user to run only the
# specific commands it needs — nothing else.
log "Configuring passwordless sudo for service restarts, reboot, and updates…"
cat > /etc/sudoers.d/ech-services << 'EOF'
# Allow the ech service account to restart ECH-related services, reboot the
# host, and run apt-get update/upgrade — nothing else.
# Generated by ECH install.sh — edit with visudo /etc/sudoers.d/ech-services
ech ALL=(root) NOPASSWD: /bin/systemctl restart ech, \
                          /bin/systemctl restart pat, \
                          /bin/systemctl restart asterisk, \
                          /bin/systemctl restart mosquitto, \
                          /bin/systemctl restart prometheus, \
                          /bin/systemctl restart avahi-daemon, \
                          /bin/systemctl reboot, \
                          /usr/bin/systemctl restart ech, \
                          /usr/bin/systemctl restart pat, \
                          /usr/bin/systemctl restart asterisk, \
                          /usr/bin/systemctl restart mosquitto, \
                          /usr/bin/systemctl restart prometheus, \
                          /usr/bin/systemctl restart avahi-daemon, \
                          /usr/bin/systemctl reboot, \
                          /usr/bin/apt-get -y update, \
                          /usr/bin/apt-get -y upgrade
EOF
chmod 440 /etc/sudoers.d/ech-services
log "Sudoers entry written to /etc/sudoers.d/ech-services"

chown -R "$ECH_USER:$ECH_USER" "$ECH_DIR"

# ── Scheduled backups (optional: --backup-disk) ────────────────────────────
if [[ -n "$BACKUP_DISK" ]]; then
  log "Setting up nightly backups to $BACKUP_DISK…"
  bash "$ECH_DIR/scripts/setup_backup.sh" "$BACKUP_DISK"     || warn "backup setup failed - run: sudo bash $ECH_DIR/scripts/setup_backup.sh $BACKUP_DISK"
else
  log "Backups not configured (use --backup-disk /dev/sdX1 or a directory to enable)"
fi

echo ""
log "=== Installation complete ==="
log "UI: http://$(hostname -I | awk '{print $1}'):8765"
log "Config: $ECH_CONFIG"
log "Database: /var/lib/ech/ech.db"
warn "ECH can transmit, including automatically. You are responsible for licensing, station"
warn "identification and interference - see 'Legal, licensing and interference' in README.md."
if $USE_SYSTEMD && command -v systemctl &>/dev/null; then
  log "Start: systemctl start ech"
fi
