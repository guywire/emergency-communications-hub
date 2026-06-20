"""
ech/adapters/mock_aprs.py
--------------------------
Simulates APRS traffic for development.
Generates position reports, messages, and status packets
representative of what APRS-IS or a local TNC would deliver.
Replace with real APRSAdapter when TNC/internet is available.
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone

from ech.adapters.base import Adapter
from ech.core.models import ChannelHealth, NormalizedMessage, Priority

log = logging.getLogger(__name__)

APRS_STATIONS = [
    {"call": "W1PBR-9",   "name": "W1PBR Mobile",    "lat": 44.102, "lon": -69.118},
    {"call": "KD1NET",    "name": "KD1NET",           "lat": 44.108, "lon": -69.125},
    {"call": "N1EOC-1",   "name": "N1EOC EOC Relay",  "lat": 44.112, "lon": -69.115},
    {"call": "W1XYZ-7",   "name": "W1XYZ Digi",       "lat": 44.095, "lon": -69.100},
    {"call": "KB1MARF",   "name": "KB1MARF Fixed",    "lat": 44.120, "lon": -69.135},
]

APRS_MESSAGES = [
    "QRV and monitoring",
    "Position updated via GPS",
    "Digi operational, hearing 8 stations",
    "Weather: clear, wind SW 12mph",
    "Battery on solar, 13.2V",
    "EOC relay active on 144.390",
    "Traveling south on Rte 1",
    "Test packet 73",
]

APRS_STATUS = [
    "Emergency Coordinator on duty",
    "ARES NET in 30 min",
    "Shelter open, 23 occupants",
]


class MockAPRSAdapter(Adapter):
    """
    Mock APRS adapter simulating a mix of position, message, and status packets.
    Config keys:
        name            str     adapter name (default: aprs-mock)
        source          str     'aprsis' | 'kiss' | 'agwpe' (cosmetic, default: aprsis)
        filter_radius   int     simulated filter radius in km (default: 50)
        interval_sec    float   seconds between packets (default: 12.0)
    """

    def __init__(self, config: dict):
        super().__init__(config)
        self.name = config.get("name", "aprs-mock")
        self._source = config.get("source", "aprsis")
        self._filter_radius = config.get("filter_radius", 50)
        self._interval = config.get("interval_sec", 12.0)
        self._packet_count = 0
        self._run_task: asyncio.Task | None = None

    async def connect(self) -> None:
        log.info("%s: connecting (mock, source=%s)", self.name, self._source)
        await asyncio.sleep(0.4)
        self._connected = True
        self._run_task = asyncio.create_task(self._run(), name=f"{self.name}-run")
        log.info("%s: connected, simulating APRS-IS filter r/44.1/-69.1/%d",
                 self.name, self._filter_radius)

    async def disconnect(self) -> None:
        self._connected = False
        if self._run_task:
            self._run_task.cancel()
            try:
                await self._run_task
            except asyncio.CancelledError:
                pass
        log.info("%s: disconnected", self.name)

    async def send(self, message: NormalizedMessage) -> bool:
        """Send an APRS message packet to a specific callsign."""
        await asyncio.sleep(0.2)
        log.debug("%s: TX APRS msg → %s | %s", self.name, message.to_id, message.body[:60])
        self._mark_tx(message)
        return True

    async def _run(self) -> None:
        log.debug("%s: RX loop started", self.name)
        try:
            while self._connected:
                if getattr(self, '_paused', False):
                    await asyncio.sleep(1.0)
                    continue
                await asyncio.sleep(self._interval + random.uniform(-2, 2))
                self._packet_count += 1

                station = random.choice(APRS_STATIONS)
                packet_type = random.choices(
                    ["position", "message", "status"],
                    weights=[0.6, 0.3, 0.1],
                )[0]

                # Drift position
                lat = station["lat"] + random.uniform(-0.005, 0.005)
                lon = station["lon"] + random.uniform(-0.005, 0.005)

                if packet_type == "position":
                    body = (
                        f"={lat:.4f}N/{abs(lon):.4f}W> "
                        f"[{random.choice(APRS_MESSAGES)}]"
                    )
                    msg_lat, msg_lon = round(lat, 5), round(lon, 5)
                elif packet_type == "message":
                    dest = random.choice(APRS_STATIONS)
                    body = f":{dest['call']:<9}:{random.choice(APRS_MESSAGES)}"
                    msg_lat, msg_lon = None, None
                else:
                    body = f">APRS,TCPIP*:{random.choice(APRS_STATUS)}"
                    msg_lat, msg_lon = None, None

                # Simulate occasional emergency packet
                priority = Priority.NORMAL
                if self._packet_count % 20 == 0:
                    priority = Priority.EMERGENCY
                    body = f"MAYDAY {station['call']} emergency position {lat:.4f}N {abs(lon):.4f}W"
                    msg_lat, msg_lon = round(lat, 5), round(lon, 5)

                msg = NormalizedMessage(
                    source_adapter=self.name,
                    source_channel=f"144.390 ({self._source})",
                    from_id=station["call"],
                    from_display=station["name"],
                    body=body,
                    priority=priority,
                    lat=msg_lat,
                    lon=msg_lon,
                    raw={"packet_type": packet_type, "source": self._source},
                )
                await self._enqueue(msg)
                log.debug("%s: packet #%d from %s (%s)",
                          self.name, self._packet_count, station["call"], packet_type)

        except asyncio.CancelledError:
            log.debug("%s: RX loop cancelled", self.name)

    def _health_detail(self) -> dict:
        return {
            "source": self._source,
            "filter_radius_km": self._filter_radius,
            "packets_received": self._packet_count,
            "mode": "mock",
        }
