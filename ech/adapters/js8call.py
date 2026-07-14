"""
ech/adapters/js8call.py
-----------------------
JS8Call HF digital mode adapter.

JS8Call is a weak-signal HF text messaging mode (built on FT8 modulation)
with a keyboard-to-keyboard interface. It runs as a desktop application
(Windows/Linux/Mac) and exposes a JSON API over TCP on port 2442.

ECH connects to that API, subscribes to incoming message events, and can
inject outgoing messages. This means JS8Call handles all the SDR/audio
interface complexity — ECH just talks to it over localhost JSON.

Architecture on a Proxmox host or Pi:
  [Transceiver] ←audio→ [soundcard] ←alsa/pulse→ [JS8Call] ←TCP 2442→ [ECH]

JS8Call API protocol (JSON over TCP, newline-delimited):
  Messages: {"type": "TYPE", "value": "...", "params": {...}}
  Key types incoming:
    RX.MSG          — a complete decoded message (directed or broadcast)
    RX.DIRECTED     — message addressed to our callsign
    RX.ACTIVITY     — raw band activity (noisy, usually filtered out)
    INBOX.MESSAGE   — offline inbox message
    INBOX.MESSAGES  — full inbox dump
    RIG.FREQ        — frequency change notification
    MODE.SPEED      — speed change
    CLOSE           — JS8Call shutting down
  Key types we send:
    TX.SEND_MESSAGE — send a directed or broadcast message

Reference: https://pauloffordracing.com/js8call-api/
           https://github.com/jfrancis42/js8net-legacy

Config keys:
  name          str     adapter name (default: hf-js8call)
  host          str     JS8Call API host (default: 127.0.0.1)
  port          int     JS8Call API port (default: 2442)
  callsign      str     Your callsign (used to detect directed messages)
  hb_interval   int     seconds between keepalive pings (default: 30)
  rx_activity   bool    surface raw RX.ACTIVITY packets (default: False,
                        they are very noisy — only enable for debugging)
  filter_own    bool    suppress messages from our own callsign (default: True)

JS8Call setup (required on the JS8Call side):
  File → Settings → Reporting tab:
    ✓ Enable TCP Server API
    TCP Server Hostname: 127.0.0.1
    TCP Server Port: 2442
    ✓ Accept TCP Requests
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone

from ech.adapters.base import Adapter
from ech.core.models import NormalizedMessage, Priority

log = logging.getLogger(__name__)

# JS8Call API message types
RX_MSG          = "RX.MSG"
RX_DIRECTED     = "RX.DIRECTED"
RX_ACTIVITY     = "RX.ACTIVITY"
RX_BAND_ACTIVITY = "RX.BAND_ACTIVITY"
RX_CALL_ACTIVITY = "RX.CALL_ACTIVITY"
INBOX_MESSAGE   = "INBOX.MESSAGE"
INBOX_MESSAGES  = "INBOX.MESSAGES"
RIG_FREQ        = "RIG.FREQ"
MODE_SPEED      = "MODE.SPEED"
APP_CLOSE       = "CLOSE"
TX_SEND_MESSAGE = "TX.SEND_MESSAGE"
STATION_GET_INFO = "STATION.GET_INFO"
STATION_INFO    = "STATION.INFO"
PING            = "PING"

SPEED_NAMES = {0: "Normal", 1: "Fast", 2: "Turbo", 4: "Slow"}

# Emergency keywords that upgrade priority
EMRG_WORDS = {"mayday", "emergency", "sos", "distress", "help!", "life safety"}
ELVT_WORDS = {"urgent", "immediate", "priority", "standby all"}

# Maidenhead grid square pattern (4-char AA00 or 6-char AA00AA)
import re as _re
_GRID_RE = _re.compile(r'\b([A-R]{2}[0-9]{2}(?:[A-X]{2})?)\b', _re.IGNORECASE)


class JS8CallAdapter(Adapter):
    """
    JS8Call HF digital mode adapter.
    Requires JS8Call running locally with TCP API enabled on port 2442.
    """

    def __init__(self, config: dict):
        super().__init__(config)
        self.name           = config.get("name", "hf-js8call")
        self._host          = config.get("host", "127.0.0.1")
        self._port          = int(config.get("port", 2442))
        self._callsign      = config.get("callsign", "").upper()
        self._hb_interval   = int(config.get("hb_interval", 30))
        self._rx_activity   = bool(config.get("rx_activity", False))
        self._filter_own    = bool(config.get("filter_own", True))

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._run_task: asyncio.Task | None = None
        self._hb_task: asyncio.Task | None = None

        self._msg_id = 0
        self._freq_hz: int = 0
        self._dial_hz: int = 0
        self._speed: int = 0
        self._rx_count = 0
        self._tx_count = 0
        self._js8_callsign = ""   # confirmed callsign from JS8Call itself
        self._nodes: dict = {}    # callsign → MeshNode (for grid/position tracking)

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def connect(self) -> None:
        log.info("JS8Call %s: connecting to %s:%d", self.name, self._host, self._port)
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self._host, self._port),
                timeout=5.0,
            )
        except (asyncio.TimeoutError, ConnectionRefusedError, OSError) as exc:
            raise ConnectionError(
                f"JS8Call not reachable at {self._host}:{self._port} — "
                f"is JS8Call running with TCP API enabled? ({exc})"
            ) from exc

        self._connected = True

        # Query station info to confirm connection and get callsign
        await self._send(STATION_GET_INFO)
        await asyncio.sleep(0.2)

        self._run_task = asyncio.create_task(self._run(), name=f"{self.name}-run")
        self._hb_task  = asyncio.create_task(self._heartbeat(), name=f"{self.name}-hb")

        log.info("JS8Call %s: connected", self.name)

    async def disconnect(self) -> None:
        self._connected = False
        for task in (self._run_task, self._hb_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
        log.info("JS8Call %s: disconnected", self.name)

    async def send(self, message: NormalizedMessage) -> bool:
        """
        Send a JS8Call directed message or broadcast.
        to_id = callsign for directed (e.g. "W1ABC"), None for @ALLCALL broadcast.
        """
        if not self._connected:
            return False

        dest = (message.to_id or "@ALLCALL").upper()
        # JS8Call TX.SEND_MESSAGE value format: "DEST: body"
        value = f"{dest}: {message.body}"

        self._msg_id += 1
        params = {
            "_ID": str(self._msg_id),
            "FREQ": self._freq_hz or 0,
        }
        try:
            await self._send(TX_SEND_MESSAGE, value=value, params=params)
            self._tx_count += 1
            self._mark_tx(message)
            log.debug("JS8Call %s: TX → %s: %s", self.name, dest, message.body[:60])
            return True
        except Exception as exc:
            log.error("JS8Call %s: send error: %s", self.name, exc)
            return False

    # ── Internal send ─────────────────────────────────────────────────────

    async def _send(self, msg_type: str, value: str = "", params: dict | None = None) -> None:
        if not self._writer:
            return
        frame = json.dumps({
            "type": msg_type,
            "value": value,
            "params": params or {},
        }) + "\n"
        self._writer.write(frame.encode())
        await self._writer.drain()

    # ── Receive loop ──────────────────────────────────────────────────────

    async def _run(self) -> None:
        log.debug("JS8Call %s: RX loop started", self.name)
        buf = ""
        try:
            while self._connected:
                chunk = await asyncio.wait_for(
                    self._reader.read(4096), timeout=5.0
                )
                if not chunk:
                    raise ConnectionError("JS8Call closed the connection")

                buf += chunk.decode("utf-8", errors="replace")

                # JS8Call sends newline-delimited JSON
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.strip()
                    if line:
                        await self._dispatch(line)

        except asyncio.TimeoutError:
            pass   # no data for 5s is fine — heartbeat keeps it alive
        except asyncio.CancelledError:
            log.debug("JS8Call %s: RX loop cancelled", self.name)
        except ConnectionError as exc:
            log.error("JS8Call %s: %s", self.name, exc)
            self._connected = False

    async def _dispatch(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            log.debug("JS8Call %s: non-JSON line: %r", self.name, raw[:80])
            return

        msg_type = msg.get("type", "")
        value    = msg.get("value", "")
        params   = msg.get("params", {})

        if msg_type in (RX_MSG, RX_DIRECTED):
            await self._handle_rx_msg(msg_type, value, params)

        elif msg_type == INBOX_MESSAGE:
            await self._handle_inbox_msg(value, params)

        elif msg_type == INBOX_MESSAGES:
            msgs = params.get("MESSAGES", [])
            for m in msgs:
                await self._handle_inbox_msg(m.get("TEXT", ""), m)

        elif msg_type == RX_ACTIVITY and self._rx_activity:
            await self._handle_activity(value, params)

        elif msg_type == RIG_FREQ:
            self._freq_hz = int(params.get("FREQ", 0))
            self._dial_hz = int(params.get("DIAL", 0))
            log.debug("JS8Call %s: freq %d Hz", self.name, self._freq_hz)

        elif msg_type == MODE_SPEED:
            self._speed = int(params.get("SPEED", 0))
            log.debug("JS8Call %s: speed %s", self.name, SPEED_NAMES.get(self._speed, self._speed))

        elif msg_type == "RX.CALL_ACTIVITY":
            # Heard station activity — may carry grid square and SNR
            call = params.get("FROM", "").upper().strip()
            grid = params.get("GRID", "").strip()
            snr  = params.get("SNR")
            if call and grid:
                await self._update_node_grid(call, grid, snr)

        elif msg_type == STATION_INFO:
            self._js8_callsign = params.get("CALL", self._callsign).upper()
            grid  = params.get("GRID", "").strip()
            if grid:
                log.info("JS8Call %s: station info: %s grid %s", self.name, self._js8_callsign, grid)
                await self._update_node_grid(self._js8_callsign, grid, snr=None)
            else:
                log.info("JS8Call %s: station info: %s", self.name, self._js8_callsign)

        elif msg_type == APP_CLOSE:
            log.warning("JS8Call %s: JS8Call is closing", self.name)
            self._connected = False

        elif msg_type == PING:
            pass  # keepalive from JS8Call, no action needed

    async def _handle_rx_msg(self, msg_type: str, value: str, params: dict) -> None:
        """
        RX.MSG / RX.DIRECTED — a fully decoded JS8 message.
        value format: "W1ABC: message text"  or  "W1ABC>W2XYZ: message text"
        """
        self._rx_count += 1

        # Parse sender and text from value
        from_id, text = self._parse_js8_value(value)
        if not from_id or not text:
            return

        # Filter our own traffic if requested
        my_call = self._js8_callsign or self._callsign
        if self._filter_own and from_id.upper() == my_call.upper():
            return

        # Resolve frequency for channel label
        freq_khz = self._freq_hz // 1000
        ch_label = f"HF {freq_khz}kHz" if freq_khz else "HF JS8"
        snr = params.get("SNR")

        # Check if this is directed to us
        to_part = params.get("TO", "")
        is_directed = msg_type == RX_DIRECTED or (
            my_call and my_call in (to_part or value).upper()
        )

        priority = self._assess_priority(text)

        # Extract grid square from message body and update node position
        grid_match = _GRID_RE.search(text)
        grid = grid_match.group(1).upper() if grid_match else None
        if grid:
            await self._update_node_grid(from_id, grid, snr)

        # Build lat/lon for the message if we know the sender's position
        node = self._nodes.get(from_id)
        lat = node.lat if node else None
        lon = node.lon if node else None

        nm = NormalizedMessage(
            source_adapter=self.name,
            source_channel=ch_label,
            from_id=from_id,
            from_display=from_id,
            body=("[DM] " if is_directed else "") + text,
            priority=priority,
            lat=lat,
            lon=lon,
            raw={
                "snr": snr,
                "freq_hz": self._freq_hz,
                "dial_hz": self._dial_hz,
                "speed": SPEED_NAMES.get(self._speed, self._speed),
                "directed": is_directed,
                "js8_type": msg_type,
                "grid": grid,
            },
        )
        await self._enqueue(nm)
        log.debug("JS8Call %s: RX from %s%s: %s", self.name, from_id,
                  f" grid={grid}" if grid else "", text[:60])

    async def _handle_inbox_msg(self, value: str, params: dict) -> None:
        """Offline inbox messages — fetched when JS8Call starts."""
        from_id, text = self._parse_js8_value(value)
        if not from_id or not text:
            return

        utc_ts = params.get("UTC", "")
        try:
            ts = datetime.strptime(utc_ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except Exception:
            ts = datetime.now(timezone.utc)

        nm = NormalizedMessage(
            source_adapter=self.name,
            source_channel="HF inbox",
            from_id=from_id,
            from_display=from_id,
            body=f"[INBOX] {text}",
            timestamp=ts,
            priority=self._assess_priority(text),
            raw={"source": "inbox"},
        )
        await self._enqueue(nm)

    async def _handle_activity(self, value: str, params: dict) -> None:
        """Raw band activity — only emitted if rx_activity: true in config."""
        if not value.strip():
            return
        nm = NormalizedMessage(
            source_adapter=self.name,
            source_channel=f"HF activity",
            from_id=params.get("FROM", "?"),
            from_display=params.get("FROM", "?"),
            body=value[:120],
            raw={"type": "activity"},
        )
        await self._enqueue(nm)

    # ── Helpers ───────────────────────────────────────────────────────────

    def _parse_js8_value(self, value: str) -> tuple[str, str]:
        """
        Split a JS8Call value string into (from_callsign, text).
        Handles formats:
          "W1ABC: message text"
          "W1ABC>W2XYZ: message text"
          "W1ABC  W2XYZ: message text"   (older format)
        """
        if not value:
            return "", ""
        # Try "SENDER>DEST: text" or "SENDER: text"
        if ":" in value:
            header, _, text = value.partition(":")
            header = header.strip()
            text = text.strip()
            if ">" in header:
                from_id = header.split(">")[0].strip()
            else:
                from_id = header.strip()
            return from_id.upper(), text
        return "", value.strip()

    async def _update_node_grid(self, callsign: str, grid: str, snr) -> None:
        """Register or update a heard station's grid square and position."""
        from ech.core.models import MeshNode
        lat, lon = self._grid_to_latlon(grid)
        node = self._nodes.get(callsign)
        if node is None:
            node = MeshNode(
                node_id=callsign,
                display_name=callsign,
                first_seen=datetime.now(timezone.utc),
            )
            self._nodes[callsign] = node
        node.last_heard = datetime.now(timezone.utc)
        node.meta["grid"] = grid.upper()
        if lat is not None:
            node.lat = lat
            node.lon = lon
        if snr is not None:
            node.snr = float(snr)
        if self._router_notify_nodes:
            await self._router_notify_nodes(self.name, len(self._nodes))

    @staticmethod
    def _grid_to_latlon(grid: str) -> tuple:
        """Convert Maidenhead grid square to (lat, lon) center of that square."""
        grid = grid.upper().strip()
        if len(grid) < 4:
            return None, None
        try:
            lon = (ord(grid[0]) - ord('A')) * 20 - 180 + (int(grid[2])) * 2 + 1.0
            lat = (ord(grid[1]) - ord('A')) * 10 - 90  + (int(grid[3])) * 1 + 0.5
            if len(grid) >= 6:
                lon += (ord(grid[4]) - ord('A')) * (2/24) - (1/24)
                lat += (ord(grid[5]) - ord('A')) * (1/24) - (1/48)
            return round(lat, 4), round(lon, 4)
        except Exception:
            return None, None

    def _assess_priority(self, text: str) -> Priority:
        lower = text.lower()
        if any(w in lower for w in EMRG_WORDS):
            return Priority.EMERGENCY
        if any(w in lower for w in ELVT_WORDS):
            return Priority.ELEVATED
        return Priority.NORMAL

    # ── Heartbeat ─────────────────────────────────────────────────────────

    async def _heartbeat(self) -> None:
        """Periodic ping to keep TCP connection alive and poll inbox."""
        try:
            while self._connected:
                await asyncio.sleep(self._hb_interval)
                if self._connected:
                    await self._send(PING)
        except asyncio.CancelledError:
            pass

    # ── Overrides ─────────────────────────────────────────────────────────

    async def nodes(self) -> list:
        return list(self._nodes.values())

    def _health_detail(self) -> dict:
        return {
            "host": f"{self._host}:{self._port}",
            "callsign": self._js8_callsign or self._callsign,
            "freq_khz": self._freq_hz // 1000 if self._freq_hz else 0,
            "dial_khz": self._dial_hz // 1000 if self._dial_hz else 0,
            "speed": SPEED_NAMES.get(self._speed, self._speed),
            "rx_count": self._rx_count,
            "tx_count": self._tx_count,
        }
