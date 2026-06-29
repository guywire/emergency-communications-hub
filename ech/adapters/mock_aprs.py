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

_STATION_TEMPLATES = [
    {"call": "W1PBR-9",   "name": "W1PBR Mobile",    "dlat": -0.010, "dlon": +0.018},
    {"call": "KD1NET",    "name": "KD1NET",           "dlat": +0.008, "dlon": -0.014},
    {"call": "N1EOC-1",   "name": "N1EOC EOC Relay",  "dlat": +0.012, "dlon": +0.005},
    {"call": "W1XYZ-7",   "name": "W1XYZ Digi",       "dlat": -0.005, "dlon": -0.022},
    {"call": "KB1MARF",   "name": "KB1MARF Fixed",    "dlat": +0.018, "dlon": +0.010},
]
_DEFAULT_LAT, _DEFAULT_LON = 44.110, -69.118

def _build_stations(base_lat: float, base_lon: float) -> list[dict]:
    return [
        {**t, "lat": round(base_lat + t["dlat"], 5), "lon": round(base_lon + t["dlon"], 5)}
        for t in _STATION_TEMPLATES
    ]

APRS_MESSAGES = [
    "Wx: 7F windchill -14F, roads icing over",
    "Tree down blocking Rte 1 MM18, use detour via Rte 3",
    "Warming Center at High School open, capacity 100",
    "Digi operational, hearing 9 stations through storm",
    "Generator fuel low at shelter, need resupply ASAP",
    "Multiple frozen pipe reports in downtown sector",
    "Mobile patrol — welfare check on elderly residents",
    "EOC relay on 144.390, all traffic clear",
    "Angry lobsters seen on town dock, harbor master notified",
    "Power restoration ETA unknown — utility crew on site",
]

APRS_STATUS = [
    "ARES EC on duty — winter storm response active",
    "Net check-in 1830Z — all stations report",
    "Warming Center A: 41 occupants, accepting more — heat OK",
]


class MockAPRSAdapter(Adapter):
    is_mock = True

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
        self._base_lat = float(config.get("base_lat", _DEFAULT_LAT))
        self._base_lon = float(config.get("base_lon", _DEFAULT_LON))
        self._stations = _build_stations(self._base_lat, self._base_lon)

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

    def set_base_location(self, lat: float, lon: float) -> None:
        self._base_lat = lat
        self._base_lon = lon
        self._stations = _build_stations(lat, lon)
        log.info("%s: repositioned stations around (%.4f, %.4f)", self.name, lat, lon)

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
                if self.is_paused():
                    await asyncio.sleep(1.0)
                    continue
                await asyncio.sleep(self._interval + random.uniform(-2, 2))
                self._packet_count += 1

                station = random.choice(self._stations)
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
                    dest = random.choice(self._stations)
                    body = f":{dest['call']:<9}:{random.choice(APRS_MESSAGES)}"
                    msg_lat, msg_lon = None, None
                else:
                    body = f">APRS,TCPIP*:{random.choice(APRS_STATUS)}"
                    msg_lat, msg_lon = None, None

                # Simulate occasional emergency packet
                priority = Priority.NORMAL
                if self._packet_count % 20 == 0:
                    priority = Priority.EMERGENCY
                    body = f"MAYDAY {station['call']} STRANDED — vehicle off road in snowdrift {lat:.4f}N {abs(lon):.4f}W"
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
