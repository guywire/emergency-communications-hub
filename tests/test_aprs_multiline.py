"""APRS multiline objects (WXSVR/SPCSVR warning boxes) decode to polygons."""

import aprslib

from ech.core.aprs_hub import AprsHub, parse_multiline

SPEC_EXAMPLE = "SPCSVR>APRS:;SPCS1528z*262100z3500.00NS07730.00WWSvr TStormWatch #174 }e0]FgcBS6:W{QFSAA"


def test_spec_example_decodes_to_closed_polygon():
    shape, comment = parse_multiline("Svr TStormWatch #174 }e0]FgcBS6:W{QFSAA", 35.0, -77.5)
    assert comment == "Svr TStormWatch #174"
    assert shape["label"] == "Severe Thunderstorm Watch"
    ring = shape["geometry"]["coordinates"][0]
    assert ring[0] == ring[-1] and len(ring) == 5
    assert ring[:4] == [[-80.0, 34.2], [-76.3, 37.1], [-75.1, 35.5], [-78.4, 33.0]]


def test_plain_comment_untouched():
    assert parse_multiline("Shelter open {abc", 44.0, -69.0) == (None, "Shelter open {abc")


def test_hub_object_carries_shape():
    h = AprsHub()
    h._ingest_object(aprslib.parse(SPEC_EXAMPLE), "SPCSVR", "aprs-internet", 0, "object")
    ob = h.objects["SPCS1528z"]
    assert ob["shape"]["geometry"]["type"] == "Polygon" and ob["comment"] == "Svr TStormWatch #174"


def test_nws_shape_endpoint_fetches_missing_zones(tmp_path):
    """/api/aprs/nws_shape draws a bulletin anywhere: missing zones fetched now."""
    import time
    from unittest.mock import MagicMock
    import yaml
    from starlette.testclient import TestClient
    from ech.api import app as app_mod
    from ech.core import aprs_wx, nws_zones

    b = {"from": "NWS-WARN", "kind": "WARN", "event": "Flood Warning", "office": "GYX",
         "ugcs": ["MEZ025", "MEZ026"], "expires": None, "received_ts": time.time(), "severity": "Moderate"}
    b["id"] = "aprs:test:MEZ025:MEZ026"
    aprs_wx.store._items[("t",)] = b

    square = {"type": "Polygon", "coordinates": [[[-70, 44], [-69, 44], [-69, 45], [-70, 45], [-70, 44]]]}
    zones = nws_zones.ZoneShapeCache(None)
    zones.put("MEZ025", square)                       # cached; MEZ026 must be fetched
    fetched = []

    class Resp:
        def raise_for_status(self): pass
        def json(self): return {"geometry": square}

    class Client:
        async def get(self, url):
            fetched.append(url); return Resp()

    wx = MagicMock(); wx.zones = zones; wx._client = Client()
    cfg = tmp_path / "config.yaml"; cfg.write_text(yaml.safe_dump({}))
    app = app_mod.create_app(MagicMock(), MagicMock(), wx_service=wx, config_path=str(cfg))
    try:
        with TestClient(app) as c:
            d = c.get("/api/aprs/nws_shape", params={"id": b["id"]}).json()
            assert d["status"] == "ok" and d["missing"] == []
            assert len(d["geometry"]["coordinates"]) == 2
            assert any("MEZ026" in u for u in fetched) and not any("MEZ025" in u for u in fetched)
            assert c.get("/api/aprs/nws_shape", params={"id": "nope"}).json()["status"] == "error"
    finally:
        aprs_wx.store._items.pop(("t",), None)
