"""
ech/core/gps.py
---------------
System-level GPS reader for ECH.

Reads NMEA 0183 sentences from a serial GPS receiver (e.g. ublox) and feeds
the position to ECH state via a callback.  When a valid fix is received the
callback is called with (lat, lon, alt_m) so all adapters that use the system
base location (APRS beacons, mock node positions, map centering) are updated.

Optionally syncs the system clock from GPS UTC — useful on an offline Pi that
has no NTP.  Requires `sudo` access (or cap_sys_time) to call `date -s`.

Config (under top-level 'gps:' key in config.yaml):
  port          str     serial device, e.g. /dev/ttyAMA0, /dev/ttyUSB1
  baud          int     baud rate (default: 9600 — most ublox modules)
  time_sync     bool    if true, sync system clock from GPS UTC (default: false)
  update_interval float seconds between location updates fed to ECH state (default: 30)
  min_satellites int    minimum satellite count required for a fix (default: 4)
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
from datetime import datetime, timezone
from typing import Callable, Awaitable

log = logging.getLogger(__name__)


class GpsReader:
    """
    Async NMEA GPS reader.  Call start() to begin reading; stop() to shut down.
    on_fix is called with (lat, lon, alt_m) whenever a valid fix is obtained
    and at least update_interval seconds have passed since the last call.
    """

    def __init__(
        self,
        config: dict,
        on_fix: Callable[[float, float, float | None], Awaitable[None]] | None = None,
    ):
        self._port     = config.get("port", "/dev/ttyAMA0")
        self._baud     = int(config.get("baud", 9600))
        self._time_sync  = bool(config.get("time_sync", False))
        self._update_int = float(config.get("update_interval", 30))
        self._min_sats   = int(config.get("min_satellites", 4))
        self._on_fix     = on_fix
        self._task: asyncio.Task | None = None
        self._last_fix_time: float = 0.0
        self._last_sync_attempt: float = 0.0   # monotonic time of last sync attempt
        self._sync_fail_count: int = 0          # give up after 3 failures
        self._fix: dict | None = None       # most recent valid fix
        self._error: str | None = None      # last error string (for status)
        self._time_synced = False

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="gps-reader")
        log.info("GPS: reader started on %s @ %d baud", self._port, self._baud)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        log.info("GPS: reader stopped")

    @property
    def status(self) -> dict:
        return {
            "port": self._port,
            "baud": self._baud,
            "fix": self._fix,
            "time_sync": self._time_sync,
            "time_synced": self._time_synced,
            "error": self._error,
        }

    # ── Main loop ─────────────────────────────────────────────────────────

    async def _run(self) -> None:
        while True:
            try:
                await self._read_serial()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._error = str(exc)
                log.warning("GPS: serial error on %s: %s — retrying in 10s", self._port, exc)
                await asyncio.sleep(10)

    async def _read_serial(self) -> None:
        import serial_asyncio  # optional dep — same as meshcore/aprs_kiss
        reader, _ = await serial_asyncio.open_serial_connection(
            url=self._port, baudrate=self._baud
        )
        self._error = None
        log.info("GPS: serial connection open on %s", self._port)
        buf = b""
        while True:
            chunk = await reader.read(256)
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                sentence = line.decode("ascii", errors="ignore").strip()
                await self._parse_sentence(sentence)

    # ── NMEA parsing ──────────────────────────────────────────────────────

    async def _parse_sentence(self, sentence: str) -> None:
        if not sentence.startswith("$"):
            return
        # Verify checksum if present
        if "*" in sentence:
            body, chk = sentence[1:].rsplit("*", 1)
            expected = 0
            for ch in body:
                expected ^= ord(ch)
            if f"{expected:02X}" != chk[:2].upper():
                return  # bad checksum — discard
            sentence = "$" + body

        parts = sentence.split(",")
        msg_type = parts[0].upper()

        # Accept both GP (single-constellation) and GN (multi-constellation) prefixes
        if msg_type in ("$GPRMC", "$GNRMC"):
            await self._handle_rmc(parts)
        elif msg_type in ("$GPGGA", "$GNGGA"):
            await self._handle_gga(parts)

    async def _handle_rmc(self, parts: list[str]) -> None:
        """$GPRMC,time,status,lat,N/S,lon,E/W,speed,course,date,..."""
        try:
            if len(parts) < 7:
                return
            status = parts[2].upper()  # A=active, V=void
            if status != "A":
                return
            lat = _parse_lat(parts[3], parts[4])
            lon = _parse_lon(parts[5], parts[6])
            if lat is None or lon is None:
                return
            utc_time = parts[1]   # HHMMSS.ss
            utc_date = parts[9]   # DDMMYY
            import time as _time
            _now = _time.monotonic()
            if (self._time_sync and not self._time_synced and utc_time and utc_date
                    and _now - self._last_sync_attempt >= 300.0):
                self._last_sync_attempt = _now
                await self._sync_clock(utc_date, utc_time)
            await self._report_fix(lat, lon, alt=None)
        except Exception as exc:
            log.debug("GPS: RMC parse error: %s", exc)

    async def _handle_gga(self, parts: list[str]) -> None:
        """$GPGGA,time,lat,N/S,lon,E/W,fix_quality,sats,hdop,alt,M,..."""
        try:
            if len(parts) < 10:
                return
            fix_qual = int(parts[6]) if parts[6] else 0
            if fix_qual == 0:
                return   # no fix
            sats = int(parts[7]) if parts[7] else 0
            if sats < self._min_sats:
                return
            lat = _parse_lat(parts[2], parts[3])
            lon = _parse_lon(parts[4], parts[5])
            if lat is None or lon is None:
                return
            alt = float(parts[9]) if parts[9] else None
            await self._report_fix(lat, lon, alt=alt)
        except Exception as exc:
            log.debug("GPS: GGA parse error: %s", exc)

    # ── Fix reporting ─────────────────────────────────────────────────────

    async def _report_fix(self, lat: float, lon: float, alt: float | None) -> None:
        import time
        now = time.monotonic()
        self._fix = {"lat": lat, "lon": lon, "alt": alt,
                     "ts": datetime.now(timezone.utc).isoformat()}
        if now - self._last_fix_time < self._update_int:
            return
        self._last_fix_time = now
        log.info("GPS: fix → %.6f, %.6f alt=%s", lat, lon, alt)
        if self._on_fix:
            try:
                await self._on_fix(lat, lon, alt)
            except Exception as exc:
                log.warning("GPS: on_fix callback error: %s", exc)

    # ── Time sync ─────────────────────────────────────────────────────────

    async def _sync_clock(self, date_str: str, time_str: str) -> None:
        """Set system clock from GPS UTC. date_str=DDMMYY, time_str=HHMMSS.ss"""
        try:
            dd, mm, yy = date_str[0:2], date_str[2:4], date_str[4:6]
            hh, mn, ss = time_str[0:2], time_str[2:4], time_str[4:6]
            year = f"20{yy}"
            date_arg = f"{year}-{mm}-{dd} {hh}:{mn}:{ss}"
            result = await asyncio.create_subprocess_exec(
                "sudo", "date", "-s", date_arg,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await result.communicate()
            if result.returncode == 0:
                self._time_synced = True
                self._sync_fail_count = 0
                log.info("GPS: system clock synced to %s UTC", date_arg)
            else:
                self._sync_fail_count += 1
                msg = stderr.decode().strip()
                if self._sync_fail_count >= 3:
                    self._time_synced = True  # give up — system likely has NTP
                    log.warning("GPS: clock sync failed 3 times (%s) — giving up, NTP will handle it", msg)
                else:
                    log.warning("GPS: clock sync failed (%d/3): %s", self._sync_fail_count, msg)
        except Exception as exc:
            self._sync_fail_count += 1
            if self._sync_fail_count >= 3:
                self._time_synced = True
                log.warning("GPS: clock sync error, giving up: %s", exc)
            else:
                log.warning("GPS: clock sync error (%d/3): %s", self._sync_fail_count, exc)


# ── NMEA coordinate helpers ────────────────────────────────────────────────────

def _parse_lat(raw: str, hemi: str) -> float | None:
    if not raw or not hemi:
        return None
    try:
        deg = int(raw[:2])
        mins = float(raw[2:])
        lat = deg + mins / 60.0
        return -lat if hemi.upper() == "S" else lat
    except (ValueError, IndexError):
        return None


def _parse_lon(raw: str, hemi: str) -> float | None:
    if not raw or not hemi:
        return None
    try:
        deg = int(raw[:3])
        mins = float(raw[3:])
        lon = deg + mins / 60.0
        return -lon if hemi.upper() == "W" else lon
    except (ValueError, IndexError):
        return None
