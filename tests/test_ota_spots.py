"""
tests/test_ota_spots.py
-----------------------
/api/ota/spots: On-the-Air programs recognised from real DX cluster
comments (2026-10-11), one combined layer; plain POTA excluded (own layer);
BOTA bunker references identified; /MM calls tagged as boats.
"""

from unittest.mock import MagicMock

import httpx
from starlette.testclient import TestClient


def _spot(call, info, lat=40.0, lon=-75.0, freq=14244.0):
    # dxsummit shape; longitude is west-positive there
    return {"dx_call": call, "de_call": "W1X", "frequency": freq, "info": info, "time": "t",
            "dx_country": "", "dx_latitude": lat, "dx_longitude": -lon,
            "de_latitude": 42.0, "de_longitude": 71.0}


FEED = [
    _spot("NJ9L", "WWFF KFF-1893"),
    _spot("KD2RD", "NA-026 Long Island, NY"),
    _spot("N4N", "SES POTA IOTA LH +"),
    _spot("ON5VL/P", "BOTA B/ON-2394", 50.8, 4.3),
    _spot("EA3JMA", "SOTA EA3/GI-050", 42.0, 2.0),
    _spot("EA3JMA", "COTA/BC-059", 42.0, 2.0, freq=7150.0),
    _spot("SP4VIT", "POTA LLOTA"),
    _spot("K1POTA", "POTA US-1234"),               # plain POTA → its own layer
    _spot("W1ABC/MM", "CQ CQ", 41.0, -70.0),
    _spot("K1NOPE", "CQ contest"),                  # no program
]


class _R:
    def raise_for_status(self): pass
    def json(self): return FEED


def test_ota_classification(monkeypatch):
    from ech.api import app as app_mod
    app_mod._dxc_cache.clear()

    async def fake_get(self, url, *a, **k):
        return _R()

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    with TestClient(app_mod.create_app(MagicMock(), MagicMock())) as c:
        d = c.get("/api/ota/spots").json()
    got = {(s["dxCall"], s["frequency"]): [(p["key"], p["ref"]) for p in s["programs"]] for s in d["spots"]}
    assert got[("NJ9L", 14244.0)] == [("wwff", "KFF-1893")]
    assert got[("KD2RD", 14244.0)] == [("iota", "NA-026")]
    assert [k for k, _ in got[("N4N", 14244.0)]] == ["iota", "light"]
    assert got[("ON5VL/P", 14244.0)] == [("bunker", "B/ON-2394")]
    assert got[("EA3JMA", 14244.0)] == [("sota", "EA3/GI-050")]
    assert got[("EA3JMA", 7150.0)][0][0] == "cota"
    assert got[("SP4VIT", 14244.0)] == [("llota", "")]
    assert got[("W1ABC/MM", 14244.0)] == [("boat", "")]
    assert ("K1POTA", 14244.0) not in got and ("K1NOPE", 14244.0) not in got
