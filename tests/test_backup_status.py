"""
tests/test_backup_status.py
----------------------------
The nav backup warning (ech/core/backup_status.py): flags an unmounted
backup drive, a failed or stale last run, and a timer that stopped firing —
the 2026-10-23 USB-stick fsck failure went six nights unnoticed.
"""

from ech.core import backup_status as bs

NOW = 1_800_000_000.0
CONF = {"BACKUP_DIR": "/mnt/usb/ech-backups", "BACKUP_MOUNT": "/mnt/usb", "BACKUP_KEEP": "14"}


def _ago(hours: float) -> str:
    return f"@{int(NOW - hours * 3600)}"


def test_not_configured_is_ok():
    s = bs.evaluate(None, None, {}, {}, now=NOW)
    assert s == {"configured": False, "ok": True, "problems": []}


def test_healthy():
    s = bs.evaluate(CONF, True, {"Result": "success", "ExecMainExitTimestamp": _ago(20)},
                    {"LastTriggerUSec": _ago(20)}, now=NOW)
    assert s["ok"] and s["problems"] == []


def test_unmounted_drive_and_failed_run():
    # Exactly the 2026-10-24..29 state: fsck failed at boot, script refused to run.
    s = bs.evaluate(CONF, False, {"Result": "exit-code", "ExecMainExitTimestamp": _ago(20)},
                    {"LastTriggerUSec": _ago(20)}, now=NOW)
    assert not s["ok"]
    assert s["problems"] == ["backup drive /mnt/usb is not mounted", "last backup run failed (exit-code)"]


def test_stale_success():
    s = bs.evaluate(CONF, True, {"Result": "success", "ExecMainExitTimestamp": _ago(50)},
                    {"LastTriggerUSec": _ago(50)}, now=NOW)
    assert s["problems"] == ["last backup ran 50 h ago"]


def test_after_reboot_uses_timer_trigger():
    # Service stamps reset on reboot ("n/a"); the persistent timer's trigger remains.
    fresh = bs.evaluate(CONF, True, {"Result": "success", "ExecMainExitTimestamp": ""},
                        {"LastTriggerUSec": _ago(10)}, now=NOW)
    stale = bs.evaluate(CONF, True, {"Result": "success", "ExecMainExitTimestamp": ""},
                        {"LastTriggerUSec": _ago(72)}, now=NOW)
    assert fresh["ok"]
    assert stale["problems"] == ["backup timer last fired 72 h ago"]


def test_timer_local_time_format():
    # LastTriggerUSec ignores --timestamp=unix and prints local time.
    import time
    local = time.strftime("%a %Y-%m-%d %H:%M:%S EDT", time.localtime(NOW - 72 * 3600))
    s = bs.evaluate(CONF, True, {"Result": "success", "ExecMainExitTimestamp": ""},
                    {"LastTriggerUSec": local}, now=NOW)
    assert s["problems"] == ["backup timer last fired 72 h ago"]
    assert bs._unix("n/a") is None and bs._unix("") is None


def test_read_conf(tmp_path):
    p = tmp_path / "backup.conf"
    p.write_text('# Written by scripts/setup_backup.sh\nBACKUP_DIR="/mnt/usb/ech-backups"\n'
                 'BACKUP_MOUNT="/mnt/usb"\nBACKUP_KEEP=14\n')
    assert bs.read_conf(str(p)) == CONF
    assert bs.read_conf(str(tmp_path / "missing.conf")) is None
