"""
ech/core/state.py
------------------
ECH operational state manager.

Manages:
  - Operational mode: standard | emergency
  - Simulation enable/disable (pauses mock adapter generators)
  - Weather service configuration (live reload)
  - Bridge rules (live reload)
  - General runtime configuration

State is persisted in SQLite key/value store and survives restarts.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)


class ECHState:
    """
    Central operational state store.
    All state changes are persisted and broadcast to WebSocket clients.
    """

    def __init__(self, db, router=None, wx_service=None):
        self._db = db
        self._router = router
        self._wx_service = wx_service
        self._ws_broadcast_fn = None   # set by router after init

        # In-memory state (loaded from DB on start)
        self._mode = "standard"        # standard | emergency
        self._simulation_enabled = True
        self._incident_name = "EXERCISE"
        self._operator_callsign = ""
        self._wx_config: dict = {}

    async def init(self) -> None:
        """Load persisted state from DB."""
        self._mode = await self._db.get_kv("mode") or "standard"
        self._simulation_enabled = (await self._db.get_kv("simulation_enabled") or "true") == "true"
        self._incident_name = await self._db.get_kv("incident_name") or "EXERCISE"
        self._operator_callsign = await self._db.get_kv("operator_callsign") or ""
        log.info("ECHState: mode=%s simulation=%s incident=%s",
                 self._mode, self._simulation_enabled, self._incident_name)
        # Propagate simulation state to any already-connected adapters so that
        # a disabled simulation stays paused across restarts.
        if not self._simulation_enabled and self._router:
            for adapter_name, adapter in self._router._adapters.items():
                if hasattr(adapter, '_paused'):
                    adapter._paused = True
                    log.info("ECHState init: paused adapter '%s' (simulation disabled)", adapter_name)

    # ── Mode ──────────────────────────────────────────────────────────────

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def is_emergency(self) -> bool:
        return self._mode == "emergency"

    async def set_mode(self, mode: str) -> None:
        assert mode in ("standard", "emergency")
        self._mode = mode
        await self._db.set_kv("mode", mode)
        await self._broadcast("mode_change", {"mode": mode})
        log.info("ECHState: mode changed to '%s'", mode)

    # ── Simulation ────────────────────────────────────────────────────────

    @property
    def simulation_enabled(self) -> bool:
        return self._simulation_enabled

    async def set_simulation(self, enabled: bool) -> None:
        self._simulation_enabled = enabled
        await self._db.set_kv("simulation_enabled", "true" if enabled else "false")

        # Pause/resume mock adapters via their _paused flag
        # Mock adapters check this flag in their _run() sleep loop
        if self._router:
            for name, adapter in self._router._adapters.items():
                # Only touch adapters that have a _paused attribute (mock adapters)
                if hasattr(adapter, '_paused'):
                    adapter._paused = not enabled
                    log.info("ECHState: %s simulation %s",
                             name, "resumed" if enabled else "paused")

        await self._broadcast("simulation_change", {"enabled": enabled})
        log.info("ECHState: simulation %s", "enabled" if enabled else "disabled")

    # ── Incident / operator ───────────────────────────────────────────────

    @property
    def incident_name(self) -> str:
        return self._incident_name

    @property
    def operator_callsign(self) -> str:
        return self._operator_callsign

    async def set_incident(self, name: str) -> None:
        self._incident_name = name
        await self._db.set_kv("incident_name", name)
        await self._broadcast("incident_change", {"incident_name": name})

    async def set_operator(self, callsign: str) -> None:
        self._operator_callsign = callsign
        await self._db.set_kv("operator_callsign", callsign)

    # ── Weather config live reload ────────────────────────────────────────

    async def update_weather_config(self, config: dict) -> None:
        """Update weather service config without restarting ECH."""
        if not self._wx_service:
            return
        self._wx_service._area = config.get("nws_area", self._wx_service._area)
        self._wx_service._poll_interval = int(config.get("poll_interval_sec", self._wx_service._poll_interval))
        self._wx_service._severity_filter = set(config.get("severity_filter", list(self._wx_service._severity_filter)))
        self._wx_service._auto_broadcast = bool(config.get("auto_broadcast_extreme", self._wx_service._auto_broadcast))
        lat = config.get("nws_lat")
        lon = config.get("nws_lon")
        if lat: self._wx_service._lat = float(lat)
        if lon: self._wx_service._lon = float(lon)
        # Persist
        import json
        await self._db.set_kv("wx_config", json.dumps(config))
        await self._broadcast("wx_config_change", config)
        log.info("ECHState: weather config updated: area=%s", self._wx_service._area)

    # ── Status snapshot ───────────────────────────────────────────────────

    def snapshot(self) -> dict:
        return {
            "mode": self._mode,
            "simulation_enabled": self._simulation_enabled,
            "incident_name": self._incident_name,
            "operator_callsign": self._operator_callsign,
        }

    # ── WS broadcast ──────────────────────────────────────────────────────

    def set_broadcast_fn(self, fn) -> None:
        self._ws_broadcast_fn = fn

    async def _broadcast(self, event_type: str, data: dict) -> None:
        if self._ws_broadcast_fn:
            try:
                await self._ws_broadcast_fn(event_type, data)
            except Exception as exc:
                log.debug("ECHState broadcast error: %s", exc)
