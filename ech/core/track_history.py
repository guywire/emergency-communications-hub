"""Server-side position history so map tracks exist before the map is opened.

A background sampler walks every adapter's node list, and stores a
(node, time, lat, lon) point whenever a node has moved more than ~30 m since
its last stored point (and not more often than MIN_INTERVAL_*). Points are
pruned after ``keep_days`` (default 7). GET /api/map/tracks serves them.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time

log = logging.getLogger(__name__)

MOVE_THRESHOLD_M = 30.0
MIN_INTERVAL_S = 60.0           # per node
MIN_INTERVAL_FAST_S = 120.0     # aircraft (icao:) - they move constantly
SAMPLE_EVERY_S = 30.0
PRUNE_EVERY_S = 3600.0
MAX_POINTS_PER_NODE = 2000      # served downsampled beyond this


def _dist_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# What a node reported at the moment a track point was stored - shown when the
# operator clicks that point on the map. Scalars only, kept small.
_INFO_TOP = ("altitude", "snr", "rssi", "battery_level", "hops_away")
_INFO_META = ("altitude_ft", "speed_kts", "track_deg", "course_deg", "squawk", "flight",
              "nav_status", "speed_kmh", "course", "speed", "comment", "status")


def point_info(node: dict) -> str | None:
    info = {}
    for k in _INFO_TOP:
        v = node.get(k)
        if isinstance(v, (int, float, str)) and v != "":
            info[k] = v
    meta = node.get("meta") or {}
    if isinstance(meta, dict):
        for k in _INFO_META:
            v = meta.get(k)
            if isinstance(v, (int, float, str)) and v != "":
                info[k] = v[:80] if isinstance(v, str) else v
    return json.dumps(info, separators=(",", ":")) if info else None


def downsample(points: list, limit: int = MAX_POINTS_PER_NODE) -> list:
    """Evenly thin a [ts, lat, lon, ...] list to at most ``limit`` points, always
    keeping the first and last."""
    n = len(points)
    if n <= limit or limit < 2:
        return points
    step = (n - 1) / (limit - 1)
    return [points[round(i * step)] for i in range(limit)]


class TrackHistory:
    def __init__(self, router, db, config: dict | None = None):
        cfg = (config or {}).get("track_history", {}) or {}
        self._router = router
        self._db = db
        self.enabled = bool(cfg.get("enabled", True))
        self.keep_days = max(1, int(cfg.get("keep_days", 7)))
        self._last: dict[tuple, tuple] = {}   # (adapter,node_id) -> (ts, lat, lon)
        self._task: asyncio.Task | None = None
        self._last_prune = 0.0

    async def start(self) -> None:
        if not self.enabled or self._db is None:
            return
        self._task = asyncio.create_task(self._loop())
        log.info("TrackHistory: recording node positions, keeping %d day(s)", self.keep_days)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await self.sample_once()
                now = time.time()
                if now - self._last_prune > PRUNE_EVERY_S:
                    self._last_prune = now
                    await self._db.purge_positions(self.keep_days)
            except asyncio.CancelledError:
                raise
            except Exception as exc:   # never let history kill the service
                log.warning("TrackHistory: %s", exc)
            await asyncio.sleep(SAMPLE_EVERY_S)

    async def sample_once(self, now: float | None = None) -> int:
        """Record moved nodes; returns the number of points written."""
        now = time.time() if now is None else now
        rows = []
        for name in list(getattr(self._router, "_adapters", {}).keys()):
            try:
                nodes = await self._router.nodes_for(name)
            except Exception:
                continue
            for n in nodes:
                lat, lon, nid = n.get("lat"), n.get("lon"), n.get("node_id")
                if not nid or lat is None or lon is None or (lat == 0 and lon == 0):
                    continue
                key = (name, nid)
                prev = self._last.get(key)
                gap = MIN_INTERVAL_FAST_S if str(nid).lower().startswith("icao:") else MIN_INTERVAL_S
                if prev is not None:
                    if now - prev[0] < gap:
                        continue
                    if _dist_m(prev[1], prev[2], lat, lon) < MOVE_THRESHOLD_M:
                        continue
                self._last[key] = (now, lat, lon)
                rows.append((nid, name, now, lat, lon, point_info(n)))
        if rows:
            await self._db.record_positions(rows)
        return len(rows)
