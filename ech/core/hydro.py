"""
ech/core/hydro.py
-----------------
River gauges with flood status, and coastal water temperature (O124).

* NOAA National Water Prediction Service (NWPS) gauges — current stage/flow
  and the observed + forecast FLOOD CATEGORY (no flooding / action / minor /
  moderate / major) for every river/tide gauge in a box around the base.
  The most emergency-relevant water data available: it says whether water
  is coming up, not just what's in it.
* NOAA CO-OPS water temperature at the nearest stations that actually have
  a temperature sensor (most tide-prediction points don't).

Both are polled on demand with short in-memory caches only — nothing is
written to disk (the live box is short on space).
"""

from __future__ import annotations

import asyncio
import logging
import math
import time

import httpx

from ech.core import identity

log = logging.getLogger(__name__)

_NWPS_GAUGES = "https://api.water.noaa.gov/nwps/v1/gauges"
_COOPS_STATIONS = "https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi/stations.json"
_COOPS_DATA = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"

_GAUGE_TTL = 600
_TEMP_TTL = 1800
_STATIONS_TTL = 24 * 3600

_gauge_cache: dict[tuple, tuple[float, list]] = {}
_temp_stations: list[tuple[str, str, float, float]] = []
_temp_stations_at = 0.0
_temp_cache: dict[str, tuple[float, dict | None]] = {}

# NWPS floodCategory → (rank for sorting/colour, human label). Unknown values
# pass through as-is rather than being guessed into a category.
FLOOD_CATEGORIES = {
    "major": (5, "Major flooding"),
    "moderate": (4, "Moderate flooding"),
    "minor": (3, "Minor flooding"),
    "action": (2, "Action stage (near flood)"),
    "no_flooding": (1, "No flooding"),
    "low_threshold": (1, "Below low threshold"),
    "not_defined": (0, "No flood stages defined"),
    "obs_not_current": (0, "Observation not current"),
    "out_of_service": (0, "Gauge out of service"),
    "fcst_not_current": (0, "No current forecast"),
}


def _bbox(lat: float, lon: float, radius_km: float) -> tuple[float, float, float, float]:
    dlat = radius_km / 111.0
    dlon = radius_km / (111.0 * max(math.cos(math.radians(lat)), 0.05))
    return lon - dlon, lat - dlat, lon + dlon, lat + dlat


def _num(v):
    return None if v in (None, -999, -9999) else v


def _category(raw: str | None) -> dict:
    key = (raw or "").strip()
    rank, label = FLOOD_CATEGORIES.get(key, (0, key.replace("_", " ") or "unknown"))
    return {"key": key, "rank": rank, "label": label}


async def river_gauges(lat: float, lon: float, radius_km: float = 80.0) -> dict:
    from ech.core.anomaly import _haversine_km
    key = (round(lat, 2), round(lon, 2), round(radius_km))
    cached = _gauge_cache.get(key)
    if cached and time.monotonic() - cached[0] < _GAUGE_TTL:
        return {"gauges": cached[1], "radius_km": radius_km, "cached": True}
    xmin, ymin, xmax, ymax = _bbox(lat, lon, radius_km)
    async with httpx.AsyncClient(timeout=20.0, headers={"User-Agent": identity.user_agent()}) as client:
        r = await client.get(_NWPS_GAUGES, params={
            "bbox.xmin": xmin, "bbox.ymin": ymin, "bbox.xmax": xmax, "bbox.ymax": ymax, "srid": "EPSG_4326"})
        r.raise_for_status()
        raw = r.json().get("gauges", [])
    out = []
    for g in raw:
        try:
            glat, glon = float(g["latitude"]), float(g["longitude"])
        except (KeyError, TypeError, ValueError):
            continue
        dist = _haversine_km(lat, lon, glat, glon)
        if dist > radius_km:
            continue
        obs = (g.get("status") or {}).get("observed") or {}
        fc = (g.get("status") or {}).get("forecast") or {}
        out.append({
            "lid": g.get("lid", ""), "name": g.get("name", ""), "lat": glat, "lon": glon,
            "distance_km": round(dist, 1),
            "stage": _num(obs.get("primary")), "stage_unit": obs.get("primaryUnit") or "",
            "flow": _num(obs.get("secondary")), "flow_unit": obs.get("secondaryUnit") or "",
            "observed_at": obs.get("validTime"),
            "observed": _category(obs.get("floodCategory")),
            "forecast": _category(fc.get("floodCategory")),
            "forecast_stage": _num(fc.get("primary")),
            "forecast_at": fc.get("validTime") if _num(fc.get("primary")) is not None else None,
            "wfo": (g.get("wfo") or {}).get("abbreviation", ""),
            "url": f"https://water.noaa.gov/gauges/{g.get('lid', '').lower()}",
        })
    out.sort(key=lambda x: (-max(x["observed"]["rank"], x["forecast"]["rank"]), x["distance_km"]))
    _gauge_cache[key] = (time.monotonic(), out)
    return {"gauges": out, "radius_km": radius_km, "cached": False}


async def _load_temp_stations(client: httpx.AsyncClient) -> None:
    global _temp_stations, _temp_stations_at
    if _temp_stations and time.monotonic() - _temp_stations_at < _STATIONS_TTL:
        return
    r = await client.get(_COOPS_STATIONS, params={"type": "watertemp"})
    r.raise_for_status()
    st = []
    for s in r.json().get("stations", []):
        try:
            st.append((str(s["id"]), str(s.get("name", "")), float(s["lat"]), float(s["lng"])))
        except (KeyError, TypeError, ValueError):
            continue
    if st:
        _temp_stations, _temp_stations_at = st, time.monotonic()


async def _latest_temp(client: httpx.AsyncClient, sid: str) -> dict | None:
    cached = _temp_cache.get(sid)
    if cached and time.monotonic() - cached[0] < _TEMP_TTL:
        return cached[1]
    r = await client.get(_COOPS_DATA, params={
        "product": "water_temperature", "date": "latest", "station": sid, "units": "english",
        "time_zone": "lst_ldt", "format": "json", "application": "SignalMatrix"})
    r.raise_for_status()
    data = (r.json().get("data") or [{}])[0]
    val = data.get("v")
    result = {"f": round(float(val), 1), "t": data.get("t")} if val not in (None, "") else None
    _temp_cache[sid] = (time.monotonic(), result)
    return result


async def water_temps(lat: float, lon: float, radius_km: float = 150.0, limit: int = 4) -> dict:
    from ech.core.anomaly import _haversine_km
    async with httpx.AsyncClient(timeout=20.0, headers={"User-Agent": identity.user_agent()}) as client:
        await _load_temp_stations(client)
        ranked = sorted(((_haversine_km(lat, lon, s[2], s[3]), s) for s in _temp_stations), key=lambda x: x[0])
        chosen = [(d, s) for d, s in ranked if d <= radius_km][:limit]
        temps = await asyncio.gather(*(_latest_temp(client, s[0]) for _, s in chosen), return_exceptions=True)
    out = []
    for (d, (sid, name, slat, slon)), tmp in zip(chosen, temps):
        if isinstance(tmp, Exception) or not tmp:
            continue
        out.append({"id": sid, "name": name, "lat": slat, "lon": slon, "distance_km": round(d, 1),
                    "water_temp_f": tmp["f"], "water_temp_c": round((tmp["f"] - 32) * 5 / 9, 1),
                    "observed_at": tmp["t"],
                    "url": f"https://tidesandcurrents.noaa.gov/stationhome.html?id={sid}"})
    return {"stations": out, "radius_km": radius_km}
