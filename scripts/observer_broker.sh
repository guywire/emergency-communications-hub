#!/usr/bin/env bash
# SignalMatrix observer broker management (O118)
#
#   sudo scripts/observer_broker.sh install            # set up broker config + SignalMatrix reader login
#   sudo scripts/observer_broker.sh add <PUBKEY> [REGION]   # create a login for one observer
#   sudo scripts/observer_broker.sh revoke <PUBKEY>    # remove an observer's login
#   sudo scripts/observer_broker.sh list               # list observers
#
# Observers log in with their MeshCore public key (64 hex chars) as the
# username and may only publish under meshcore/<REGION>/<their key>/…
# (enforced by deploy/mosquitto/signalmatrix.acl). Installing does NOT expose
# anything to the internet — see the external listener notes in
# deploy/mosquitto/signalmatrix-observers.conf.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONF_SRC="$HERE/deploy/mosquitto/signalmatrix-observers.conf"
ACL_SRC="$HERE/deploy/mosquitto/signalmatrix.acl"
CONF_DST="/etc/mosquitto/conf.d/signalmatrix-observers.conf"
ACL_DST="/etc/mosquitto/signalmatrix.acl"
PASSWD="/etc/mosquitto/signalmatrix_passwd"
READER="signalmatrix"

die() { echo "observer_broker: $*" >&2; exit 1; }
need_root() { [[ $EUID -eq 0 ]] || die "run with sudo"; }
newpass() { openssl rand -base64 18 | tr -d '/+=' | cut -c1-20; }
reload() { systemctl reload mosquitto 2>/dev/null || systemctl restart mosquitto; }
normkey() {
  local k="${1:-}"
  [[ "$k" =~ ^[0-9A-Fa-f]{64}$ ]] || die "public key must be 64 hex characters (got '${k:0:20}…')"
  echo "${k^^}"
}

cmd="${1:-}"
case "$cmd" in
  install)
    need_root
    command -v mosquitto_passwd >/dev/null || die "install mosquitto first: sudo apt install mosquitto mosquitto-clients"
    install -m 644 "$CONF_SRC" "$CONF_DST"
    install -m 644 "$ACL_SRC" "$ACL_DST"
    if [[ ! -f "$PASSWD" ]] || ! grep -q "^$READER:" "$PASSWD"; then
      pw="$(newpass)"
      touch "$PASSWD"
      mosquitto_passwd -b "$PASSWD" "$READER" "$pw"
      new_reader=1
    fi
    chown mosquitto:mosquitto "$PASSWD" 2>/dev/null || true
    chmod 640 "$PASSWD"
    systemctl restart mosquitto
    echo "Observer broker installed (localhost only: observers on 127.0.0.1:1884)."
    if [[ "${new_reader:-}" ]]; then
      cat <<EOF

Add this adapter to /etc/ech/config.yaml so SignalMatrix compares against YOUR
observers (alongside LetsMesh if you have it), then restart SignalMatrix:

  - type: letsmesh_compare
    name: observers-compare
    host: 127.0.0.1
    port: 1884
    username: $READER
    password: "$pw"
    meshcore_adapter: meshcore-usb      # your local MeshCore adapter name
    topics:
      - meshcore/+/+/packets

(The password is shown once — it is stored only as a hash.)
EOF
    fi
    ;;
  add)
    need_root
    key="$(normkey "${2:-}")"
    region="${3:-REGION}"
    [[ -f "$PASSWD" ]] || die "run '$0 install' first"
    grep -q "^$key:" "$PASSWD" && die "observer already exists (revoke first to reissue)"
    pw="$(newpass)"
    mosquitto_passwd -b "$PASSWD" "$key" "$pw"
    reload
    cat <<EOF
Observer added. Send the observer's operator:

  broker:    <your public host name>  (port 8883 TLS, or wss://<host>/mqtt — whichever you enabled)
  username:  $key
  password:  $pw
  topic:     meshcore/$region/$key/packets   (same layout as LetsMesh)

They can only publish under their own key, and cannot read anyone's reports.
EOF
    ;;
  revoke)
    need_root
    key="$(normkey "${2:-}")"
    grep -q "^$key:" "$PASSWD" 2>/dev/null || die "no such observer"
    mosquitto_passwd -D "$PASSWD" "$key"
    reload
    echo "Observer ${key:0:8}… revoked."
    ;;
  list)
    need_root
    [[ -f "$PASSWD" ]] || die "not installed"
    cut -d: -f1 "$PASSWD" | grep -v "^$READER$" || echo "(no observers yet)"
    ;;
  *)
    sed -n '2,13p' "$0"; exit 1 ;;
esac
