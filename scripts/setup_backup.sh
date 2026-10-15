#!/usr/bin/env bash
# Set up scheduled ECH backups onto a second disk / USB stick / directory.
# Run as root. Also called by install.sh --backup-disk.
#
# Usage: setup_backup.sh <block-device | directory> [keep-count]
#   setup_backup.sh /dev/sdb1            # existing filesystem on a USB stick
#   setup_backup.sh /srv/ech-backups     # any directory (NFS mount, etc.)
#
# Environment:
#   MOUNTPOINT   where a block device is mounted (default /mnt/ech-backup)
#
# What it does: mounts the device by UUID via /etc/fstab (nofail, so a missing
# stick never blocks boot), installs /usr/local/sbin/ech-backup, writes
# /etc/ech/backup.conf and enables a daily ech-backup.timer. It NEVER formats
# anything - a device with no filesystem is refused.

set -euo pipefail

TARGET="${1:-}"
KEEP="${2:-14}"
MOUNTPOINT="${MOUNTPOINT:-/mnt/ech-backup}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

die() { echo "setup_backup: $*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || die "run as root"
[[ -n "$TARGET" ]] || die "usage: $0 <block-device | directory> [keep-count]"
[[ "$KEEP" =~ ^[0-9]+$ && "$KEEP" -ge 1 ]] || die "keep-count must be a positive number"
[[ -f "$HERE/ech-backup.sh" ]] || die "ech-backup.sh not found next to this script"

REQUIRE_MOUNT=""
if [[ -b "$TARGET" ]]; then
  uuid="$(blkid -s UUID -o value "$TARGET" || true)"
  fstype="$(blkid -s TYPE -o value "$TARGET" || true)"
  [[ -n "$uuid" && -n "$fstype" ]] || die "$TARGET has no filesystem. Refusing to format it - create one first, e.g. mkfs.ext4 -L ech-backup $TARGET"
  case "$fstype" in
    swap|LVM2_member|crypto_LUKS|linux_raid_member) die "$TARGET is $fstype - not a backup disk" ;;
  esac
  mnt="$(findmnt -rn -S "$TARGET" -o TARGET | head -1 || true)"
  if [[ -z "$mnt" ]]; then
    mnt="$(findmnt --fstab -rn -S "UUID=$uuid" -o TARGET | head -1 || true)"
  fi
  case "$mnt" in
    /|/boot|/boot/*|/usr|/var|/home) die "$TARGET is the system disk ($mnt) - pick a different device or a directory" ;;
  esac
  if [[ -z "$mnt" ]]; then
    mnt="$MOUNTPOINT"
    mkdir -p "$mnt"
    echo "UUID=$uuid $mnt $fstype defaults,nofail,x-systemd.device-timeout=5 0 2" >> /etc/fstab
    echo "setup_backup: added $mnt ($fstype, UUID=$uuid) to /etc/fstab"
    systemctl daemon-reload 2>/dev/null || true
  fi
  mountpoint -q "$mnt" || mount "$mnt"
  BACKUP_MOUNT="$mnt"
  BACKUP_DIR="$mnt/ech-backups"
else
  mkdir -p "$TARGET"
  BACKUP_MOUNT=""
  BACKUP_DIR="$TARGET"
fi

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

install -m 755 "$HERE/ech-backup.sh" /usr/local/sbin/ech-backup
mkdir -p /etc/ech
cat > /etc/ech/backup.conf <<EOF
# Written by scripts/setup_backup.sh - see /usr/local/sbin/ech-backup
BACKUP_DIR="$BACKUP_DIR"
BACKUP_MOUNT="$BACKUP_MOUNT"
BACKUP_KEEP=$KEEP
EOF
chmod 600 /etc/ech/backup.conf

if command -v systemctl >/dev/null 2>&1; then
  cat > /etc/systemd/system/ech-backup.service <<'EOF'
[Unit]
Description=ECH database + config backup

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/ech-backup
Nice=10
IOSchedulingClass=idle
EOF
  cat > /etc/systemd/system/ech-backup.timer <<'EOF'
[Unit]
Description=Daily ECH backup

[Timer]
OnCalendar=*-*-* 03:30:00
RandomizedDelaySec=15min
Persistent=true

[Install]
WantedBy=timers.target
EOF
  systemctl daemon-reload
  systemctl enable --now ech-backup.timer
  echo "setup_backup: daily timer enabled (03:30), keeping $KEEP archives in $BACKUP_DIR"
  echo "setup_backup: running a first backup now..."
  systemctl start ech-backup.service
  journalctl -u ech-backup.service -n 8 --no-pager 2>/dev/null | sed 's/^/  /' || true
else
  echo "setup_backup: no systemd - schedule /usr/local/sbin/ech-backup from cron"
fi
