"""
ech/adapters/mock_sms.py
------------------------
Mock SMS adapter for development without a physical modem.
Generates realistic inbound SMS messages from fake phone numbers,
simulates a contact list, and supports the full send/receive cycle.
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone

from ech.adapters.base import Adapter
from ech.core.models import NormalizedMessage, Priority

log = logging.getLogger(__name__)

FAKE_CONTACTS = [
    {"number": "+12075551001", "name": "John EOC Director"},
    {"number": "+12075551002", "name": "Sarah CERT Lead"},
    {"number": "+12075551003", "name": "Mike Shelter Mgr"},
    {"number": "+12075551004", "name": "Lisa DEM Liaison"},
    {"number": "+12075551005", "name": "Tom Mobile Unit"},
]

SMS_MESSAGES = [
    "En route to staging area, ETA 15 min",
    "Shelter at 40% capacity, accepting more",
    "Road closure on Rte 1 near mile marker 22",
    "Need 3 additional cots at main shelter",
    "All units report in please",
    "Generator fuel running low, need resupply",
    "Medical team on scene, situation stable",
    "Communications check — please acknowledge",
    "EOC activated, report to primary location",
    "Resource request: 2 chain saws, safety gear",
]


class MockSMSAdapter(Adapter):
    """
    Mock SMS adapter.
    Config keys:
        name            str     adapter name (default: sms-mock)
        interval_sec    float   seconds between inbound messages (default: 25.0)
        operator        str     simulated carrier name (default: MockCell)
    """

    def __init__(self, config: dict):
        super().__init__(config)
        self.name = config.get("name", "sms-mock")
        self._interval = config.get("interval_sec", 25.0)
        self._operator = config.get("operator", "MockCell")
        self._run_task: asyncio.Task | None = None
        self._tx_count = 0
        self._rx_count = 0
        self._tick = 0

    async def connect(self) -> None:
        log.info("%s: connecting (mock SMS, operator=%s)", self.name, self._operator)
        await asyncio.sleep(0.2)
        self._connected = True
        self._run_task = asyncio.create_task(self._run(), name=f"{self.name}-run")
        log.info("%s: connected", self.name)

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
            log.warning("%s: no to_id for SMS send", self.name)
            return False
        await asyncio.sleep(0.3)
        self._tx_count += 1
        self._mark_tx(message)
        log.debug("%s: TX → %s: %s", self.name, message.to_id, message.body[:60])
        return True

    async def _run(self) -> None:
        log.debug("%s: RX loop started", self.name)
        try:
            while self._connected:
                if getattr(self, '_paused', False):
                    await asyncio.sleep(1.0)
                    continue
                await asyncio.sleep(self._interval + random.uniform(-5, 5))
                self._tick += 1
                self._rx_count += 1

                contact = random.choice(FAKE_CONTACTS)
                body = random.choice(SMS_MESSAGES)
                priority = Priority.NORMAL

                if self._tick % 9 == 0:
                    body = "URGENT: need immediate assistance at shelter B"
                    priority = Priority.ELEVATED
                elif self._tick % 19 == 0:
                    body = "911 EMERGENCY all units respond to main shelter NOW"
                    priority = Priority.EMERGENCY

                msg = NormalizedMessage(
                    source_adapter=self.name,
                    source_channel="SMS",
                    from_id=contact["number"],
                    from_display=contact["name"],
                    body=body,
                    priority=priority,
                    raw={"number": contact["number"]},
                )
                await self._enqueue(msg)

        except asyncio.CancelledError:
            log.debug("%s: RX loop cancelled", self.name)

    def _health_detail(self) -> dict:
        return {
            "operator": self._operator,
            "rx_count": self._rx_count,
            "tx_count": self._tx_count,
            "mode": "mock",
        }
