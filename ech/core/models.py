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
    # Routing / path metadata (protocol-specific; None if not available)
    hop_count: int | None = None    # RF hops taken (hopStart-hopLimit for Meshtastic, path_length for MeshCore)
    hop_start: int | None = None    # original TTL / max hops (Meshtastic only)
    path: str | None = None         # relay/digipeater chain, e.g. "WIDE1-1,W1ABC*" (APRS)
    via_mqtt: bool = False          # packet arrived via MQTT gateway rather than direct RF
    msg_type: str = "text"          # "text" | "position" | "telemetry" — non-text types skip message feed
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        _raw = self.raw or {}
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
            "hop_count": self.hop_count,
            "hop_start": self.hop_start,
            "path": self.path,
            "via_mqtt": self.via_mqtt,
            "msg_type": self.msg_type,
            "snr": _raw.get("snr"),
            "rssi": _raw.get("rssi"),
            "raw_json": self.raw or {},   # JS reads msg._raw from this; must be present on WS messages
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
    first_seen: datetime | None = None
    last_heard: datetime | None = None
    name_source: str = ""   # "self_info" | "advert" | "message_text" | "contact"
    snr: float | None = None
    rssi: int | None = None
    battery_level: int | None = None   # 0-100 %
    battery_voltage: float | None = None   # volts
    ch_util: float | None = None           # channel utilization %
    air_util_tx: float | None = None       # air utilization TX %
    uptime_secs: int | None = None
    # Environment metrics (Meshtastic sensor boards)
    temperature: float | None = None       # °C
    humidity: float | None = None          # %
    pressure: float | None = None          # hPa
    # Identity
    firmware_version: str = ""
    hw_model: str = ""
    lat: float | None = None
    lon: float | None = None
    meta: dict[str, Any] = field(default_factory=dict)  # adapter-specific extras (mmsi, icao, etc.)

    def to_dict(self) -> dict:
        d = {
            "node_id": self.node_id,
            "display_name": self.display_name,
            "short_name": self.short_name,
            "first_seen": self.first_seen.isoformat() if self.first_seen else None,
            "last_heard": self.last_heard.isoformat() if self.last_heard else None,
            "name_source": self.name_source,
            "snr": self.snr,
            "rssi": self.rssi,
            "battery_level": self.battery_level,
            "firmware_version": self.firmware_version,
            "hw_model": self.hw_model,
            "lat": self.lat,
            "lon": self.lon,
        }
        if self.battery_voltage is not None: d["battery_voltage"] = self.battery_voltage
        if self.ch_util is not None:         d["ch_util"] = self.ch_util
        if self.air_util_tx is not None:     d["air_util_tx"] = self.air_util_tx
        if self.uptime_secs is not None:     d["uptime_secs"] = self.uptime_secs
        if self.temperature is not None:     d["temperature"] = self.temperature
        if self.humidity is not None:        d["humidity"] = self.humidity
        if self.pressure is not None:        d["pressure"] = self.pressure
        if self.meta:                        d["meta"] = self.meta
        return d
