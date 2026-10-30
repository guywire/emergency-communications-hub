"""
ech/core/backup_status.py
--------------------------
Is the nightly backup (scripts/ech-backup.sh, run as root by ech-backup.timer)
actually working? Answered without root: the archive directory is mode 700,
so this reads only what the ech user can see -

  /etc/ech/backup.conf        backups configured at all, and which mount they need
                              (paths only; setup_backup.sh writes it 644)
  os.path.ismount()           is that drive mounted
  systemctl show ...          last run's result/time and the timer's last trigger

Added after the USB stick failed its boot fsck on 2026-10-23 and backups were
skipped for six nights with nothing in ECH saying so.
"""

from __future__ import annotations

import asyncio
import os
import re
import shlex
import time

CONF_PATH = "/etc/ech/backup.conf"
MAX_AGE_HOURS = 36   # daily timer + slack for a late or missed run

_cache: tuple[float, dict] | None = None
_CACHE_SECS = 600


def read_conf(path: str = CONF_PATH) -> dict[str, str] | None:
    """KEY="value" lines from backup.conf, or None if backups aren't set up."""
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return None
    conf: dict[str, str] = {}
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        try:
            parts = shlex.split(val)
        except ValueError:
            continue
        conf[key.strip()] = parts[0] if parts else ""
    return conf


_LOCAL_TS = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


def _unix(val: str) -> float | None:
    """systemctl --timestamp=unix gives '@1761794259' for most timestamps, but
    usec properties like the timer's LastTriggerUSec ignore the flag and print
    local time ('Thu 2026-10-29 03:40:59 EDT') — systemctl runs on this same
    host, so local time is ours too. Empty/'n/a' = never."""
    val = (val or "").strip()
    if val.startswith("@"):
        try:
            return float(val[1:])
        except ValueError:
            return None
    m = _LOCAL_TS.search(val)
    if m:
        return time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))
    return None


def evaluate(conf: dict[str, str] | None, mounted: bool | None, service: dict[str, str],
             timer: dict[str, str], now: float | None = None) -> dict:
    """Turn the raw facts into {configured, ok, problems, ...}. Pure, for tests."""
    if conf is None:
        return {"configured": False, "ok": True, "problems": []}
    now = time.time() if now is None else now
    problems: list[str] = []
    mount = conf.get("BACKUP_MOUNT", "")
    if mount and mounted is False:
        problems.append(f"backup drive {mount} is not mounted")

    last_exit = _unix(service.get("ExecMainExitTimestamp", ""))
    result = service.get("Result", "")
    if last_exit and result and result != "success":
        problems.append(f"last backup run failed ({result})")
    elif last_exit and now - last_exit > MAX_AGE_HOURS * 3600:
        problems.append(f"last backup ran {int((now - last_exit) // 3600)} h ago")

    # The service's own stamps reset on reboot; the (persistent) timer's last
    # trigger survives it, so it is what catches a timer that stopped firing.
    last_trigger = _unix(timer.get("LastTriggerUSec", ""))
    if not last_exit and last_trigger and now - last_trigger > MAX_AGE_HOURS * 3600:
        problems.append(f"backup timer last fired {int((now - last_trigger) // 3600)} h ago")

    return {
        "configured": True,
        "ok": not problems,
        "problems": problems,
        "mount": mount,
        "mounted": mounted,
        "last_run": last_exit,
        "last_result": result or None,
        "last_trigger": last_trigger,
    }


async def _systemctl_show(unit: str, props: list[str]) -> dict[str, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "systemctl", "show", unit, "--timestamp=unix", *[f"-p{p}" for p in props],
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
    except (OSError, asyncio.TimeoutError):
        return {}
    info: dict[str, str] = {}
    for line in out.decode(errors="replace").splitlines():
        key, _, val = line.partition("=")
        info[key] = val
    return info


async def backup_status(force: bool = False) -> dict:
    """Cached for 10 minutes — the nav polls it from every open page."""
    global _cache
    if not force and _cache and time.monotonic() - _cache[0] < _CACHE_SECS:
        return _cache[1]
    conf = read_conf()
    service = await _systemctl_show("ech-backup.service", ["Result", "ExecMainExitTimestamp"])
    timer = await _systemctl_show("ech-backup.timer", ["LastTriggerUSec", "UnitFileState"])
    if conf is None and timer.get("UnitFileState") == "enabled":
        # Installs from before setup_backup.sh made backup.conf world-readable:
        # backups are set up, we just can't see the mount — the run/timer
        # checks still catch failures (the script exits 1 when unmounted).
        conf = {}
    mount = (conf or {}).get("BACKUP_MOUNT", "")
    mounted = os.path.ismount(mount) if mount else None
    status = evaluate(conf, mounted, service, timer)
    _cache = (time.monotonic(), status)
    return status
