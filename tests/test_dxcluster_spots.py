"""
tests/test_dxcluster_spots.py
-----------------------------
2026-10-11 audit: dxsummit.fi reports longitudes WEST-positive (Slovenia
-14, Massachusetts +71.65). The DX layer must negate them, filter by the
SPOTTER's distance from base ("what's heard near me"), never geocode to FCC
home addresses, and IOTA/BOTA must be worldwide.
"""

from unittest.mock import MagicMock

import httpx
from starlette.testclient import TestClient

FEED = [
    # Spotted from Massachusetts (west-positive 71.6) — near a Maine base
    {"dx_call": "HA3MGA", "de_call": "AE1P", "frequency": 14074.0, "info": "FT8",
     "time": "2026-10-11T15:20:00", "dx_country": "Hungary",
     "dx_latitude": 47.0, "dx_longitude": -18.0, "de_latitude": 42.4, "de_longitude": 71.6},
    # Spotted from Germany — must NOT appear in the near-me DX layer
    {"dx_call": "JA1XYZ", "de_call": "DL1ABC", "frequency": 21074.0, "info": "IOTA AS-007",
     "time": "2026-10-11T15:21:00", "dx_country": "Japan",
     "dx_latitude": 35.7, "dx_longitude": -139.7, "de_latitude": 50.0, "de_longitude": -8.0},
]


class _Resp:
    def raise_for_status(self):
        pass

    def json(self):
        return FEED


def _client(monkeypatch):
    from ech.api import app as app_mod
    app_mod._dxc_cache.clear()
    urls = []

    async def fake_get(self, url, *a, **k):
        urls.append(url)
        return _Resp()

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    state = MagicMock(_base_lat=44.16, _base_lon=-69.12)
    return TestClient(app_mod.create_app(MagicMock(), MagicMock(), ech_state=state)), urls


def test_dx_layer_fixes_longitude_and_filters_by_spotter(monkeypatch):
    c, urls = _client(monkeypatch)
    with c:
        d = c.get("/api/dxcluster/spots?radius_km=500").json()
    assert [s["dxCall"] for s in d["spots"]] == ["HA3MGA"]
    s = d["spots"][0]
    assert s["lon"] == 18.0 and s["spotterLon"] == -71.6     # sign corrected
    assert 0 < s["spotterKm"] < 500
    assert not any("callook" in u for u in urls)               # no home-address geocoding


def test_iota_is_worldwide(monkeypatch):
    c, _ = _client(monkeypatch)
    with c:
        d = c.get("/api/iota/spots").json()
    assert [(s["dxCall"], s["reference"]) for s in d["spots"]] == [("JA1XYZ", "AS-007")]
    assert d["spots"][0]["lon"] == 139.7
