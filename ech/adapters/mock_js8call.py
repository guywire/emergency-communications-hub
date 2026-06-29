"""
ech/adapters/mock_js8call.py
-----------------------------
Mock JS8Call adapter — simulates HF JS8 traffic for development.
Generates realistic HF-style messages with callsigns, SNR reports,
band conditions, and occasional net activity.
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone

from ech.adapters.base import Adapter
from ech.core.models import NormalizedMessage, Priority

log = logging.getLogger(__name__)

HF_STATIONS = [
    {"call": "W1PBR",   "grid": "FN44"},
    {"call": "KD1NET",  "grid": "FN44"},
    {"call": "N1EOC",   "grid": "FN43"},
    {"call": "W1XYZ",   "grid": "FN45"},
    {"call": "KA1MARF", "grid": "FN44"},
    {"call": "N1SAR",   "grid": "FN42"},
]

HF_MSGS = [
    "Net check-in, storm monitoring ongoing SNR",
    "Wx: temp 6F, wind NW 28mph, windchill -18F SNR",
    "Frozen pipe burst at County Road shelter, crew dispatched SNR",
    "Tree down on power line Rte 1 — utility ETA 4hrs SNR",
    "Warming center at library: 22 occupants, space for 60 more SNR",
    "Battery backup on generator, 13.8V nominal SNR",
    "Mobile welfare check unit cleared sector 2, 3 assists SNR",
    "All HF nets monitoring 7.078 dial through the storm SNR",
    "Lobster uprising activity near Penobscot Bay — monitoring situation SNR",
    "Road crews working Cedar Lane, ETA clearance 2 hours SNR",
]

HF_NET_CALLS = [
    "@ALLCALL: ARES winter storm net active, all stations check in",
    "@NET: ICS-213 resource request traffic follows, stand by",
    "@ALLCALL: Priority traffic — warming center overflow, need locations",
]

BANDS = [
    ("7.078 MHz", 7078000),
    ("14.078 MHz", 14078000),
    ("10.130 MHz", 10130000),
]


class MockJS8CallAdapter(Adapter):
    is_mock = True

    """
    Mock JS8Call adapter — generates realistic HF JS8 traffic.
    Config keys:
        name            str     adapter name (default: hf-js8call-mock)
        callsign        str     local station callsign (default: W1ABC)
        interval_sec    float   seconds between messages (default: 20.0)
    """

    def __init__(self, config: dict):
        super().__init__(config)
        self.name = config.get("name", "hf-js8call-mock")
        self._callsign = config.get("callsign", "W1ABC")
        self._interval = config.get("interval_sec", 20.0)
        self._band = random.choice(BANDS)
        self._run_task: asyncio.Task | None = None
        self._tick = 0

    async def connect(self) -> None:
        log.info("%s: connecting (mock HF JS8Call, band=%s)", self.name, self._band[0])
        await asyncio.sleep(0.3)
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
        await asyncio.sleep(0.5)   # simulate ~500ms TX queue delay
        dest = message.to_id or "@ALLCALL"
        log.debug("%s: TX → %s: %s", self.name, dest, message.body[:60])
        self._mark_tx(message)
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

                station = random.choice(HF_STATIONS)
                snr = random.randint(-20, +10)
                freq_khz = self._band[1] // 1000

                # Occasionally simulate a net call
                if self._tick % 7 == 0:
                    body = random.choice(HF_NET_CALLS)
                    from_id = "W1NET"
                    priority = Priority.ELEVATED
                elif self._tick % 13 == 0:
                    body = "MAYDAY W1SAR — person with hypothermia symptoms at harbor, need EMS"
                    from_id = "W1SAR"
                    priority = Priority.EMERGENCY
                else:
                    body = f"{random.choice(HF_MSGS)} SNR {snr:+d}dB"
                    from_id = station["call"]
                    priority = Priority.NORMAL

                msg = NormalizedMessage(
                    source_adapter=self.name,
                    source_channel=f"HF {freq_khz}kHz",
                    from_id=from_id,
                    from_display=from_id,
                    body=body,
                    priority=priority,
                    raw={
                        "snr": snr,
                        "freq_hz": self._band[1],
                        "speed": "Normal",
                        "mode": "JS8",
                    },
                )
                await self._enqueue(msg)

        except asyncio.CancelledError:
            log.debug("%s: RX loop cancelled", self.name)

    def _health_detail(self) -> dict:
        return {
            "band": self._band[0],
            "callsign": self._callsign,
            "mode": "mock",
        }
