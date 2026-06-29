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
    {"number": "+12075551001", "name": "Pat EOC Director"},
    {"number": "+12075551002", "name": "Ray CERT Lead"},
    {"number": "+12075551003", "name": "Sue Warming Center Mgr"},
    {"number": "+12075551004", "name": "Dan DEM Liaison"},
    {"number": "+12075551005", "name": "Chris DPW Supervisor"},
]

SMS_MESSAGES = [
    "Tree down on Oak St — sending crew now, ETA 30 min",
    "Warming Center B at capacity (80), redirecting to high school",
    "Pipe burst in Town Hall basement — water off, plumber called",
    "Road closure Rte 1 MM22 — large oak across both lanes",
    "Need 10 more cots and blankets at warming center ASAP",
    "Generator at warming center low on fuel — can you authorize refill?",
    "Welfare check complete sector 3 — 2 residents transported to warming center",
    "Harbor reports lobsters blocking Pier 3 access — coast guard alerted",
    "Power out in 4 neighborhoods, utility ETA 6 hrs",
    "Salt truck broke down on Cedar Ave — other truck covering",
    "Can someone check on Mrs. Landry at 14 Pine? No answer on phone",
    "Windchill hits -20F tonight — running extra patrol for stranded motorists",
]


class MockSMSAdapter(Adapter):
    is_mock = True

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
                if self.is_paused():
                    await asyncio.sleep(1.0)
                    continue
                await asyncio.sleep(self._interval + random.uniform(-5, 5))
                self._tick += 1
                self._rx_count += 1

                contact = random.choice(FAKE_CONTACTS)
                body = random.choice(SMS_MESSAGES)
                priority = Priority.NORMAL

                if self._tick % 9 == 0:
                    body = "URGENT: suspected hypothermia case at warming center, ambulance requested"
                    priority = Priority.ELEVATED
                elif self._tick % 19 == 0:
                    body = "EMERGENCY: LOBSTER REVOLT confirmed at docks — claws up, all units respond"
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
