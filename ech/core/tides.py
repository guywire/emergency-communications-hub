"""
ech/core/tides.py
-----------------
Nearby NOAA tide predictions for the map's Tides layer.

* Station list: NOAA CO-OPS metadata API (all ~3,500 tide-prediction
  stations). The response is ~2 MB, so only id/name/lat/lon are kept, in
  memory, refreshed daily — nothing is written to disk (the live box is short
  on space).
* Predictions: CO-OPS datagetter, high/low ("hilo") times, MLLW datum, local
  station time, cached per station for an hour.

The mesh bot's `tide` command uses one configured station; this serves the
nearest few stations to the base position instead.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta

import httpx
from ech.core import identity

log = logging.getLogger(__name__)

_STATIONS_URL = "https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi/stations.json"
_DATA_URL = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"
_STATIONS_TTL = 24 * 3600
_PRED_TTL = 3600

_stations: list[tuple[str, str, float, float]] = []
_stations_at = 0.0
_pred_cache: dict[str, tuple[float, list[dict]]] = {}
_lock = asyncio.Lock()


def _haversine_km(lat1, lon1, lat2, lon2) -> float:
    from ech.core.anomaly import _haversine_km as h
    return h(lat1, lon1, lat2, lon2)


async def _load_stations(client: httpx.AsyncClient) -> None:
    global _stations, _stations_at
    if _stations and time.monotonic() - _stations_at < _STATIONS_TTL:
        return
    r = await client.get(_STATIONS_URL, params={"type": "tidepredictions"})
    r.raise_for_status()
    out = []
    for s in r.json().get("stations", []):
        try:
            out.append((str(s["id"]), str(s.get("name", "")), float(s["lat"]), float(s["lng"])))
        except (KeyError, TypeError, ValueError):
            continue
    if out:
        _stations, _stations_at = out, time.monotonic()
        log.info("Tides: loaded %d NOAA tide-prediction stations", len(out))


async def _predictions(client: httpx.AsyncClient, station: str) -> list[dict]:
    cached = _pred_cache.get(station)
    if cached and time.monotonic() - cached[0] < _PRED_TTL:
        return cached[1]
    begin = (datetime.now() - timedelta(hours=12)).strftime("%Y%m%d %H:%M")
    r = await client.get(_DATA_URL, params={
        "product": "predictions", "application": "SignalMatrix", "begin_date": begin,
        "range": "60", "datum": "MLLW", "station": station, "time_zone": "lst_ldt",
        "units": "english", "interval": "hilo", "format": "json"})
    r.raise_for_status()
    preds = [{"t": p["t"], "ft": round(float(p["v"]), 1), "type": "high" if p.get("type") == "H" else "low"}
             for p in r.json().get("predictions", []) if p.get("t") and p.get("v") not in (None, "")]
    _pred_cache[station] = (time.monotonic(), preds)
    return preds


def _summarise(preds: list[dict], now: datetime) -> dict:
    """Next high/low, the previous event, and whether the tide is rising."""
    def ts(p):
        return datetime.strptime(p["t"], "%Y-%m-%d %H:%M")
    past = [p for p in preds if ts(p) <= now]
    future = [p for p in preds if ts(p) > now]
    nxt_high = next((p for p in future if p["type"] == "high"), None)
    nxt_low = next((p for p in future if p["type"] == "low"), None)
    prev = past[-1] if past else None
    rising = None
    if future:
        rising = future[0]["type"] == "high"
    return {"next_high": nxt_high, "next_low": nxt_low, "previous": prev,
            "rising": rising, "upcoming": future[:4]}


async def nearby_tides(lat: float, lon: float, radius_km: float = 60.0, limit: int = 6) -> dict:
    async with _lock:
        async with httpx.AsyncClient(timeout=20.0, headers={"User-Agent": identity.user_agent()}) as client:
            await _load_stations(client)
            ranked = sorted(((_haversine_km(lat, lon, s[2], s[3]), s) for s in _stations),
                            key=lambda x: x[0])
            chosen = [(d, s) for d, s in ranked if d <= radius_km][:limit]
            results = await asyncio.gather(*(_predictions(client, s[0]) for _, s in chosen),
                                           return_exceptions=True)
    # NOAA returns station-local time; the stations shown are all near the
    # base, so the server's local clock is the right "now" to compare against.
    now = datetime.now()
    stations = []
    for (dist, (sid, name, slat, slon)), preds in zip(chosen, results):
        if isinstance(preds, Exception) or not preds:
            continue
        stations.append({"id": sid, "name": name, "lat": slat, "lon": slon,
                         "distance_km": round(dist, 1), **_summarise(preds, now),
                         "noaa_url": f"https://tidesandcurrents.noaa.gov/noaatidepredictions.html?id={sid}"})
    return {"stations": stations, "radius_km": radius_km, "datum": "MLLW", "units": "ft"}
