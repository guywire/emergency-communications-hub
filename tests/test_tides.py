"""
tests/test_tides.py
-------------------
/api/tides/nearby: nearest NOAA stations to base, next high/low and
rising/falling — with NOAA mocked (no network, nothing written to disk).
"""

from datetime import datetime, timedelta
from unittest.mock import MagicMock

import httpx
from starlette.testclient import TestClient


def test_tides_nearby(monkeypatch):
    from ech.api.app import create_app
    from ech.core import tides

    tides._stations.clear(); tides._pred_cache.clear(); tides._stations_at = 0.0
    now = datetime.now()
    f = lambda h: (now + timedelta(hours=h)).strftime("%Y-%m-%d %H:%M")
    preds = {"predictions": [{"t": f(-3), "v": "0.2", "type": "L"}, {"t": f(2), "v": "11.1", "type": "H"},
                             {"t": f(8), "v": "-0.4", "type": "L"}, {"t": f(14), "v": "10.6", "type": "H"}]}
    stations = {"stations": [
        {"id": "8415490", "name": "Rockland", "lat": 44.105, "lng": -69.101},
        {"id": "8418150", "name": "Portland", "lat": 43.658, "lng": -70.244},   # ~105 km — outside 60
    ]}

    class R:
        def __init__(self, d): self.d = d
        def raise_for_status(self): pass
        def json(self): return self.d

    async def fake_get(self, url, params=None, **k):
        return R(stations if "mdapi" in url else preds)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    app = create_app(MagicMock(), MagicMock(), ech_state=MagicMock(_base_lat=44.16, _base_lon=-69.12))
    with TestClient(app) as c:
        d = c.get("/api/tides/nearby").json()
    assert [s["name"] for s in d["stations"]] == ["Rockland"]
    s = d["stations"][0]
    assert s["rising"] is True and s["next_high"]["ft"] == 11.1 and s["next_low"]["ft"] == -0.4
    assert s["previous"]["type"] == "low" and len(s["upcoming"]) == 3
