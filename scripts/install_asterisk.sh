#!/usr/bin/env bash
# ECH Asterisk installer for Debian 12 (Bookworm)
# Builds Asterisk 20 LTS from source — the only reliable path on Debian 12
# since Asterisk was removed from official Debian repos.
#
# Build time: ~20-30 min on AMD G-T48E (2 cores). Go get coffee.
# Memory needed: ~512MB during compile — fine on 1.5GB machine.
# Disk needed: ~500MB during build, ~150MB installed.
#
# What you get:
#   - Asterisk 20 LTS (long term support through 2027)
#   - SIP/PJSIP on port 5060
#   - AMI on port 5038 (localhost, for ECH integration)
#   - Extensions 101-110, conference room 900, page-all 9999
#   - ConfBridge (no DAHDI required)
#
# Usage: sudo bash scripts/install_asterisk.sh

set -euo pipefail
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
log()  { echo -e "${GREEN}[AST]${NC} $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
err()  { echo -e "${RED}[ERR]${NC} $*"; exit 1; }

[[ $EUID -ne 0 ]] && err "Run as root: sudo bash scripts/install_asterisk.sh"

# ── Already installed? ────────────────────────────────────────────────────
if command -v asterisk &>/dev/null; then
    EXISTING=$(asterisk -V 2>/dev/null || echo "unknown")
    warn "Asterisk already installed: $EXISTING"
    read -p "Reinstall/upgrade? [y/N]: " REPLY
    [[ "${REPLY:-n}" =~ ^[Yy]$ ]] || exit 0
fi

# ── Callsign ──────────────────────────────────────────────────────────────
read -p "Your callsign (for Asterisk config): " CALLSIGN
CALLSIGN=${CALLSIGN:-KN0O}
CALLSIGN_UPPER=$(echo "$CALLSIGN" | tr '[:lower:]' '[:upper:]')
log "Configuring for callsign: $CALLSIGN_UPPER"

# ── Build dependencies ────────────────────────────────────────────────────
log "Installing build dependencies (this may take a few minutes)..."
apt-get update -qq
apt-get install -y --no-install-recommends \
    build-essential wget curl git \
    libssl-dev libncurses5-dev libnewt-dev libxml2-dev \
    libsqlite3-dev uuid-dev libjansson-dev \
    pkg-config libedit-dev \
    sox mpg123 \
    libasound2-dev \
    2>/dev/null

# ── Download Asterisk 20 LTS ──────────────────────────────────────────────
AST_VER="20"
BUILD_DIR="/usr/src/asterisk-build"
mkdir -p "$BUILD_DIR"
cd "$BUILD_DIR"

log "Downloading Asterisk $AST_VER LTS source..."
# Try latest, fall back to known good version
if ! wget -q --show-progress \
    "https://downloads.asterisk.org/pub/telephony/asterisk/asterisk-${AST_VER}-current.tar.gz" \
    -O "asterisk-${AST_VER}.tar.gz" 2>/dev/null; then
    log "Trying direct version download..."
    wget -q --show-progress \
        "https://downloads.asterisk.org/pub/telephony/asterisk/asterisk-20.12.0.tar.gz" \
        -O "asterisk-${AST_VER}.tar.gz" || \
    err "Could not download Asterisk source. Check internet connection."
fi

log "Extracting..."
tar -xzf "asterisk-${AST_VER}.tar.gz"
cd asterisk-${AST_VER}*/

# ── Install prereqs via Asterisk script ──────────────────────────────────
log "Running Asterisk prerequisite script..."
contrib/scripts/install_prereq install 2>/dev/null || \
    warn "Some prereqs may be missing — continuing anyway"

# ── Configure ─────────────────────────────────────────────────────────────
log "Configuring Asterisk (minimal build - no GUI, no DAHDI)..."
./configure \
    --with-jansson-bundled \
    --disable-xmldoc \
    --without-dahdi \
    --without-pri \
    --without-misdn \
    --without-ss7 \
    --without-openr2 \
    2>/dev/null | tail -5

# ── Select modules ────────────────────────────────────────────────────────
log "Selecting modules for minimal ARES/EOC build..."
# Start with nothing, add only what we need
make menuselect.makeopts 2>/dev/null || true

menuselect/menuselect \
    --disable-category MENUSELECT_ADDONS \
    --disable-category MENUSELECT_CORE_SOUNDS \
    --disable-category MENUSELECT_MOH \
    --disable-category MENUSELECT_EXTRA_SOUNDS \
    --enable chan_sip \
    --enable chan_pjsip \
    --enable pbx_config \
    --enable app_dial \
    --enable app_voicemail \
    --enable app_confbridge \
    --enable app_playback \
    --enable app_echo \
    --enable app_page \
    --enable app_saytime \
    --enable app_sayunixtime \
    --enable app_read \
    --enable res_pjsip \
    --enable res_pjsip_session \
    --enable res_pjsip_authenticator_digest \
    --enable res_pjsip_endpoint_identifier_ip \
    --enable res_pjsip_registrar \
    --enable res_pjsip_outbound_registration \
    --enable res_pjsip_sdp_rtp \
    --enable res_rtp_asterisk \
    --enable codec_ulaw \
    --enable codec_alaw \
    --enable codec_gsm \
    --enable format_wav \
    --enable format_gsm \
    --enable cdr_csv \
    --enable manager \
    --enable CORE-SOUNDS-EN-ULAW \
    menuselect.makeopts 2>/dev/null || warn "menuselect issue — using defaults"

# ── Compile ───────────────────────────────────────────────────────────────
NCPU=$(nproc)
log "Compiling Asterisk with $NCPU cores (this takes ~20-30 min on this hardware)..."
log "You can watch progress — it will print lots of output..."
make -j${NCPU} 2>&1 | grep -E "^(CC|LD|Building|error:|warning: )" || true
make -j${NCPU} || err "Compilation failed. Check output above."

log "Installing..."
make install 2>/dev/null
make samples 2>/dev/null || true  # installs sample configs if /etc/asterisk is empty

log "Setting up init scripts..."
make config 2>/dev/null || true
ldconfig

# ── Asterisk user ─────────────────────────────────────────────────────────
id asterisk &>/dev/null || useradd --system --home /var/lib/asterisk --shell /usr/sbin/nologin asterisk
for dir in /var/lib/asterisk /var/log/asterisk /var/spool/asterisk /var/run/asterisk /etc/asterisk; do
    [[ -d "$dir" ]] && chown -R asterisk:asterisk "$dir" 2>/dev/null || true
done

# ── Write ECH configs ─────────────────────────────────────────────────────
CONF="/etc/asterisk"
log "Writing ECH/ARES configuration..."

cat > "$CONF/asterisk.conf" << EOF
[options]
verbose = 3
debug = 0
runuser = asterisk
rungroup = asterisk
defaultlanguage = en
EOF

cat > "$CONF/modules.conf" << 'EOF'
[modules]
autoload = yes
noload = chan_dahdi.so
noload = res_timing_dahdi.so
noload = app_meetme.so
noload = res_adsi.so
noload = chan_mobile.so
noload = chan_ooh323.so
noload = res_hep.so
noload = res_hep_rtcp.so
noload = res_hep_pjsip.so
EOF

cat > "$CONF/sip.conf" << SIPEOF
[general]
context = from-internal
allowoverlap = no
udpbindaddr = 0.0.0.0
tcpenable = yes
tcpbindaddr = 0.0.0.0
transport = udp,tcp
srvlookup = yes
qualify = yes
nat = force_rport,comedia
directmedia = no
dtmfmode = rfc2833
disallow = all
allow = ulaw
allow = alaw

[101]
type=friend;context=from-internal;host=dynamic;secret=101change
callerid="EOC Position 1" <101>;mailbox=101@default;qualify=yes

[102]
type=friend;context=from-internal;host=dynamic;secret=102change
callerid="EOC Position 2" <102>;mailbox=102@default;qualify=yes

[103]
type=friend;context=from-internal;host=dynamic;secret=103change
callerid="Net Control" <103>;mailbox=103@default;qualify=yes

[104]
type=friend;context=from-internal;host=dynamic;secret=104change
callerid="Logistics" <104>;mailbox=104@default;qualify=yes

[105]
type=friend;context=from-internal;host=dynamic;secret=105change
callerid="Operations" <105>;mailbox=105@default;qualify=yes

[106]
type=friend;context=from-internal;host=dynamic;secret=106change
callerid="Planning" <106>;mailbox=106@default;qualify=yes

[107]
type=friend;context=from-internal;host=dynamic;secret=107change
callerid="Safety" <107>;mailbox=107@default;qualify=yes

[108]
type=friend;context=from-internal;host=dynamic;secret=108change
callerid="Liaison" <108>;mailbox=108@default;qualify=yes

[109]
type=friend;context=from-internal;host=dynamic;secret=109change
callerid="Field 1" <109>;mailbox=109@default;qualify=yes

[110]
type=friend;context=from-internal;host=dynamic;secret=110change
callerid="Field 2" <110>;mailbox=110@default;qualify=yes
SIPEOF

cat > "$CONF/extensions.conf" << 'EXTEOF'
[from-internal]
; Direct extensions
exten => 101,1,Dial(SIP/101,30,rTt)
 same => n,Voicemail(101@default,u)
 same => n,Hangup()

exten => 102,1,Dial(SIP/102,30,rTt)
 same => n,Voicemail(102@default,u)
 same => n,Hangup()

exten => 103,1,Dial(SIP/103,30,rTt)
 same => n,Voicemail(103@default,u)
 same => n,Hangup()

exten => 104,1,Dial(SIP/104,30,rTt)
 same => n,Voicemail(104@default,u)
 same => n,Hangup()

exten => 105,1,Dial(SIP/105,30,rTt)
 same => n,Voicemail(105@default,u)
 same => n,Hangup()

exten => 106,1,Dial(SIP/106,30,rTt)
 same => n,Voicemail(106@default,u)
 same => n,Hangup()

exten => 107,1,Dial(SIP/107,30,rTt)
 same => n,Voicemail(107@default,u)
 same => n,Hangup()

exten => 108,1,Dial(SIP/108,30,rTt)
 same => n,Voicemail(108@default,u)
 same => n,Hangup()

exten => 109,1,Dial(SIP/109,30,rTt)
 same => n,Voicemail(109@default,u)
 same => n,Hangup()

exten => 110,1,Dial(SIP/110,30,rTt)
 same => n,Voicemail(110@default,u)
 same => n,Hangup()

; Conference room 900 (ConfBridge - no DAHDI needed)
exten => 900,1,Answer()
 same => n,ConfBridge(900,default_bridge,default_user)
 same => n,Hangup()

; Page all stations
exten => 9999,1,Page(SIP/101&SIP/102&SIP/103&SIP/104&SIP/105,d)
 same => n,Hangup()

; Voicemail retrieval
exten => *98,1,VoiceMailMain(${CALLERID(num)}@default)
 same => n,Hangup()

; Echo test
exten => *43,1,Answer()
 same => n,Echo()
 same => n,Hangup()

; Time
exten => *86,1,Answer()
 same => n,SayUnixTime(,UTC,HNS)
 same => n,Hangup()
EXTEOF

cat > "$CONF/manager.conf" << 'EOF'
[general]
enabled = yes
port = 5038
bindaddr = 127.0.0.1
displayconnects = yes

[ech]
secret = ech_ami_secret_change_me
read = system,call,log,verbose,agent,user,config,dtmf,reporting,cdr,dialplan,originate
write = system,call,originate
permit = 127.0.0.1/255.255.255.255
EOF

cat > "$CONF/voicemail.conf" << 'EOF'
[general]
format = wav49|gsm|wav
maxmessage = 180
attach = yes
maxlogins = 3

[default]
101 => 1234,EOC Position 1,
102 => 1234,EOC Position 2,
103 => 1234,Net Control,
104 => 1234,Logistics,
105 => 1234,Operations,
106 => 1234,Planning,
107 => 1234,Safety,
108 => 1234,Liaison,
109 => 1234,Field 1,
110 => 1234,Field 2,
EOF

cat > "$CONF/confbridge.conf" << 'EOF'
[general]

[default_user]
type = user
announce_join_leave = yes
music_on_hold_when_empty = yes

[default_bridge]
type = bridge
max_members = 50
EOF

# Fix permissions
chown -R asterisk:asterisk "$CONF" 2>/dev/null || true

# ── Systemd ───────────────────────────────────────────────────────────────
# make config should have created init scripts; enable the systemd unit
if [[ -f /etc/init.d/asterisk ]]; then
    systemctl daemon-reload
fi
systemctl enable asterisk 2>/dev/null || true
systemctl restart asterisk

sleep 3
if systemctl is-active asterisk &>/dev/null; then
    log "✓ Asterisk is running: $(asterisk -V)"
else
    warn "Asterisk may not have started cleanly."
    warn "Check: sudo journalctl -u asterisk -n 30 --no-pager"
fi

# ── Update ECH config ────────────────────────────────────────────────────
ECH_CONFIG="/etc/ech/config.yaml"
if [[ -f "$ECH_CONFIG" ]] && ! grep -q "aredn_ami\|asterisk-pbx" "$ECH_CONFIG"; then
    log "Adding Asterisk AMI adapter to ECH config..."
    cat >> "$ECH_CONFIG" << ECHEOF

  - type: aredn_ami
    name: asterisk-pbx
    host: 127.0.0.1
    port: 5038
    username: ech
    secret: ech_ami_secret_change_me
ECHEOF
    log "Restart ECH to activate: sudo systemctl restart ech"
fi

# ── Cleanup ───────────────────────────────────────────────────────────────
cd /
rm -rf "$BUILD_DIR"
log "Build directory cleaned up"

IP=$(hostname -I | awk '{print $1}')
echo ""
log "=== Asterisk installed for $CALLSIGN_UPPER ==="
log "Version: $(asterisk -V)"
log "SIP: ${IP}:5060   AMI: 127.0.0.1:5038"
log ""
log "Extensions 101-110  |  Conference: 900  |  Page all: 9999"
log "Voicemail PIN: 1234 (change in $CONF/voicemail.conf)"
echo ""
warn "CHANGE THESE before going live:"
warn "  SIP passwords in $CONF/sip.conf  (currently 101change, 102change, etc.)"
warn "  AMI secret in $CONF/manager.conf AND /etc/ech/config.yaml"
echo ""
log "Softphone: install Linphone on your phone/laptop"
log "  Server: ${IP}   Username: 101   Password: 101change"
log ""
log "Live console: sudo asterisk -rvvv"
log "  sip show peers    — who's registered"
log "  core show uptime  — uptime"
log "  exit              — quit (doesn't stop Asterisk)"
