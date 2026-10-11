"""
tests/test_hydro.py
-------------------
O124: NOAA NWPS river gauges (flood category, most severe first, radius
filter, -999 = no data) and CO-OPS water temperature, NOAA mocked.
"""

from unittest.mock import MagicMock

import httpx
from starlette.testclient import TestClient


def _gauge(lid, lat, lon, stage, cat, fcat="fcst_not_current"):
    return {"lid": lid, "name": lid + " River", "latitude": lat, "longitude": lon,
            "wfo": {"abbreviation": "GYX"},
            "status": {"observed": {"primary": stage, "primaryUnit": "ft", "secondary": -999,
                                    "secondaryUnit": "kcfs", "floodCategory": cat, "validTime": "2026-10-11T19:30:00Z"},
                       "forecast": {"primary": -999, "primaryUnit": "", "secondary": -999,
                                    "secondaryUnit": "", "floodCategory": fcat, "validTime": "0001-01-01T00:00:00Z"}}}


GAUGES = {"gauges": [
    _gauge("CALM1", 44.20, -69.10, 3.5, "no_flooding"),
    _gauge("HIGH1", 44.30, -69.30, 12.2, "minor"),
    _gauge("FAR01", 46.50, -68.00, 2.0, "major"),       # ~270 km — outside 80 km
]}
STATIONS = {"stations": [{"id": "8413320", "name": "Bar Harbor", "lat": 44.392, "lng": -68.205}]}
TEMP = {"data": [{"t": "2026-10-11 15:42", "v": "54.1"}]}


class _R:
    def __init__(self, d): self.d = d
    def raise_for_status(self): pass
    def json(self): return self.d


def test_river_gauges_and_water_temp(monkeypatch):
    from ech.api.app import create_app
    from ech.core import hydro
    hydro._gauge_cache.clear(); hydro._temp_cache.clear(); hydro._temp_stations.clear()
    hydro._temp_stations_at = 0.0

    async def fake_get(self, url, params=None, **k):
        if "nwps" in url:
            return _R(GAUGES)
        if "mdapi" in url:
            return _R(STATIONS)
        return _R(TEMP)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    app = create_app(MagicMock(), MagicMock(), ech_state=MagicMock(_base_lat=44.16, _base_lon=-69.12))
    with TestClient(app) as c:
        g = c.get("/api/rivers/gauges").json()
        w = c.get("/api/water/temperature").json()
    assert [x["lid"] for x in g["gauges"]] == ["HIGH1", "CALM1"]          # most severe first, far one dropped
    hi = g["gauges"][0]
    assert hi["observed"] == {"key": "minor", "rank": 3, "label": "Minor flooding"}
    assert hi["flow"] is None and hi["forecast_stage"] is None              # -999 → no data
    assert w["stations"][0]["water_temp_f"] == 54.1 and w["stations"][0]["water_temp_c"] == 12.3
