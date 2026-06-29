"""
ech/adapters/mock_pat_winlink.py
---------------------------------
Mock Pat Winlink adapter for development without Pat or Winlink.

Also provides a tiny in-process mock Pat HTTP server (MockPatServer)
used by the test suite to exercise the real PatWinlinkAdapter
without any network dependency.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import uuid
from datetime import datetime, timezone

from ech.adapters.base import Adapter
from ech.core.models import NormalizedMessage, Priority

log = logging.getLogger(__name__)

WINLINK_STATIONS = [
    "W1EOC", "KD1NET", "N1MARF", "W1PBR", "KB1CERT", "N1SAR",
]

WINLINK_SUBJECTS = [
    "SITREP: Warming Center Status",
    "ICS-213: Resource Request — Blankets/Cots",
    "Winter Storm Net check-in",
    "Damage assessment: frozen pipes",
    "Road closure update",
    "Supply request: generator fuel",
    "Welfare check summary",
    "BOLO: Unauthorized vessels near lobster beds",
]

WINLINK_BODIES = [
    "All stations report in. Winter storm net active on Winlink. Temp -2F at EOC.",
    "Warming Center A: 54/80 capacity. Accepting more. Heat stable, generator nominal.",
    "ICS-213 resource request: 30 blankets, 15 cots, 2 propane heaters — needed at HS shelter by 1800Z.",
    "Road closures confirmed: Rte 1 MP22 (tree), Cedar Ave (ice), Industrial Park (water main break).",
    "Welfare check complete sectors 1-4. 7 residents transported to warming center. No injuries.",
    "Generator at Warming Center B: fuel at 25%. Requesting immediate resupply. ETA needed.",
    "Frozen pipe damage assessment complete for downtown: 14 structures affected, 3 require immediate attention.",
    "Harbor Master to EOC: Organized lobster activity at docks, traps being redistributed by unknown parties. Coast Guard investigating.",
]


class MockPatWinlinkAdapter(Adapter):
    is_mock = True

    """
    Mock Pat Winlink adapter.
    Simulates inbound Winlink messages without Pat or network connectivity.
    Config keys:
        name            str     adapter name (default: winlink-mock)
        callsign        str     local callsign (default: W1ABC)
        interval_sec    float   seconds between simulated inbound messages (default: 45.0)
    """

    def __init__(self, config: dict):
        super().__init__(config)
        self.name         = config.get("name", "winlink-mock")
        self._callsign    = config.get("callsign", "W1ABC")
        self._interval    = config.get("interval_sec", 45.0)
        self._run_task: asyncio.Task | None = None
        self._tick = 0
        self._rx_count = 0
        self._tx_count = 0

    async def connect(self) -> None:
        log.info("%s: connecting (mock Winlink/Pat)", self.name)
        await asyncio.sleep(0.2)
        self._connected = True
        self._run_task = asyncio.create_task(self._run(), name=f"{self.name}-run")
        log.info("%s: connected, callsign=%s", self.name, self._callsign)

    async def disconnect(self) -> None:
        self._connected = False
        if self._run_task:
            self._run_task.cancel()
            try:
                await self._run_task
            except asyncio.CancelledError:
                pass

    async def send(self, message: NormalizedMessage) -> bool:
        if not message.to_id:
            log.warning("%s: to_id required for Winlink send", self.name)
            return False
        await asyncio.sleep(0.3)
        self._tx_count += 1
        self._mark_tx(message)
        log.debug("%s: TX → %s [%s]", self.name, message.to_id, message.body[:60])
        return True

    async def _run(self) -> None:
        try:
            while self._connected:
                if self.is_paused():
                    await asyncio.sleep(1.0)
                    continue
                await asyncio.sleep(self._interval + random.uniform(-10, 10))
                self._tick += 1
                self._rx_count += 1

                sender  = random.choice(WINLINK_STATIONS)
                subject = random.choice(WINLINK_SUBJECTS)
                body    = random.choice(WINLINK_BODIES)
                mid     = str(uuid.uuid4())[:8].upper()
                priority = Priority.NORMAL

                if self._tick % 8 == 0:
                    subject = "URGENT: Warming Center Overflow"
                    body    = "URGENT: High School warming center at capacity. Need overflow site. Temperature -8F outside."
                    priority = Priority.ELEVATED
                elif self._tick % 20 == 0:
                    subject = "EMERGENCY TRAFFIC"
                    body    = "EMERGENCY: Confirmed hypothermia victim — 4 Birch St. EMS dispatched. Requesting CERT welfare check sweep."
                    priority = Priority.EMERGENCY

                nm = NormalizedMessage(
                    source_adapter=self.name,
                    source_channel="Winlink",
                    from_id=sender,
                    from_display=sender,
                    body=f"[{subject}] {body}",
                    priority=priority,
                    raw={"mid": mid, "subject": subject},
                )
                await self._enqueue(nm)

        except asyncio.CancelledError:
            pass

    def _health_detail(self) -> dict:
        return {
            "callsign": self._callsign,
            "pat_url": "(mock)",
            "rx_count": self._rx_count,
            "tx_count": self._tx_count,
            "mode": "mock",
        }


# ── Tiny mock Pat HTTP server (for testing PatWinlinkAdapter) ─────────────

class MockPatServer:
    """
    Minimal asyncio HTTP server that mimics Pat's relevant API endpoints.
    Used exclusively by the test suite.

    Endpoints implemented:
      GET  /api/status             → version + empty status
      GET  /api/mailbox/in         → list of fake inbox messages
      GET  /api/mailbox/in/{mid}   → full message body
      POST /api/mailbox/out        → accept outbox post, return mid
      POST /api/connect            → acknowledge connect request
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 0):
        self._host = host
        self._port = port
        self._server = None
        self._messages: dict[str, dict] = {}   # mid → message dict
        self._outbox: list[dict] = []
        self._seed_messages()

    def _seed_messages(self) -> None:
        for i in range(3):
            mid = f"TESTMID{i:04d}"
            self._messages[mid] = {
                "mid":     mid,
                "subject": f"Test message {i}",
                "body":    f"Body of test message {i}. This is a Winlink test.",
                "from":    f"W1TEST{i}",
                "date":    datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "files":   [],
            }

    def add_message(self, mid: str, msg: dict) -> None:
        self._messages[mid] = msg

    @property
    def port(self) -> int:
        return self._server.sockets[0].getsockname()[1] if self._server else self._port

    @property
    def outbox(self) -> list[dict]:
        return list(self._outbox)

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_conn, self._host, self._port
        )
        log.debug("MockPatServer listening on %s:%d", self._host, self.port)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def _handle_conn(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            raw = await reader.read(4096)
            text = raw.decode("utf-8", errors="replace")
            lines = text.split("\r\n")
            request_line = lines[0] if lines else ""
            parts = request_line.split(" ")
            method = parts[0] if len(parts) > 0 else "GET"
            path   = parts[1] if len(parts) > 1 else "/"

            # Parse body for POST
            body_str = ""
            if "\r\n\r\n" in text:
                body_str = text.split("\r\n\r\n", 1)[1]

            response_body = self._route(method, path, body_str)
            response_json = json.dumps(response_body)
            response = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {len(response_json)}\r\n"
                "Connection: close\r\n"
                "\r\n"
                + response_json
            )
            writer.write(response.encode())
            await writer.drain()
        except Exception as exc:
            log.debug("MockPatServer handler error: %s", exc)
        finally:
            writer.close()

    def _route(self, method: str, path: str, body: str) -> dict | list:
        if path == "/api/status":
            return {
                "PatVersion": "1.0.0-mock",
                "Callsign": "W1TEST",
                "ActiveListeners": ["telnet"],
                "ConnectedTo": "",
            }

        if path == "/api/mailbox/in" and method == "GET":
            return list(self._messages.values())

        if path.startswith("/api/mailbox/in/") and method == "GET":
            mid = path.split("/")[-1]
            return self._messages.get(mid, {})

        if path == "/api/mailbox/out" and method == "POST":
            try:
                msg = json.loads(body) if body.strip() else {}
            except json.JSONDecodeError:
                msg = {}
            mid = str(uuid.uuid4())[:8].upper()
            msg["mid"] = mid
            self._outbox.append(msg)
            return {"mid": mid, "status": "queued"}

        if path == "/api/connect" and method == "POST":
            return {"status": "connecting"}

        return {}
