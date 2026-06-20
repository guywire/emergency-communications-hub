"""
ech/adapters/mock_meshcore.py
------------------------------
Simulates a MeshCore network for development.
MeshCore uses callsign-based addressing and has a slightly different
node model from Meshtastic — reflected here.
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone

from ech.adapters.base import Adapter
from ech.core.models import ChannelHealth, MeshNode, NormalizedMessage, Priority

log = logging.getLogger(__name__)

MESHCORE_NODES = [
    {"id": "NODE-1", "name": "EOC Relay"},
    {"id": "NODE-2", "name": "Harbor Master"},
    {"id": "NODE-3", "name": "DPW Truck 3"},
    {"id": "NODE-4", "name": "Red Cross Shelter"},
]

def _concentric_positions(lat, lon, n, inner_km=3.0, outer_km=9.0):
    import math
    R = 6371.0
    out = []
    for i in range(n):
        dist = inner_km if i % 2 == 0 else outer_km
        angle = math.radians((i * 360.0 / n) + 45)
        dlat = math.degrees((dist / R) * math.cos(angle))
        dlon = math.degrees((dist / R) * math.sin(angle) / math.cos(math.radians(lat)))
        out.append((round(lat + dlat, 5), round(lon + dlon, 5)))
    return out

MESHCORE_MSGS = [
    "TAC-1 relay online, path clear",
    "DPW Truck 3: Elm St cleared, moving to Pine Ave",
    "Red Cross Shelter: 58 occupants, need more cots",
    "Harbor Master: 4 Canadian vessels in restricted zone — watching",
    "EOC Relay: check-in all units, cold weather protocol active",
    "Frozen water main confirmed at Industrial Park, DPW responding",
    "Need repeat, partial decode — storm interference on link",
    "ACK last, 10-4 — warming center status received",
    "Windchill advisory: -15°F overnight, all welfare checks complete",
]


class MockMeshCoreAdapter(Adapter):
    """
    Mock MeshCore adapter.
    Config keys:
        name            str     adapter name (default: meshcore-mock)
        channel         str     channel name (default: TAC-1)
        interval_sec    float   seconds between messages (default: 15.0)
    """

    def __init__(self, config: dict):
        super().__init__(config)
        self.name = config.get("name", "meshcore-mock")
        self._channel = config.get("channel", "TAC-1")
        self._interval = config.get("interval_sec", 15.0)
        self._base_lat = config.get("base_lat", None)
        self._base_lon = config.get("base_lon", None)
        self._nodes: list[MeshNode] = []
        self._run_task: asyncio.Task | None = None

    async def connect(self) -> None:
        log.info("%s: connecting (mock)", self.name)
        await asyncio.sleep(0.2)
        positions = []
        if self._base_lat is not None and self._base_lon is not None:
            positions = _concentric_positions(
                float(self._base_lat), float(self._base_lon), len(MESHCORE_NODES)
            )
        self._nodes = [
            MeshNode(
                node_id=n["id"],
                display_name=n["name"],
                short_name=n["id"],
                last_heard=datetime.now(timezone.utc),
                snr=round(random.uniform(3.0, 10.0), 1),
                rssi=random.randint(-120, -70),
                battery_level=random.randint(50, 100),
                firmware_version="mc-0.9.4",
                lat=positions[i][0] if i < len(positions) else None,
                lon=positions[i][1] if i < len(positions) else None,
            )
            for i, n in enumerate(MESHCORE_NODES)
        ]
        self._connected = True
        self._run_task = asyncio.create_task(self._run(), name=f"{self.name}-run")
        log.info("%s: connected, channel=%s, %d nodes", self.name, self._channel, len(self._nodes))

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
        await asyncio.sleep(0.15)
        log.debug("%s: TX → %s | %s", self.name, message.to_id or "ch", message.body[:60])
        self._mark_tx(message)
        return True

    async def _run(self) -> None:
        log.debug("%s: RX loop started", self.name)
        tick = 0
        try:
            while self._connected:
                if getattr(self, '_paused', False):
                    await asyncio.sleep(1.0)
                    continue
                await asyncio.sleep(self._interval + random.uniform(-3, 3))
                tick += 1

                node = random.choice(self._nodes)
                node.last_heard = datetime.now(timezone.utc)
                node.snr = round(random.uniform(2.0, 11.0), 1)

                body = random.choice(MESHCORE_MSGS)
                priority = Priority.NORMAL
                if tick % 11 == 0:
                    priority = Priority.ELEVATED
                    body = "⚡ ELEVATED: pipe freeze cascade risk — need plumber at Town Hall NOW"

                msg = NormalizedMessage(
                    source_adapter=self.name,
                    source_channel=self._channel,
                    from_id=node.node_id,
                    from_display=node.display_name,
                    body=body,
                    priority=priority,
                    lat=node.lat if (node.lat is not None and random.random() < 0.3) else None,
                    lon=node.lon if (node.lon is not None and random.random() < 0.3) else None,
                    raw={"snr": node.snr, "channel": self._channel},
                )
                await self._enqueue(msg)

        except asyncio.CancelledError:
            log.debug("%s: RX loop cancelled", self.name)

    async def nodes(self) -> list[MeshNode]:
        return list(self._nodes)

    def _health_detail(self) -> dict:
        return {
            "channel": self._channel,
            "node_count": len(self._nodes),
            "mode": "mock",
        }
