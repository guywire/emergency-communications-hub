"""
ech/core/pota_service.py
-------------------------
POTA (Parks on the Air) proximity alert service.

Polls the same public activator-spot feed the map's on-demand POTA overlay
uses (api.pota.app/spot/activator — see ech/api/app.py's /api/pota/spots)
and, when a new activation appears within radius_km of the ECH base
position that hasn't been seen recently, optionally posts it to the mesh
(e.g. a "pota" MeshCore channel), mirroring WeatherService's auto-broadcast
pattern (ech/core/weather.py).

POTA's spot API has no bounding-box query parameter — "nearby" here means
a radius around the configured base position (same convention the map's
POTA layer already uses), not a literal rectangle.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from ech.core.anomaly import _haversine_km
from ech.core.models import NormalizedMessage, Priority

log = logging.getLogger(__name__)

POTA_SPOT_URL = "https://api.pota.app/spot/activator"


class PotaService:
    """Polls POTA activator spots and auto-broadcasts newly-detected nearby activations."""

    def __init__(self, config: dict, router=None):
        cfg = config.get("pota_service", {}) or {}
        self.enabled = bool(cfg.get("enabled", False))

        # Falls back to weather_service coords, same convention as air_quality_service.
        wx_cfg = config.get("weather_service", {}) or {}
        self._lat: float | None = cfg.get("lat") or wx_cfg.get("nws_lat")
        self._lon: float | None = cfg.get("lon") or wx_cfg.get("nws_lon")

        self._radius_km     = float(cfg.get("radius_km", 500.0))
        self._poll_interval = int(cfg.get("poll_interval_sec", 300))
        # How long a still-active park is remembered so it isn't re-announced
        # every poll — a fresh activation after this long counts as new again.
        self._seen_ttl = int(cfg.get("seen_ttl_sec", 6 * 3600))

        self._auto_adapters = cfg.get("auto_broadcast_adapters", [])
        self._auto_channel  = cfg.get("auto_broadcast_channel", "pota")

        self._auto_min_interval = int(cfg.get("auto_broadcast_min_interval_sec", 120))
        self._auto_max_per_hour = int(cfg.get("auto_broadcast_max_per_hour", 10))

        self._router = router
        self._db = None
        # activator@reference -> last-seen unix ts
        self._seen: dict[str, float] = {}
        self._hour_broadcast_ts: list[float] = []
        self._last_auto_broadcast: float = 0.0

        self._poll_task: asyncio.Task | None = None
        self._client: httpx.AsyncClient | None = None
        self._last_poll = None
        self._poll_count = 0
        self._broadcast_count = 0
        self._last_error = ""

    def set_db(self, db) -> None:
        self._db = db

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def start(self) -> None:
        if not self.enabled:
            log.info("PotaService: disabled in config")
            return
        self._client = httpx.AsyncClient(timeout=15.0, follow_redirects=True)
        self._poll_task = asyncio.create_task(self._poll_loop(), name="pota-poll")
        log.info("PotaService: started, radius=%.0fkm, interval=%ds", self._radius_km, self._poll_interval)

    async def stop(self) -> None:
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        if self._client:
            await self._client.aclose()

    async def trigger_poll(self) -> None:
        await self._poll()

    async def _poll_loop(self) -> None:
        await self._poll()
        try:
            while True:
                await asyncio.sleep(self._poll_interval)
                await self._poll()
        except asyncio.CancelledError:
            pass

    # ── Poll ──────────────────────────────────────────────────────────────

    async def _poll(self) -> None:
        if self._lat is None or self._lon is None:
            self._last_error = "no lat/lon configured (set pota_service.lat/lon or weather_service.nws_lat/lon)"
            log.debug("PotaService: %s — skipping poll", self._last_error)
            return
        self._poll_count += 1
        from datetime import datetime, timezone
        self._last_poll = datetime.now(timezone.utc)
        try:
            resp = await self._client.get(POTA_SPOT_URL)
            resp.raise_for_status()
            raw_spots = resp.json()
            self._last_error = ""
        except Exception as exc:
            self._last_error = f"{type(exc).__name__}: {exc}"
            log.warning("PotaService: poll failed: %s", self._last_error)
            return

        now_ts = time.time()
        # Expire stale "seen" entries so a genuinely fresh activation re-announces.
        self._seen = {k: t for k, t in self._seen.items() if now_ts - t < self._seen_ttl}

        for s in raw_spots:
            lat, lon = s.get("latitude"), s.get("longitude")
            if lat is None or lon is None:
                continue
            if _haversine_km(self._lat, self._lon, lat, lon) > self._radius_km:
                continue

            activator = s.get("activator", "")
            reference = s.get("reference", "")
            if not activator or not reference:
                continue
            key = f"{activator}@{reference}"
            if key in self._seen:
                self._seen[key] = now_ts   # still active — refresh, don't re-announce
                continue
            self._seen[key] = now_ts

            park_name = s.get("name") or s.get("parkName") or reference
            freq = s.get("frequency", "")
            mode = s.get("mode", "")
            body = f"\U0001f3de POTA: {activator} activating {reference} ({park_name}) — {freq} kHz {mode}".strip()

            msg = NormalizedMessage(
                source_adapter="pota-service",
                source_channel=self._auto_channel or "pota",
                from_id="POTA",
                from_display="POTA Spots",
                body=body,
                priority=Priority.NORMAL,
                raw={
                    "activator": activator, "reference": reference, "parkName": park_name,
                    "frequency": freq, "mode": mode, "lat": lat, "lon": lon,
                },
            )
            if self._router:
                await self._router._handle_inbound(msg)

            await self._maybe_auto_broadcast(key, body, now_ts)

    async def _maybe_auto_broadcast(self, key: str, body: str, now_ts: float) -> None:
        if not self._router:
            return
        skip_reason = None
        if now_ts - self._last_auto_broadcast < self._auto_min_interval:
            skip_reason = f"min interval {self._auto_min_interval}s"
        else:
            cutoff = now_ts - 3600
            self._hour_broadcast_ts = [t for t in self._hour_broadcast_ts if t > cutoff]
            if len(self._hour_broadcast_ts) >= self._auto_max_per_hour:
                skip_reason = f"hourly cap {self._auto_max_per_hour}/hr reached"

        if skip_reason:
            log.info("PotaService: skipping auto-broadcast for %s (%s)", key, skip_reason)
            return

        raw_hint = {"channel_name": self._auto_channel} if self._auto_channel else None
        await self._router.send(
            body=body[:200],
            adapter_names=self._auto_adapters or None,
            priority=Priority.NORMAL,
            raw=raw_hint,
        )
        self._last_auto_broadcast = now_ts
        self._hour_broadcast_ts.append(now_ts)
        self._broadcast_count += 1
        log.info("PotaService: auto-broadcast sent for %s → channel=%r adapters=%s (%d this hour)",
                  key, self._auto_channel or "(adapter default)", self._auto_adapters or "all",
                  len(self._hour_broadcast_ts))
        if self._db:
            import json as _json
            try:
                await self._db.set_kv("pota_broadcast_state", _json.dumps({
                    "last_auto": self._last_auto_broadcast,
                }))
            except Exception:
                pass

    # ── Status ────────────────────────────────────────────────────────────

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "radius_km": self._radius_km,
            "poll_interval_sec": self._poll_interval,
            "seen_ttl_sec": self._seen_ttl,
            "auto_broadcast_adapters": self._auto_adapters,
            "auto_broadcast_channel": self._auto_channel,
            "auto_broadcast_min_interval_sec": self._auto_min_interval,
            "auto_broadcast_max_per_hour": self._auto_max_per_hour,
            "seen_count": len(self._seen),
            "poll_count": self._poll_count,
            "broadcast_count_session": self._broadcast_count,
            "last_poll": self._last_poll.isoformat() if self._last_poll else None,
            "last_error": self._last_error,
        }
