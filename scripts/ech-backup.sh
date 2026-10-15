#!/usr/bin/env bash
# ECH backup - snapshot the database + configuration into one tar.gz and prune
# old ones. Installed to /usr/local/sbin/ech-backup by scripts/setup_backup.sh
# and run daily by ech-backup.timer. Safe to run by hand at any time.
#
# Settings come from /etc/ech/backup.conf (written by setup_backup.sh):
#   BACKUP_DIR            where archives are written
#   BACKUP_MOUNT          mount point that must be mounted (stops a missing USB
#                         stick from silently filling the root disk); empty = no check
#   BACKUP_KEEP           how many archives to keep (default 14)
#   BACKUP_DB             space-separated DB paths (default: the usual ech.db spots)

set -euo pipefail

CONF="${ECH_BACKUP_CONF:-/etc/ech/backup.conf}"
# shellcheck disable=SC1090
[[ -f "$CONF" ]] && source "$CONF"

BACKUP_DIR="${BACKUP_DIR:?BACKUP_DIR not set - run scripts/setup_backup.sh}"
BACKUP_KEEP="${BACKUP_KEEP:-14}"
BACKUP_MOUNT="${BACKUP_MOUNT:-}"
BACKUP_DB="${BACKUP_DB:-/opt/ech/ech.db /var/lib/ech/ech.db}"

if [[ -n "$BACKUP_MOUNT" ]] && ! mountpoint -q "$BACKUP_MOUNT"; then
  echo "ech-backup: $BACKUP_MOUNT is not mounted - skipping (not writing to the root disk)" >&2
  exit 1
fi

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"
stamp="$(date +%Y%m%d-%H%M%S)"
stage="$(mktemp -d "$BACKUP_DIR/.stage.XXXXXX")"
trap 'rm -rf "$stage"' EXIT

mkdir -p "$stage/ech-backup-$stamp"
snap="$stage/ech-backup-$stamp"

# Consistent online snapshot of each SQLite DB (a plain cp of a live WAL
# database can be torn).
for db in $BACKUP_DB; do
  [[ -f "$db" ]] || continue
  python3 - "$db" "$snap/$(basename "$db")" <<'PY'
import sqlite3, sys
src = sqlite3.connect("file:%s?mode=ro" % sys.argv[1], uri=True)
dst = sqlite3.connect(sys.argv[2])
with dst:
    src.backup(dst)
dst.close(); src.close()
PY
  echo "ech-backup: snapshot $db"
done

# Configuration (contains secrets - the archive is mode 600 in a 700 dir)
[[ -d /etc/ech ]]      && cp -a /etc/ech      "$snap/etc-ech"
[[ -d /etc/asterisk ]] && cp -a /etc/asterisk "$snap/etc-asterisk"
for d in $BACKUP_DB; do
  a="$(dirname "$d")/message_archive"
  [[ -d "$a" ]] && { cp -a "$a" "$snap/message_archive"; break; }
done

out="$BACKUP_DIR/ech-backup-$stamp.tar.gz"
tar -C "$stage" -czf "$out.partial" "ech-backup-$stamp"
chmod 600 "$out.partial"
mv "$out.partial" "$out"
echo "ech-backup: wrote $out ($(du -h "$out" | cut -f1))"

# Prune: keep the newest BACKUP_KEEP archives
ls -1t "$BACKUP_DIR"/ech-backup-*.tar.gz 2>/dev/null | tail -n +"$((BACKUP_KEEP + 1))" | while read -r old; do
  rm -f -- "$old"
  echo "ech-backup: pruned $old"
done
