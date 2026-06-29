"""
ech/adapters/base.py
--------------------
Abstract base class every transport adapter must implement.
Drop a new module into ech/adapters/ that subclasses Adapter, register it
in config.yaml, and ECH will load it automatically — no core edits required.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import AsyncIterator

from ech.core.models import ChannelHealth, MeshNode, NormalizedMessage

log = logging.getLogger(__name__)


class Adapter(ABC):
    """
    Contract all transport adapters must satisfy.

    Lifecycle
    ---------
    1. __init__(config)   — validate config, set up state, no I/O yet
    2. connect()          — open serial port / TCP socket / BLE connection
    3. receive()          — yield NormalizedMessages until disconnect
    4. send(msg)          — deliver a message; return True on success
    5. disconnect()       — clean teardown; called on shutdown or config reload
    """

    is_mock: bool = False  # override to True in mock/sim adapters

    def __init__(self, config: dict):
        self.config = config
        self.name: str = config.get("name", self.__class__.__name__.lower())
        self._connected = False
        self._paused = False
        self._last_rx = None
        self._last_tx = None
        self._rx_queue: asyncio.Queue[NormalizedMessage] = asyncio.Queue(maxsize=512)
        # Delivery tracking — set by router after registration
        self._last_sent_uuid: str | None = None   # UUID of most recently sent message
        self._router_notify = None                 # async callback(adapter, uuid, status, detail)
        self._router_notify_nodes = None           # async callback(adapter, node_count)
        self._router_broadcast = None              # async callback(event_type, data_dict)

    # ── Required ──────────────────────────────────────────────────────────

    @abstractmethod
    async def connect(self) -> None:
        """Open the underlying transport. Raise on unrecoverable failure."""

    @abstractmethod
    async def disconnect(self) -> None:
        """Clean shutdown. Must be idempotent."""

    @abstractmethod
    async def send(self, message: NormalizedMessage) -> bool:
        """Send message. Return True if delivered to transport layer."""

    @abstractmethod
    async def _run(self) -> None:
        """
        Internal receive loop. Put inbound NormalizedMessages onto
        self._rx_queue. Should run until disconnect() is called.
        Override this, not receive().
        """

    # ── Optional overrides ────────────────────────────────────────────────

    async def nodes(self) -> list[MeshNode]:
        """Return visible mesh nodes. Override for Meshtastic / MeshCore."""
        return []

    async def time_sync(self) -> bool:
        """Sync the device clock to current UTC. Override in adapters that support it."""
        return False

    async def announce(self) -> bool:
        """Broadcast node presence to the network. Override in adapters that support it."""
        return False

    async def ping(self, node_id: str) -> dict:
        """Send a traceroute/ping to a discovered node. Override in adapters that support it."""
        return {"status": "unsupported"}

    async def health(self) -> ChannelHealth:
        from ech.core.models import ChannelState
        detail = self._health_detail()
        detail["paused"] = self._paused
        return ChannelHealth(
            adapter=self.name,
            state=ChannelState.CONNECTED if self._connected else ChannelState.DISCONNECTED,
            last_rx=self._last_rx,
            last_tx=self._last_tx,
            detail=detail,
        )

    def pause(self) -> None:
        self._paused = True
        log.info("%s: paused", self.name)

    def resume(self) -> None:
        self._paused = False
        log.info("%s: resumed", self.name)

    def set_base_location(self, lat: float, lon: float) -> None:
        """Update simulated node positions. Override in mock adapters; no-op for real ones."""
        pass

    def is_paused(self) -> bool:
        return self._paused

    def _health_detail(self) -> dict:
        """Override to add adapter-specific stats to the health payload."""
        return {}

    # ── Public receive iterator ───────────────────────────────────────────

    async def receive(self) -> AsyncIterator[NormalizedMessage]:
        """
        Async generator consumed by the router.
        Pulls from the internal queue populated by _run().
        When paused, drains the queue without yielding so it doesn't overflow.
        """
        while self._connected:
            try:
                msg = await asyncio.wait_for(self._rx_queue.get(), timeout=1.0)
                if self._paused:
                    continue   # discard — adapter is paused
                self._last_rx = msg.timestamp
                yield msg
            except asyncio.TimeoutError:
                continue

    # ── Helpers ───────────────────────────────────────────────────────────

    async def _enqueue(self, msg: NormalizedMessage) -> None:
        try:
            self._rx_queue.put_nowait(msg)
        except asyncio.QueueFull:
            log.warning("%s: RX queue full, dropping message from %s", self.name, msg.from_id)

    def _mark_tx(self, msg: NormalizedMessage) -> None:
        import datetime
        self._last_tx = datetime.datetime.now(datetime.timezone.utc)
