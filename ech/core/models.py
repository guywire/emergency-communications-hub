"""
ech/core/models.py
------------------
Canonical data models shared across the entire ECH stack.
All adapters convert inbound packets to NormalizedMessage before the router
sees them. Nothing outside adapters/ should import raw hardware types.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import IntEnum
from typing import Any


class Priority(IntEnum):
    NORMAL = 0
    ELEVATED = 1
    EMERGENCY = 2


class ChannelState(str):
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    ERROR = "error"
    CONNECTING = "connecting"


@dataclass
class NormalizedMessage:
    """Single canonical message representation used throughout ECH."""

    source_adapter: str          # adapter name, e.g. "meshtastic-usb"
    source_channel: str          # channel/freq/group within that adapter
    from_id: str                 # raw sender — callsign, node hex ID, phone
    body: str

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    from_display: str = ""       # resolved from contact store; "" until resolved
    to_id: str | None = None     # None means broadcast / channel message
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    priority: Priority = Priority.NORMAL
    lat: float | None = None
    lon: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "source_adapter": self.source_adapter,
            "source_channel": self.source_channel,
            "from_id": self.from_id,
            "from_display": self.from_display or self.from_id,
            "to_id": self.to_id,
            "body": self.body,
            "timestamp": self.timestamp.isoformat(),
            "priority": int(self.priority),
            "lat": self.lat,
            "lon": self.lon,
        }


@dataclass
class ChannelHealth:
    adapter: str
    state: str
    last_rx: datetime | None = None
    last_tx: datetime | None = None
    detail: dict[str, Any] = field(default_factory=dict)   # adapter-specific stats

    def to_dict(self) -> dict:
        return {
            "adapter": self.adapter,
            "state": self.state,
            "last_rx": self.last_rx.isoformat() if self.last_rx else None,
            "last_tx": self.last_tx.isoformat() if self.last_tx else None,
            "detail": self.detail,
        }


@dataclass
class MeshNode:
    """A node visible in a mesh network (Meshtastic or MeshCore)."""
    node_id: str
    display_name: str
    short_name: str = ""
    last_heard: datetime | None = None
    snr: float | None = None
    rssi: int | None = None
    battery_level: int | None = None   # 0-100
    firmware_version: str = ""
    lat: float | None = None
    lon: float | None = None

    def to_dict(self) -> dict:
        return {
            "node_id": self.node_id,
            "display_name": self.display_name,
            "short_name": self.short_name,
            "last_heard": self.last_heard.isoformat() if self.last_heard else None,
            "snr": self.snr,
            "rssi": self.rssi,
            "battery_level": self.battery_level,
            "firmware_version": self.firmware_version,
            "lat": self.lat,
            "lon": self.lon,
        }
