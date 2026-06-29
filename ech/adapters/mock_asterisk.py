"""Mock Asterisk/PBX adapter for testing without real hardware."""
from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import datetime, timezone

from ech.adapters.base import Adapter
from ech.core.models import NormalizedMessage, Priority

log = logging.getLogger(__name__)

_MOCK_CALLERS = [
    ("5551234", "Ops Center"),
    ("5559876", "Field Team Alpha"),
    ("5550001", "Shelter Coordinator"),
    ("unknown", "Unknown caller"),
]


class MockAsteriskAdapter(Adapter):
    is_mock = True

    def __init__(self, config: dict):
        super().__init__(config)
        self.name          = config.get("name", "pbx-mock")
        self._interval     = float(config.get("interval_sec", 60.0))
        self._local_ext    = str(config.get("local_extension", "101"))
        self._active_calls: dict[str, dict] = {}
        self._call_log: list[dict]          = []

    async def connect(self) -> None:
        self._connected = True
        log.info("Mock Asterisk PBX: connected (simulating calls every %.0fs)", self._interval)

    async def disconnect(self) -> None:
        self._connected = False

    async def send(self, message: NormalizedMessage) -> bool:
        return False

    async def _run(self) -> None:
        await asyncio.sleep(self._interval * 0.3)
        while self._connected:
            await self._simulate_call()
            await asyncio.sleep(self._interval + random.uniform(-10, 10))

    async def _simulate_call(self) -> None:
        callerid, name = random.choice(_MOCK_CALLERS)
        exten   = self._local_ext
        uid     = f"mock-{int(time.time())}"
        duration = random.randint(0, 180)
        answered = duration > 5
        status   = "answered" if answered else "missed"
        icon     = "📞" if answered else "📵"
        dur_str  = f"{duration // 60}m {duration % 60}s" if duration >= 60 else f"{duration}s"

        body = (
            f"{icon} [INBOUND] {callerid} ({name}) → ext {exten} "
            f"| {status} | {dur_str}"
        )
        call_info = {
            "call_uid":   uid,
            "direction":  "inbound",
            "callerid":   callerid,
            "name":       name,
            "exten":      exten,
            "duration_s": duration,
            "status":     status,
            "cause":      "Normal Clearing",
        }
        msg = NormalizedMessage(
            source_adapter=self.name,
            source_channel="voice",
            from_id=callerid,
            from_display=f"{name} <{callerid}>",
            body=body,
            timestamp=datetime.now(timezone.utc),
            priority=Priority.NORMAL,
            raw=call_info,
        )
        await self._enqueue(msg)
        self._call_log.insert(0, call_info | {"body": body, "timestamp": msg.timestamp.isoformat()})
        if len(self._call_log) > 50:
            self._call_log = self._call_log[:50]
        log.info("Mock PBX: simulated call from %s (%s)", callerid, status)

    async def originate(self, destination: str, caller_extension: str | None = None) -> bool:
        src = caller_extension or self._local_ext
        log.info("Mock PBX originate: %s → %s", src, destination)
        return True

    async def page(self, target: str | None = None) -> bool:
        log.info("Mock PBX page: target=%s", target or "all")
        return True

    async def push_to_screen(self, text: str) -> bool:
        log.info("Mock PBX screen push: %s", text[:80])
        return True

    def xml_directory(self, contacts: list[dict]) -> str:
        return '<?xml version="1.0"?><YealinkIPPhoneDirectory></YealinkIPPhoneDirectory>'

    def _health_detail(self) -> dict:
        return {
            "ami_host":     "mock:5038",
            "local_ext":    self._local_ext,
            "active_calls": 0,
            "recent_calls": len(self._call_log),
            "screen_phone": False,
        }

    def recent_calls(self) -> list[dict]:
        return list(self._call_log)

    def active_calls(self) -> list[dict]:
        return []
