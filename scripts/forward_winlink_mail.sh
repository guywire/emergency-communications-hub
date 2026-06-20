#!/usr/bin/env bash
# /opt/ech/scripts/forward_winlink_mail.sh
#
# Pat post-session hook: forwards new Winlink messages to personal email.
# Pat runs this script after each successful connect session.
#
# Setup:
#   1. Install msmtp (lightweight SMTP client):
#        sudo apt install msmtp msmtp-mta -y
#
#   2. Configure msmtp for your email (~/.msmtprc or /etc/msmtprc):
#        account default
#        host smtp.gmail.com        # or your provider
#        port 587
#        tls on
#        tls_starttls on
#        auth on
#        user your@gmail.com
#        password yourapppassword   # use an App Password for Gmail
#        from your@gmail.com
#        logfile /var/log/msmtp.log
#
#   3. Add this hook to Pat's config (~/.config/pat/config.json):
#        "on_successful_session": ["/opt/ech/scripts/forward_winlink_mail.sh"]
#
#   4. Set FORWARD_TO below and make this script executable:
#        chmod +x /opt/ech/scripts/forward_winlink_mail.sh
#
# The script tracks which messages have been forwarded using a state file,
# so it only sends each message once even if Pat is connected multiple times.

set -euo pipefail

# ── Configuration ─────────────────────────────────────────────────────────
FORWARD_TO="${WINLINK_FORWARD_TO:-your@email.com}"   # set this or export env var
CALLSIGN="${PAT_CALLSIGN:-$(grep -r '"mycall"' ~/.config/pat/config.json 2>/dev/null | head -1 | sed 's/.*"mycall": *"\([^"]*\)".*/\1/' || echo 'KN0O')}"
MAILBOX_IN="${HOME}/.local/share/pat/mailbox/${CALLSIGN}/in"
STATE_FILE="${HOME}/.local/share/pat/forwarded_mids"
LOG="/var/log/ech/winlink_forward.log"
FROM_ADDR="${CALLSIGN}@winlink.org"

# ── Setup ─────────────────────────────────────────────────────────────────
mkdir -p "$(dirname "$LOG")" 2>/dev/null || true
touch "$STATE_FILE" 2>/dev/null || STATE_FILE="/tmp/forwarded_mids_${CALLSIGN}"
touch "$STATE_FILE"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG"; }

# ── Check deps ────────────────────────────────────────────────────────────
if ! command -v msmtp &>/dev/null && ! command -v sendmail &>/dev/null; then
  log "ERROR: No MTA found. Install msmtp: sudo apt install msmtp msmtp-mta -y"
  exit 0   # exit 0 so Pat doesn't treat this as a failure
fi

if [[ ! -d "$MAILBOX_IN" ]]; then
  log "No inbox directory found at $MAILBOX_IN"
  exit 0
fi

# ── Process each message in the inbox ─────────────────────────────────────
forwarded=0
failed=0

for msgfile in "$MAILBOX_IN"/*.b2f "$MAILBOX_IN"/*.txt 2>/dev/null; do
  [[ -f "$msgfile" ]] || continue

  # Extract message ID from filename
  mid="$(basename "$msgfile" .b2f)"
  mid="${mid%.txt}"

  # Skip if already forwarded
  if grep -qx "$mid" "$STATE_FILE" 2>/dev/null; then
    continue
  fi

  # Parse message headers and body
  subject="$(grep -i '^Subject:' "$msgfile" | head -1 | sed 's/^[Ss]ubject: *//' | tr -d '\r')"
  from_addr="$(grep -i '^From:' "$msgfile" | head -1 | sed 's/^[Ff]rom: *//' | tr -d '\r')"
  date_hdr="$(grep -i '^Date:' "$msgfile" | head -1 | sed 's/^[Dd]ate: *//' | tr -d '\r')"

  # Body starts after blank line
  body="$(awk '/^$/{found=1;next} found{print}' "$msgfile" | head -100)"

  # Build and send email via msmtp/sendmail
  {
    echo "To: ${FORWARD_TO}"
    echo "From: Winlink <${FROM_ADDR}>"
    echo "Subject: [Winlink ${CALLSIGN}] ${subject:-No subject}"
    echo "Date: ${date_hdr:-$(date -R)}"
    echo "X-Winlink-MID: ${mid}"
    echo "X-Winlink-From: ${from_addr}"
    echo ""
    echo "--- Winlink message forwarded by ECH ---"
    echo "From:    ${from_addr}"
    echo "Subject: ${subject}"
    echo "MID:     ${mid}"
    echo ""
    echo "${body}"
    echo ""
    echo "--- End of Winlink message ---"
    echo "Forwarded from ${CALLSIGN} via ECH (Emergency Communications Hub)"
  } | if command -v msmtp &>/dev/null; then
    msmtp --read-envelope-from -t 2>> "$LOG"
  else
    sendmail -t 2>> "$LOG"
  fi

  if [[ $? -eq 0 ]]; then
    echo "$mid" >> "$STATE_FILE"
    log "Forwarded MID ${mid} (${subject:-no subject}) from ${from_addr} to ${FORWARD_TO}"
    ((forwarded++)) || true
  else
    log "FAILED to forward MID ${mid}"
    ((failed++)) || true
  fi
done

log "Session complete: ${forwarded} forwarded, ${failed} failed"

# ── Trim state file to last 1000 entries ─────────────────────────────────
if [[ $(wc -l < "$STATE_FILE") -gt 1000 ]]; then
  tail -1000 "$STATE_FILE" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "$STATE_FILE"
fi

exit 0
