"""
ech/core/meshmapper_coverage.py
--------------------------------
MeshMapper Coverage API client — surfaces whether the hub's own location is a
confirmed two-way (BIDIR) RF link or a one-way link (RX/TX only) according to
aggregated community coverage data, to help explain "I hear distant nodes but
they don't hear me" reports.

https://wiki.meshmapper.net/coverage-api/
  GET https://meshmapper.net/coverage.php?key=<region-scoped key>
  Region is implied by the key (one key per region, no lat/lon query param).
  Response: {success, region, region_name, coverage_type_counts, bbox,
             grid_squares: [{grid_id, bounds:{south,west,north,east},
                              coverage_type, snr, effective, count, ...}]}
  coverage_type in {BIDIR, TX, RX, DISC, DEAD, DROP} — BIDIR = confirmed
  two-way, TX = we're heard but don't hear back, RX = we hear but aren't
  heard, DISC = discovery only, DEAD/DROP = no usable route.
  Rate limit: 100 requests/day per key — default poll interval kept well
  under that (30 min = 48/day).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import httpx

log = logging.getLogger(__name__)

COVERAGE_URL = "https://meshmapper.net/coverage.php"

COVERAGE_EXPLANATION = {
    "BIDIR": "Confirmed two-way link — other stations both hear you and are heard by you.",
    "TX":    "You are being heard, but this grid square hasn't confirmed hearing others back.",
    "RX":    "You are hearing other stations, but they don't appear to be hearing you back "
             "— classic asymmetric link (lower TX power / antenna gain / height than the far end).",
    "DISC":  "Discovery-only traffic seen — not enough packets yet to confirm a two-way link.",
    "DEAD":  "No usable route recently — link may be down or stations moved.",
    "DROP":  "Packets seen but no confirmed connection — likely too weak or too much noise.",
}


class MeshMapperCoverageService:
    """Polls the MeshMapper Coverage API and caches the grid for lookup."""

    def __init__(self, config: dict, router=None):
        cfg = config.get("meshmapper_coverage", {}) or {}
        self.enabled = bool(cfg.get("enabled", False))
        self._api_key = cfg.get("api_key", "")

        wx_cfg = config.get("weather_service", {}) or {}
        self._lat: float | None = cfg.get("lat") or wx_cfg.get("nws_lat")
        self._lon: float | None = cfg.get("lon") or wx_cfg.get("nws_lon")

        self._poll_interval = int(cfg.get("poll_interval_sec", 1800))  # 30 min = 48/day, under the 100/day cap

        self._router = router
        self._client: httpx.AsyncClient | None = None
        self._poll_task: asyncio.Task | None = None

        self._grid_squares: list[dict] = []
        self._region = ""
        self._region_name = ""
        self._type_counts: dict = {}
        self._last_poll: datetime | None = None
        self._poll_count = 0
        self._last_error = ""
        self._etag: str | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def start(self) -> None:
        if not self.enabled:
            log.info("MeshMapperCoverageService: disabled in config")
            return
        if not self._api_key:
            log.warning("MeshMapperCoverageService: enabled but no api_key configured — not starting")
            return
        self._client = httpx.AsyncClient(timeout=20.0, follow_redirects=True)
        self._poll_task = asyncio.create_task(self._poll_loop(), name="meshmapper-coverage-poll")
        log.info("MeshMapperCoverageService: started, interval=%ds", self._poll_interval)

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

    async def _poll(self) -> None:
        if not self._api_key:
            self._last_error = "no api_key configured"
            return
        self._poll_count += 1
        self._last_poll = datetime.now(timezone.utc)
        try:
            headers = {"If-None-Match": self._etag} if self._etag else {}
            resp = await self._client.get(COVERAGE_URL, params={"key": self._api_key}, headers=headers)
            if resp.status_code == 304:
                self._last_error = ""
                return
            resp.raise_for_status()
            data = resp.json()
            if not data.get("success"):
                self._last_error = f"API returned success=false: {data}"
                log.warning("MeshMapperCoverageService: %s", self._last_error)
                return
            self._etag = resp.headers.get("ETag") or self._etag
            self._region = data.get("region", "")
            self._region_name = data.get("region_name", "")
            self._type_counts = data.get("coverage_type_counts", {})
            self._grid_squares = data.get("grid_squares", [])
            self._last_error = ""
        except httpx.HTTPError as exc:
            self._last_error = f"HTTP error: {exc}"
            log.warning("MeshMapperCoverageService: %s", self._last_error)
        except Exception as exc:
            self._last_error = f"poll error: {exc}"
            log.error("MeshMapperCoverageService: %s", self._last_error)

    # ── Lookup ──────────────────────────────────────────────────────────

    # Cells only exist where packets were actually heard, so a point rarely
    # falls exactly inside one — fall back to the nearest cell center within
    # this radius (~1.1km) rather than reporting "no data" for a station that
    # has a perfectly good neighboring cell one grid square away.
    _NEAREST_FALLBACK_DEG = 0.01

    def cell_at(self, lat: float, lon: float) -> dict | None:
        """Return the grid square containing (lat, lon); if none contains it
        exactly, return the nearest cell within _NEAREST_FALLBACK_DEG. None if
        no data has been fetched yet or nothing is close enough."""
        for cell in self._grid_squares:
            b = cell.get("bounds", {})
            if b.get("south", 90) <= lat <= b.get("north", -90) and \
               b.get("west", 180) <= lon <= b.get("east", -180):
                return cell
        if not self._grid_squares:
            return None
        def _dist2(cell):
            b = cell.get("bounds", {})
            clat = (b.get("south", lat) + b.get("north", lat)) / 2
            clon = (b.get("west", lon) + b.get("east", lon)) / 2
            return (clat - lat) ** 2 + (clon - lon) ** 2
        nearest = min(self._grid_squares, key=_dist2)
        if _dist2(nearest) ** 0.5 <= self._NEAREST_FALLBACK_DEG:
            return nearest
        return None

    def my_coverage(self) -> dict | None:
        """Coverage cell for the hub's own configured base location."""
        if self._lat is None or self._lon is None:
            return None
        return self.cell_at(self._lat, self._lon)

    # ── Status / API payloads ──────────────────────────────────────────

    def status(self) -> dict:
        my_cell = self.my_coverage()
        my_type = my_cell.get("coverage_type") if my_cell else None
        return {
            "enabled": self.enabled,
            "configured": bool(self._api_key),
            "poll_count": self._poll_count,
            "last_poll": self._last_poll.isoformat() if self._last_poll else None,
            "last_error": self._last_error,
            "region": self._region,
            "region_name": self._region_name,
            "total_squares": len(self._grid_squares),
            "coverage_type_counts": self._type_counts,
            "my_coverage_type": my_type,
            "my_coverage_explanation": COVERAGE_EXPLANATION.get(my_type, ""),
            "my_cell": my_cell,
        }
