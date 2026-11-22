"""Server side of a few map overlays (operator request, 2026-11-22).

* Mold risk: no agency publishes outdoor mold forecasts, so this is an index
  computed from humidity and temperature on a grid around the base (Open-Meteo,
  free, no key): hours in the last 2 days and next 2 days with relative humidity
  80%+ at 50-90F, the conditions mold spores grow and release in. An estimate.
* Pollen: there is no free, keyless US pollen source. With a Google Pollen API
  key (config map_layers.google_pollen_api_key) the server proxies Google's
  pollen heatmap tiles so the key never reaches the browser.

Snow depth (NOAA NOHRSC), storm cells (IEM NEXRAD storm attributes), SPC storm
reports and NHC tropical tracks are loaded straight by the browser (map.html).
"""
from __future__ import annotations

import time

import httpx

from ech.core import identity

OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
POLLEN_TILE = "https://pollen.googleapis.com/v1/mapTypes/{kind}/heatmapTiles/{z}/{x}/{y}"
POLLEN_KINDS = {"tree": "TREE_UPI", "grass": "GRASS_UPI", "weed": "WEED_UPI"}
MOLD_LEVELS = [(10, "low"), (30, "moderate"), (55, "high"), (101, "very high")]

_mold_cache: dict[tuple, tuple[float, dict]] = {}


def mold_index(rh: list, temp_f: list) -> tuple[int, str]:
    """Percent of hours with RH >= 80% and 50-90F -> (index 0-100, level)."""
    pairs = [(h, t) for h, t in zip(rh, temp_f) if h is not None and t is not None]
    if not pairs:
        return 0, "unknown"
    n = sum(1 for h, t in pairs if h >= 80 and 50 <= t <= 90)
    idx = round(100 * n / len(pairs))
    return idx, next(lbl for lim, lbl in MOLD_LEVELS if idx < lim)


def grid_points(lat: float, lon: float, n: int = 7, dlat: float = 0.35, dlon: float = 0.5) -> list[tuple[float, float]]:
    half = n // 2
    return [(round(lat + (i - half) * dlat, 3), round(lon + (j - half) * dlon, 3))
            for i in range(n) for j in range(n)]


async def mold_grid(lat: float, lon: float, n: int = 7) -> dict:
    key = (round(lat, 2), round(lon, 2), n)
    hit = _mold_cache.get(key)
    if hit and time.time() - hit[0] < 3 * 3600:
        return hit[1]
    pts = grid_points(lat, lon, n)
    async with httpx.AsyncClient(headers={"User-Agent": identity.user_agent()}, timeout=25) as c:
        r = await c.get(OPEN_METEO, params={
            "latitude": ",".join(str(p[0]) for p in pts), "longitude": ",".join(str(p[1]) for p in pts),
            "hourly": "relative_humidity_2m,temperature_2m", "temperature_unit": "fahrenheit",
            "past_days": "2", "forecast_days": "2", "timezone": "auto"})
        r.raise_for_status()
        data = r.json()
    if isinstance(data, dict):
        data = [data]
    cells = []
    for (plat, plon), d in zip(pts, data):
        h = d.get("hourly") or {}
        idx, level = mold_index(h.get("relative_humidity_2m") or [], h.get("temperature_2m") or [])
        cells.append({"lat": plat, "lon": plon, "index": idx, "level": level})
    out = {"cells": cells, "dlat": 0.35, "dlon": 0.5, "generated": time.time(),
           "note": "estimate: share of hours with humidity 80%+ at 50-90F, past 2 days and next 2"}
    _mold_cache[key] = (time.time(), out)
    return out


async def pollen_tile(api_key: str, kind: str, z: int, x: int, y: int) -> bytes | None:
    t = POLLEN_KINDS.get(kind)
    if not t or not api_key:
        return None
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(POLLEN_TILE.format(kind=t, z=z, x=x, y=y), params={"key": api_key})
        return r.content if r.status_code == 200 else None
