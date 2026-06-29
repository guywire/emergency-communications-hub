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


def _is_mock(adapter) -> bool:
    return bool(getattr(adapter, "is_mock", False))


# Built-in mock adapters auto-started when simulation is enabled and no mocks are configured
_BUILTIN_SIM_CONFIGS = [
    {"type": "mock_meshtastic", "name": "__sim_meshtastic", "interval_sec": 8.0,  "node_count": 5},
    {"type": "mock_aprs",       "name": "__sim_aprs",       "interval_sec": 12.0},
    {"type": "mock_meshcore",   "name": "__sim_meshcore",   "interval_sec": 15.0},
    {"type": "mock_sms",        "name": "__sim_sms",        "interval_sec": 25.0},
]


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
        self._base_lat: float | None = None
        self._base_lon: float | None = None
        self._builtin_sim_names: list[str] = []   # names of auto-created sim adapters

    async def init(self) -> None:
        """Load persisted state from DB."""
        self._mode = await self._db.get_kv("mode") or "standard"
        self._simulation_enabled = (await self._db.get_kv("simulation_enabled") or "true") == "true"
        self._incident_name = await self._db.get_kv("incident_name") or "EXERCISE"
        self._operator_callsign = await self._db.get_kv("operator_callsign") or ""

        lat_s = await self._db.get_kv("base_lat")
        lon_s = await self._db.get_kv("base_lon")
        if lat_s and lon_s:
            try:
                self._base_lat = float(lat_s)
                self._base_lon = float(lon_s)
            except ValueError:
                pass

        # Restore persisted weather configuration on startup.
        import json
        wx_json = await self._db.get_kv("wx_config")
        if wx_json and self._wx_service:
            try:
                cfg = json.loads(wx_json)
                await self.update_weather_config(cfg)
                self._wx_config = cfg
                log.info("ECHState: restored weather configuration from database")
            except Exception as exc:
                log.warning("ECHState: failed to restore weather configuration: %s", exc)

        log.info("ECHState: mode=%s simulation=%s incident=%s base=(%.4f,%.4f)",
                 self._mode, self._simulation_enabled, self._incident_name,
                 self._base_lat or 0.0, self._base_lon or 0.0)
        # Propagate base location to adapters before they connect (set_base_location is no-op on real adapters)
        if self._base_lat is not None and self._router:
            for adapter in self._router._adapters.values():
                adapter.set_base_location(self._base_lat, self._base_lon)
        # Simulation toggle is a full live/sim switch:
        #   sim ON  → mocks run, real adapters paused
        #   sim OFF → real adapters run, mocks paused
        if self._router:
            has_mocks = any(_is_mock(a) for a in self._router._adapters.values())
            for adapter_name, adapter in self._router._adapters.items():
                if _is_mock(adapter):
                    if self._simulation_enabled:
                        adapter.resume()
                    else:
                        adapter.pause()
                        log.info("ECHState init: paused mock adapter '%s'", adapter_name)
                else:
                    if self._simulation_enabled:
                        adapter.pause()
                        log.info("ECHState init: paused real adapter '%s' (simulation active)", adapter_name)
                    else:
                        adapter.resume()
                        log.info("ECHState init: real adapter '%s' active", adapter_name)
            # If no mocks in config and simulation is enabled, start built-ins
            if self._simulation_enabled and not has_mocks:
                await self._start_builtin_sim_adapters()

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

        if self._router:
            has_config_mocks = any(
                _is_mock(a) and a.name not in self._builtin_sim_names
                for a in self._router._adapters.values()
            )
            if enabled:
                # Sim ON: mocks run, real adapters pause
                for name, adapter in list(self._router._adapters.items()):
                    if _is_mock(adapter):
                        adapter.resume()
                        log.info("ECHState: mock %s resumed", name)
                    else:
                        adapter.pause()
                        log.info("ECHState: real adapter %s paused (sim active)", name)
                if not has_config_mocks:
                    await self._start_builtin_sim_adapters()
            else:
                # Sim OFF: real adapters run, mocks pause
                for name, adapter in list(self._router._adapters.items()):
                    if _is_mock(adapter) and name not in self._builtin_sim_names:
                        adapter.pause()
                        log.info("ECHState: mock %s paused", name)
                    elif not _is_mock(adapter):
                        adapter.resume()
                        log.info("ECHState: real adapter %s resumed", name)
                await self._stop_builtin_sim_adapters()

        await self._broadcast("simulation_change", {"enabled": enabled})
        log.info("ECHState: simulation %s", "enabled" if enabled else "disabled")

    async def _start_builtin_sim_adapters(self) -> None:
        """Instantiate and start the built-in simulation adapters."""
        if not self._router:
            return
        from ech.main import build_adapter
        for cfg in _BUILTIN_SIM_CONFIGS:
            if cfg["name"] in self._router._adapters:
                continue
            full_cfg = dict(cfg)
            if self._base_lat is not None:
                full_cfg["base_lat"] = self._base_lat
                full_cfg["base_lon"] = self._base_lon
            try:
                adapter = build_adapter(full_cfg)
                await self._router.start_adapter(adapter)
                self._builtin_sim_names.append(cfg["name"])
                log.info("ECHState: started built-in sim adapter '%s'", cfg["name"])
            except Exception as exc:
                log.warning("ECHState: failed to start built-in sim adapter '%s': %s", cfg["name"], exc)

    async def _stop_builtin_sim_adapters(self) -> None:
        """Stop and remove built-in simulation adapters."""
        if not self._router:
            return
        for name in list(self._builtin_sim_names):
            await self._router.stop_adapter(name)
            log.info("ECHState: stopped built-in sim adapter '%s'", name)
        self._builtin_sim_names.clear()

    # ── Base location ─────────────────────────────────────────────────────

    async def set_base_location(self, lat: float, lon: float) -> None:
        """Update the global base location — propagates to weather, mock adapters, and DB."""
        self._base_lat = lat
        self._base_lon = lon
        await self._db.set_kv("base_lat", str(lat))
        await self._db.set_kv("base_lon", str(lon))
        # Propagate to weather service
        if self._wx_service:
            self._wx_service._lat = lat
            self._wx_service._lon = lon
            # Re-persist weather config with new coords
            import json
            wx_cfg = {**self._wx_config, "nws_lat": lat, "nws_lon": lon}
            await self._db.set_kv("wx_config", json.dumps(wx_cfg))
            self._wx_config = wx_cfg
        # Propagate to all adapters (no-op for real adapters; updates positions in mocks)
        if self._router:
            for adapter in self._router._adapters.values():
                adapter.set_base_location(lat, lon)
        await self._broadcast("base_location_change", {"lat": lat, "lon": lon})
        log.info("ECHState: base location set to (%.5f, %.5f)", lat, lon)

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
        if "auto_broadcast_adapters" in config:
            self._wx_service._auto_adapters = list(config["auto_broadcast_adapters"])
        lat = config.get("nws_lat")
        lon = config.get("nws_lon")
        if lat is not None: self._wx_service._lat = float(lat)
        if lon is not None: self._wx_service._lon = float(lon)
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
            "base_lat": self._base_lat,
            "base_lon": self._base_lon,
            "builtin_sim_active": list(self._builtin_sim_names),
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
