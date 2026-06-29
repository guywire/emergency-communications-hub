"""
ech/core/cat_rigctld.py
------------------------
CAT (Computer Aided Transceiver) control via rigctld (Hamlib).

Architecture
------------
rigctld runs separately on the same machine (or network), connected to the
radio via USB/serial.  ECH connects to rigctld over TCP and polls every
poll_interval seconds.  Current freq/mode is broadcast to all browser
clients as a WebSocket `cat_update` event so the ham log auto-fills.

rigctld setup examples
----------------------
  Xiegu G90       (CI-V 0x70, 19200 baud):
    rigctld -m 3083 -r /dev/ttyUSB0 -s 19200 -t 4532

  Icom IC-7300    (CI-V, 9600 baud):
    rigctld -m 3073 -r /dev/ttyUSB0 -s 9600 -t 4532

  Icom IC-705     (CI-V, USB):
    rigctld -m 3085 -r /dev/ttyUSB0 -t 4532

  Icom IC-9700:
    rigctld -m 3081 -r /dev/ttyUSB0 -t 4532

  Yaesu FT-991A   (38400 baud):
    rigctld -m 1035 -r /dev/ttyUSB0 -s 38400 -t 4532

  Yaesu FT-817/818:
    rigctld -m 1039 -r /dev/ttyUSB0 -s 9600 -t 4532

  Yaesu FT-DX10:
    rigctld -m 1043 -r /dev/ttyUSB0 -s 38400 -t 4532

  Any rig (test without hardware):
    rigctld -m 1 -t 4532   # dummy rig

Config (config.yaml)
---------------------
  cat:
    enabled: true
    rigctld_host: localhost
    rigctld_port: 4532
    poll_interval: 2.0       # seconds between freq/mode polls
    auto_fill_hamlog: true   # push updates to ham log via WS
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

log = logging.getLogger(__name__)

# Hamlib mode string → ECH/ADIF mode
_RIG_TO_ECH: dict[str, str] = {
    "USB":     "USB",
    "LSB":     "LSB",
    "CW":      "CW",
    "CWR":     "CW",
    "AM":      "AM",
    "FM":      "FM",
    "WFM":     "FM",
    "PKTUSB":  "DI",
    "PKTLSB":  "DI",
    "PKTFM":   "DI",
    "RTTY":    "DI",
    "RTTYR":   "DI",
    "FT8":     "FT8",
    "FT4":     "FT4",
    "C4FM":    "FM",
    "DSB":     "AM",
    "SAM":     "AM",
}

# ECH/ADIF mode → rigctld mode string
_ECH_TO_RIG: dict[str, str] = {
    "USB":     "USB",
    "LSB":     "LSB",
    "SSB":     "USB",   # default to USB; caller can override
    "CW":      "CW",
    "AM":      "AM",
    "FM":      "FM",
    "DI":      "PKTUSB",
    "FT8":     "PKTUSB",
    "FT4":     "PKTUSB",
    "JS8":     "PKTUSB",
    "WSPR":    "PKTUSB",
    "PH":      "USB",
}


def _freq_to_band(hz: int) -> str:
    mhz = hz / 1_000_000
    if mhz < 2:   return "160M"
    if mhz < 4:   return "80M"
    if mhz < 6:   return "60M"
    if mhz < 8:   return "40M"
    if mhz < 11:  return "30M"
    if mhz < 15:  return "20M"
    if mhz < 19:  return "17M"
    if mhz < 22:  return "15M"
    if mhz < 25:  return "12M"
    if mhz < 30:  return "10M"
    if mhz < 55:  return "6M"
    if mhz < 148: return "2M"
    if mhz < 225: return "1.25M"
    if mhz < 450: return "70CM"
    if mhz < 928: return "33CM"
    return "UHF"


class CATController:
    def __init__(self, config: dict, router=None):
        cat_cfg = config.get("cat", {})
        self.enabled          = bool(cat_cfg.get("enabled", False))
        self._host            = cat_cfg.get("rigctld_host", "localhost")
        self._port            = int(cat_cfg.get("rigctld_port", 4532))
        self._poll_interval   = float(cat_cfg.get("poll_interval", 2.0))
        self._auto_fill       = bool(cat_cfg.get("auto_fill_hamlog", True))
        self._router          = router

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._connected       = False
        self._freq_hz: int | None = None
        self._mode: str | None = None
        self._bw: int | None   = None
        self._rig_info: str    = ""
        self._poll_task: asyncio.Task | None = None
        self._last_update: float = 0.0
        self._error: str       = ""

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        if not self.enabled:
            log.info("CAT: disabled in config")
            return
        self._poll_task = asyncio.create_task(self._poll_loop(), name="cat-poll")
        log.info("CAT: started, rigctld=%s:%d poll=%.1fs",
                 self._host, self._port, self._poll_interval)

    async def stop(self) -> None:
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        await self._disconnect()

    # ── Connection ────────────────────────────────────────────────────────────

    async def _connect(self) -> bool:
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self._host, self._port), timeout=5.0
            )
            self._connected = True
            self._error = ""
            # Ask rigctld for rig model info
            info = await self._cmd("_")
            self._rig_info = info.strip().splitlines()[0] if info else ""
            log.info("CAT: connected to rigctld at %s:%d — %s",
                     self._host, self._port, self._rig_info or "unknown rig")
            if self._router:
                await self._router.broadcast_ws_event("cat_status", self.status())
            return True
        except Exception as exc:
            self._connected = False
            self._error = str(exc)
            log.debug("CAT: rigctld connect failed: %s", exc)
            return False

    async def _disconnect(self) -> None:
        self._connected = False
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
            self._writer = None
            self._reader = None

    # ── Poll loop ─────────────────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        backoff = 5.0
        while True:
            try:
                if not self._connected:
                    ok = await self._connect()
                    if not ok:
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 1.5, 60.0)
                        continue
                    backoff = 5.0

                freq = await self._get_freq()
                mode, bw = await self._get_mode()

                changed = (freq != self._freq_hz or mode != self._mode)
                self._freq_hz = freq
                self._mode    = mode
                self._bw      = bw
                self._last_update = time.monotonic()

                if changed and self._router and self._auto_fill:
                    await self._router.broadcast_ws_event("cat_update", self._payload())

                await asyncio.sleep(self._poll_interval)

            except asyncio.CancelledError:
                return
            except Exception as exc:
                log.warning("CAT: poll error: %s — reconnecting", exc)
                self._error = str(exc)
                await self._disconnect()
                if self._router:
                    await self._router.broadcast_ws_event("cat_status", self.status())
                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.5, 60.0)

    # ── rigctld wire protocol ─────────────────────────────────────────────────

    async def _cmd(self, command: str) -> str:
        """Send a command to rigctld and return the response text."""
        if not self._writer or not self._reader:
            raise ConnectionError("not connected")
        self._writer.write((command + "\n").encode())
        await self._writer.drain()
        lines: list[str] = []
        while True:
            try:
                raw = await asyncio.wait_for(self._reader.readline(), timeout=3.0)
            except asyncio.TimeoutError:
                raise TimeoutError(f"rigctld timeout for cmd {command!r}")
            line = raw.decode(errors="replace").rstrip("\n\r")
            if line.startswith("RPRT "):
                code = int(line.split()[1])
                if code != 0:
                    raise IOError(f"rigctld error RPRT {code} for cmd {command!r}")
                break   # success, no more data
            lines.append(line)
            # Single-value commands (f, m first line) — check if next read would block
            # For multi-line (m = two lines), keep reading until RPRT or we have enough
            if command.strip() == "m" and len(lines) >= 2:
                break
            if command.strip() in ("f", "_", "v", "t") and len(lines) >= 1:
                break
        return "\n".join(lines)

    async def _get_freq(self) -> int | None:
        try:
            resp = await self._cmd("f")
            return int(resp.strip())
        except Exception as exc:
            log.debug("CAT: get_freq error: %s", exc)
            return self._freq_hz   # return last known

    async def _get_mode(self) -> tuple[str | None, int | None]:
        try:
            resp = await self._cmd("m")
            lines = resp.strip().splitlines()
            mode = lines[0] if lines else None
            bw   = int(lines[1]) if len(lines) > 1 else 0
            return mode, bw
        except Exception as exc:
            log.debug("CAT: get_mode error: %s", exc)
            return self._mode, self._bw

    # ── Public control API ────────────────────────────────────────────────────

    async def get_freq(self) -> int | None:
        """Return current VFO frequency in Hz (cached from last poll)."""
        return self._freq_hz

    async def set_freq(self, hz: int) -> bool:
        """Set VFO frequency in Hz."""
        if not self._connected:
            return False
        try:
            await self._cmd(f"F {int(hz)}")
            self._freq_hz = hz
            if self._router:
                await self._router.broadcast_ws_event("cat_update", self._payload())
            log.info("CAT: set freq → %d Hz (%.4f MHz)", hz, hz / 1e6)
            return True
        except Exception as exc:
            log.warning("CAT: set_freq error: %s", exc)
            self._error = str(exc)
            return False

    async def set_mode(self, mode: str, bw: int = 0) -> bool:
        """Set mode using ECH mode name (USB, LSB, CW, AM, FM, DI…)."""
        if not self._connected:
            return False
        rig_mode = _ECH_TO_RIG.get(mode.upper(), mode.upper())
        try:
            await self._cmd(f"M {rig_mode} {bw}")
            self._mode = rig_mode
            self._bw   = bw
            if self._router:
                await self._router.broadcast_ws_event("cat_update", self._payload())
            log.info("CAT: set mode → %s bw=%d", rig_mode, bw)
            return True
        except Exception as exc:
            log.warning("CAT: set_mode error: %s", exc)
            self._error = str(exc)
            return False

    # ── Status / payload ──────────────────────────────────────────────────────

    def _payload(self) -> dict[str, Any]:
        """Current rig state dict broadcast as cat_update event."""
        freq_mhz = self._freq_hz / 1_000_000 if self._freq_hz else None
        ech_mode = _RIG_TO_ECH.get(self._mode or "", self._mode or "")
        band     = _freq_to_band(self._freq_hz) if self._freq_hz else None
        return {
            "connected": self._connected,
            "freq_hz":   self._freq_hz,
            "freq_mhz":  round(freq_mhz, 6) if freq_mhz else None,
            "band":      band,
            "rig_mode":  self._mode,
            "mode":      ech_mode,
            "bw":        self._bw,
            "rig_info":  self._rig_info,
        }

    def status(self) -> dict[str, Any]:
        """Full status including connectivity details."""
        p = self._payload()
        p["error"]       = self._error
        p["host"]        = self._host
        p["port"]        = self._port
        p["last_update"] = self._last_update
        return p
