"""
ech/adapters/mock_meshtastic.py
--------------------------------
Simulates a Meshtastic mesh network for development and testing.
Generates realistic node list, periodic check-ins, and position updates.
Replace with real MeshtasticAdapter when hardware is available.
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone

from ech.adapters.base import Adapter
from ech.core.models import ChannelHealth, MeshNode, NormalizedMessage, Priority

log = logging.getLogger(__name__)

FAKE_NODES = [
    {"id": "!a1b2c3d4", "long": "EOC Main",          "short": "EOC"},
    {"id": "!b2c3d4e5", "long": "Warming Center A",   "short": "WCA"},
    {"id": "!c3d4e5f6", "long": "Warming Center B",   "short": "WCB"},
    {"id": "!d4e5f6a7", "long": "Mobile Unit 1",      "short": "MU1"},
    {"id": "!e5f6a7b8", "long": "Public Works Depot", "short": "PWD"},
]

def _concentric_positions(lat, lon, n, inner_km=2.0, outer_km=6.0):
    """Place n nodes in two concentric rings around lat,lon."""
    import math
    R = 6371.0
    out = []
    for i in range(n):
        dist = inner_km if i % 2 == 0 else outer_km
        angle = math.radians((i * 360.0 / n) + (15 if i % 2 else 0))
        dlat = math.degrees((dist / R) * math.cos(angle))
        dlon = math.degrees((dist / R) * math.sin(angle) / math.cos(math.radians(lat)))
        out.append((round(lat + dlat, 5), round(lon + dlon, 5)))
    return out

CHECK_IN_MESSAGES = [
    "Warming Center A open, capacity 80, currently 34 occupants",
    "Warming Center B open at high school gym — heat is on",
    "Road closure: Elm St impassable, tree down across road",
    "Temp: 8°F windchill -12°F — hypothermia risk for unsheltered",
    "Frozen pipe burst at Town Hall, water shut off, crew en route",
    "Power out in sector 4, generator running at warming center",
    "Public Works has 2 chainsaws on Maple Ave clearing debris",
    "Pipe freeze advisory in effect for all unheated structures",
    "Warming Center A requests 20 more blankets from supply cache",
    "Tree down on power line at Main & Oak, utility crew notified",
    "Mobile Unit 1 checking on elderly residents in sector 2",
    "Harbor watch: large lobster congregation at Pier 4, nature or coordination unclear",
]


class MockMeshtasticAdapter(Adapter):
    is_mock = True

    """
    Drop-in Meshtastic adapter that emits fake messages at a configurable rate.
    Config keys:
        name            str     adapter name shown in UI (default: meshtastic-mock)
        channel         int     channel index to simulate (default: 0)
        interval_sec    float   seconds between generated messages (default: 8.0)
        node_count      int     how many of FAKE_NODES to use (default: 5)
    """

    def __init__(self, config: dict):
        super().__init__(config)
        self.name = config.get("name", "meshtastic-mock")
        self._channel_idx = config.get("channel", 0)
        self._interval = config.get("interval_sec", 8.0)
        self._node_count = min(config.get("node_count", 5), len(FAKE_NODES))
        self._base_lat = config.get("base_lat", None)
        self._base_lon = config.get("base_lon", None)
        self._nodes: list[MeshNode] = []
        self._run_task: asyncio.Task | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def connect(self) -> None:
        log.info("%s: connecting (mock)", self.name)
        await asyncio.sleep(0.3)   # simulate handshake delay
        # Generate concentric positions around base if configured
        positions = []
        if self._base_lat is not None and self._base_lon is not None:
            positions = _concentric_positions(
                float(self._base_lat), float(self._base_lon), self._node_count
            )
        self._nodes = [
            MeshNode(
                node_id=n["id"],
                display_name=n["long"],
                short_name=n["short"],
                last_heard=datetime.now(timezone.utc),
                snr=round(random.uniform(5.0, 12.0), 1),
                rssi=random.randint(-110, -70),
                battery_level=random.randint(40, 100),
                firmware_version="2.3.14.abcdef",
                lat=positions[i][0] if i < len(positions) else None,
                lon=positions[i][1] if i < len(positions) else None,
            )
            for i, n in enumerate(FAKE_NODES[: self._node_count])
        ]
        self._connected = True
        self._run_task = asyncio.create_task(self._run(), name=f"{self.name}-run")
        log.info("%s: connected, %d nodes in mesh", self.name, len(self._nodes))

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
        if self._nodes:
            positions = _concentric_positions(lat, lon, len(self._nodes))
            for i, node in enumerate(self._nodes):
                if i < len(positions):
                    node.lat, node.lon = positions[i]
            log.info("%s: repositioned %d nodes around (%.4f, %.4f)", self.name, len(self._nodes), lat, lon)

    async def send(self, message: NormalizedMessage) -> bool:
        await asyncio.sleep(0.1)   # simulate TX delay
        log.debug("%s: TX → %s | %s", self.name, message.to_id or "broadcast", message.body[:60])
        self._mark_tx(message)
        # Echo back to inbox so sender sees their own message in the UI
        echo = NormalizedMessage(
            source_adapter=self.name,
            source_channel=f"ch{self._channel_idx}",
            from_id="!local",
            from_display="This station",
            to_id=message.to_id,
            body=f"[sent] {message.body}",
            priority=message.priority,
        )
        await self._enqueue(echo)
        return True

    # ── Internal receive loop ─────────────────────────────────────────────

    async def _run(self) -> None:
        """Generate periodic fake traffic and node heartbeats."""
        log.debug("%s: RX loop started", self.name)
        tick = 0
        try:
            while self._connected:
                if self.is_paused():
                    await asyncio.sleep(1.0)
                    continue
                await asyncio.sleep(self._interval)
                tick += 1

                # Pick a random sender node
                node = random.choice(self._nodes)
                node.last_heard = datetime.now(timezone.utc)
                node.snr = round(random.uniform(4.0, 13.0), 1)
                node.rssi = random.randint(-115, -65)
                node.battery_level = max(0, node.battery_level - random.randint(0, 1))

                # Occasionally drift position (only if position is set)
                if node.lat is not None and node.lon is not None and random.random() < 0.3:
                    node.lat = round(node.lat + random.uniform(-0.001, 0.001), 6)
                    node.lon = round(node.lon + random.uniform(-0.001, 0.001), 6)

                # Every 5th tick, emit an EMERGENCY-priority message
                priority = Priority.NORMAL
                body = random.choice(CHECK_IN_MESSAGES)
                if tick % 5 == 0:
                    priority = Priority.ELEVATED
                    body = "⚡ ELEVATED: multiple frozen pipe reports — all units confirm status"
                if tick % 17 == 0:
                    priority = Priority.EMERGENCY
                    body = "🚨 EMERGENCY: Lobster coalition storming Pier 4 — claws up, blocking dock access, request marine unit"

                msg = NormalizedMessage(
                    source_adapter=self.name,
                    source_channel=f"ch{self._channel_idx}",
                    from_id=node.node_id,
                    from_display=node.display_name,
                    body=body,
                    priority=priority,
                    lat=node.lat if (node.lat is not None and random.random() < 0.4) else None,
                    lon=node.lon if (node.lon is not None and random.random() < 0.4) else None,
                    raw={"snr": node.snr, "rssi": node.rssi, "battery": node.battery_level},
                )
                await self._enqueue(msg)
                log.debug("%s: generated message from %s", self.name, node.display_name)

        except asyncio.CancelledError:
            log.debug("%s: RX loop cancelled", self.name)

    # ── Overrides ─────────────────────────────────────────────────────────

    async def nodes(self) -> list[MeshNode]:
        return list(self._nodes)

    def _health_detail(self) -> dict:
        return {
            "channel": self._channel_idx,
            "node_count": len(self._nodes),
            "mode": "mock",
        }
