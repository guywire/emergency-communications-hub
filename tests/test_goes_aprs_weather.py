"""
tests/test_goes_aprs_weather.py
-------------------------------
O132: the map weather layer's three sources (NWS internet, local GOES
receiver EMWIN, APRS NWS bulletins), NWS zone shapes for zone-based alerts
such as Special Weather Statements, GOES full-disk reprojection, and APRS
symbols for the map.

Fixtures are real: the APRS lines were captured from APRS-IS (filter
g/NWS*) on 2026-10-12, and the product texts are real NWS products from
api.weather.gov/products, which is the text EMWIN carries.
"""

import math
import os
import time
from datetime import datetime, timezone

import pytest

from ech.core import aprs_wx, nws_zones
from ech.core.goes_receiver import (GoesReceiverService, latlon_to_scan, parse_emwin_product,
                                    reproject_full_disk, newest_file)

SVR_TEXT = """000
WUUS55 KABQ 120100
SVRABQ
NMC027-053-057-120130-
/O.NEW.KABQ.SV.W.0326.261012T0100Z-261012T0130Z/

BULLETIN - IMMEDIATE BROADCAST REQUESTED
Severe Thunderstorm Warning
National Weather Service Albuquerque NM
700 PM MDT Sun Oct 11 2026

&&

LAT...LON 3378 10605 3382 10614 3385 10656 3435 10655
      3432 10595 3412 10543 3365 10577
TIME...MOT...LOC 0100Z 194DEG 35KT 3397 10650 3394 10614 3382 10584

$$
"""

SPS_TEXT = """000
WWUS82 KFFC 121324
SPSFFC

Special Weather Statement
National Weather Service Peachtree City GA
924 AM EDT Mon Oct 12 2026

GAZ008-009-015-016-122000-
Fannin-Gilmer-Union-Lumpkin-
924 AM EDT Mon Oct 12 2026

...GUSTY WINDS IN THE NORTHEAST GEORGIA MOUNTAINS...

Wind gusts of 30 to 40 mph are expected.

$$
"""

WSW_TEXT = """000
WWUS45 KTFX 121131
WSWTFX

URGENT - WINTER WEATHER MESSAGE

MTZ305-306-121245-
/O.CAN.KTFX.WW.Y.0027.000000T0000Z-261012T1500Z/
Hill County-Northern Blaine County-

...WINTER WEATHER ADVISORY IS CANCELLED...

$$

MTZ303-304-311-121245-
/O.EXP.KTFX.WW.Y.0027.000000T0000Z-261012T1200Z/
Eastern Glacier-

$$
"""

REF = datetime(2026, 10, 12, 14, 0, tzinfo=timezone.utc)


# ── NWS zones ────────────────────────────────────────────────────────────

def test_expand_ugc_ranges_and_state_switch():
    codes, exp = nws_zones.expand_ugc("MEZ001>003-005-\nNHZ001-121630-")
    assert codes == ["MEZ001", "MEZ002", "MEZ003", "MEZ005", "NHZ001"]
    assert exp == "121630"


def test_simplify_keeps_closed_ring_and_drops_slivers():
    # 0.5° square with 200 collinear points along each side, plus a tiny islet
    side = [i / 400 for i in range(201)]
    ring = ([[-70 + t, 44.0] for t in side] + [[-69.5, 44 + t] for t in side[1:]]
            + [[-69.5 - t, 44.5] for t in side[1:]] + [[-70.0, 44.5 - t] for t in side[1:]])
    islet = [[-68.0, 44.0], [-67.999, 44.0], [-67.999, 44.001], [-68.0, 44.0]]
    out = nws_zones.simplify_geometry({"type": "MultiPolygon", "coordinates": [[ring], [islet]]})
    assert out["type"] == "MultiPolygon"
    assert len(out["coordinates"]) == 1                 # islet dropped
    simple = out["coordinates"][0][0]
    assert simple[0] == simple[-1]                      # still closed
    assert len(simple) <= 6                             # corners only
    assert nws_zones.geometry_bbox(out) == (-70.0, 44.0, -69.5, 44.5)


def test_zone_url_validation():
    assert nws_zones.ugc_from_url("https://api.weather.gov/zones/forecast/GAZ008") == "GAZ008"
    assert nws_zones.ugc_from_url("https://api.weather.gov/zones/county/MEC005") == "MEC005"
    assert nws_zones.ugc_from_url("https://evil.example/zones/forecast/GAZ008") is None
    assert nws_zones.ugc_url("MEC005").endswith("/zones/county/MEC005")
    assert nws_zones.ugc_url("ANZ150").endswith("/zones/forecast/ANZ150")


def test_region_areas_includes_neighbours_and_marine():
    assert nws_zones.region_areas("me") == ["ME", "NH", "AN"]


@pytest.mark.asyncio
async def test_zone_cache_fetch_persist_and_negative_cache(tmp_path):
    import httpx
    calls = []
    square = {"type": "Polygon", "coordinates": [[[-70, 44], [-69, 44], [-69, 45], [-70, 45], [-70, 44]]]}

    def handler(request):
        calls.append(str(request.url))
        if request.url.path.endswith("GAZ008"):
            return httpx.Response(200, json={"geometry": square})
        return httpx.Response(404)

    path = str(tmp_path / "zones.json")
    cache = nws_zones.ZoneShapeCache(path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await cache.ensure(["GAZ008", "GAZ999", "bad"], client) == 2
        assert cache.get("GAZ008")["type"] == "MultiPolygon"
        assert await cache.ensure(["GAZ008", "GAZ999"], client) == 0   # cached / negative-cached
    assert len(calls) == 2
    assert nws_zones.ZoneShapeCache(path).get("GAZ008") is not None    # persisted


# ── APRS NWS bulletins ───────────────────────────────────────────────────

def test_aprs_bulletin_parse_real_capture():
    b = aprs_wx.parse_bulletin("CAEFLS", "NWS-WARN", "122315z,FLOOD,SCC017,SCC063,SCC079", REF)
    assert b["office"] == "CAE" and b["product"] == "FLS"
    assert b["event"] == "Flood" and b["severity"] == "Severe"
    assert b["ugcs"] == ["SCC017", "SCC063", "SCC079"]
    assert b["expires"] == "2026-10-12T23:15:00+00:00"
    flash = aprs_wx.parse_bulletin("SLCFFW", "NWS-WARN", "121700z,FLASH_FLOOD,UTC025", REF)
    assert flash["event"] == "Flash Flood"
    assert aprs_wx.parse_bulletin("KN0O", "KN0O-9", "hello", REF) is None


def test_aprs_expiry_month_rollover():
    late = datetime(2026, 10, 31, 23, 0, tzinfo=timezone.utc)
    assert aprs_wx.parse_expiry("010200", late) == datetime(2026, 11, 1, 2, 0, tzinfo=timezone.utc)


def test_aprs_store_dedupes_twins_and_cancels():
    s = aprs_wx.AprsWxStore()
    future = datetime.now(timezone.utc).strftime("%d") + "2359"
    assert s.ingest("CAEFLS", "NWS-WARN", f"{future}z,FLOOD,SCC017,SCC079")
    assert s.ingest("CAEFLS", "NWS_WARN", f"{future}z,FLOOD,SCC017,SCC079")
    assert len(s.active()) == 1
    assert s.ingest("CAEFLS", "NWS-TEST", f"{future}z,TEST,SCC017")
    assert len(s.active()) == 1
    assert s.ingest("CAEFLS", "NWS-CANCL", f"{future}z,FLOOD,SCC079")
    assert s.active() == []


# ── GOES EMWIN products ──────────────────────────────────────────────────

def test_emwin_svr_polygon():
    [e] = parse_emwin_product(SVR_TEXT, REF)
    assert e["key"] == "KABQ.SV.W.0326" and e["action"] == "NEW"
    assert e["event"] == "Severe Thunderstorm Warning"
    ring = e["geometry"]["coordinates"][0]
    assert ring[0] == [-106.05, 33.78] and ring[0] == ring[-1] and len(ring) == 8
    assert e["expires"] == "2026-10-12T01:30:00+00:00"


def test_emwin_sps_zone_based():
    [e] = parse_emwin_product(SPS_TEXT, REF)
    assert e["event"] == "Special Weather Statement"
    assert e["ugcs"] == ["GAZ008", "GAZ009", "GAZ015", "GAZ016"]
    assert e["geometry"] is None
    assert e["headline"] == "GUSTY WINDS IN THE NORTHEAST GEORGIA MOUNTAINS"
    assert e["expires"] == "2026-10-12T20:00:00+00:00"


def test_emwin_scan_cancel_removes_event(tmp_path):
    svc = GoesReceiverService({"goes_receiver": {"enabled": True, "emwin_dir": str(tmp_path)},
                               "database": {"path": ":memory:"}})
    adv = WSW_TEXT.replace("/O.CAN.", "/O.NEW.").replace("/O.EXP.", "/O.NEW.") \
                  .replace("261012T1500Z", "301012T1500Z").replace("261012T1200Z", "301012T1200Z")
    (tmp_path / "a.TXT").write_text(adv)
    svc.scan_emwin()
    [a] = svc.emwin_alerts()
    assert sorted(a["ugcs"]) == ["MTZ303", "MTZ304", "MTZ305", "MTZ306", "MTZ311"]
    p = tmp_path / "b.TXT"
    p.write_text(WSW_TEXT)
    os.utime(p, (time.time() + 5, time.time() + 5))
    svc.scan_emwin()
    assert svc.emwin_alerts() == []


# ── GOES imagery ─────────────────────────────────────────────────────────

def test_scan_angles_match_pug_worked_example():
    """GOES-R PUG vol. 3 §5.1.2.8.2 worked example (sat at 75°W)."""
    import numpy as np
    x, y, vis = latlon_to_scan(np.array([33.846162]), np.array([-84.690932]), -75.0)
    assert math.isclose(float(x[0]), -0.024052, abs_tol=1e-6)
    assert math.isclose(float(y[0]), 0.095340, abs_tol=1e-6)
    assert bool(vis[0])


def test_reproject_places_marked_pixel_at_its_lat_lon():
    np = pytest.importorskip("numpy")
    Image = pytest.importorskip("PIL.Image")
    n = 2712
    arr = np.zeros((n, n), dtype=np.uint8)
    x, y, _ = latlon_to_scan(np.array([44.0]), np.array([-69.0]), -75.2)
    col = int(round((x[0] + 0.151872) / (0.303744 / n) - 0.5))
    row = int(round((0.151872 - y[0]) / (0.303744 / n) - 0.5))
    arr[row - 1:row + 2, col - 1:col + 2] = 255
    bounds = (40.0, -74.0, 48.0, -64.0)
    out = reproject_full_disk(Image.fromarray(arr, "L"), -75.2, bounds, 512)
    o = np.asarray(out)
    assert o[..., 3].min() == 255                      # whole box is on the disk
    ys, xs = np.nonzero(o[..., 0] == 255)
    # Expected pixel of 44N 69W in the Web Mercator output
    merc = lambda lat: math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))
    ex = (-69.0 - -74.0) / 10.0 * 512
    ey = (merc(48.0) - merc(44.0)) / (merc(48.0) - merc(40.0)) * out.size[1]
    assert abs(xs.mean() - ex) < 3 and abs(ys.mean() - ey) < 3


def test_newest_file_follows_newest_date_folder(tmp_path):
    old = tmp_path / "2026-10-11"
    new = tmp_path / "2026-10-12"
    old.mkdir()
    new.mkdir()
    (old / "GOES19_FD_CH13_20261011T230020Z.png").write_bytes(b"x")
    (new / "GOES19_FD_CH13_20261012T130020Z.png").write_bytes(b"x")
    os.utime(old, (time.time() - 3600, time.time() - 3600))
    assert newest_file(str(tmp_path), "*", (".png",)).endswith("20261012T130020Z.png")


def test_scan_imagery_rejects_non_full_disk(tmp_path):
    Image = pytest.importorskip("PIL.Image")
    Image.new("L", (400, 200)).save(tmp_path / "conus.png")

    class _Wx:
        _lat, _lon = 44.1, -69.1

    svc = GoesReceiverService({"goes_receiver": {"enabled": True, "imagery_dir": str(tmp_path),
                                                 "cache_dir": str(tmp_path / "c")},
                               "database": {"path": ":memory:"}}, wx_service=_Wx())
    os.makedirs(svc.cache_dir)
    svc.scan_imagery()
    assert "not a square full-disk" in svc.image_error
    assert svc.use_local_imagery() is False


# ── API merge + APRS adapter ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_map_alerts_endpoint_merges_sources_and_zones(monkeypatch, tmp_path):
    from httpx import AsyncClient, ASGITransport
    from ech.core.database import Database
    from ech.core.router import Router
    from ech.core.anomaly import AnomalyEngine
    from ech.core.weather import WeatherService
    from ech.api.app import create_app

    db = Database(":memory:")
    await db.connect()
    router = Router(db, anomaly_engine=AnomalyEngine({}))
    await router.start()

    ws = WeatherService({"weather_service": {"enabled": True, "map_alert_areas": ["GA", "SC"],
                                             "zone_cache_path": str(tmp_path / "z.json")}})
    sq = {"type": "MultiPolygon", "coordinates": [[[[-84, 34], [-83, 34], [-83, 35], [-84, 34]]]]}
    ws.zones.put("GAZ008", sq)
    ws._map_alerts = [{"id": "x", "event": "Special Weather Statement", "severity": "Moderate",
                       "geometry": None, "ugcs": ["GAZ008"], "source": "nws_api"}]
    ws._map_last_ok = time.time()

    goes = GoesReceiverService({"goes_receiver": {"enabled": True, "alerts_source": "auto"},
                                "database": {"path": ":memory:"}})
    goes._emwin_alerts = {"k": {"key": "KFFC.SPS.1", "event": "Special Weather Statement",
                                "severity": "Moderate", "headline": "", "office": "KFFC",
                                "expires": None, "geometry": None, "ugcs": ["GAZ008"]}}
    store = aprs_wx.AprsWxStore()
    future = datetime.now(timezone.utc).strftime("%d") + "2359"
    store.ingest("CAEFLS", "NWS-WARN", f"{future}z,FLOOD,SCC017")
    store.ingest("SLCFLS", "NWS-WARN", f"{future}z,FLOOD,UTC053")      # outside region
    monkeypatch.setattr(aprs_wx, "store", store)

    app = create_app(router, db, wx_service=ws, goes_service=goes)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        body = (await client.get("/api/weather/map_alerts")).json()
        assert sorted(a["source"] for a in body["alerts"]) == ["aprs", "nws_api"]  # GOES idle: internet fresh
        assert body["zones"] == {"GAZ008": sq}
        assert body["zones_missing"] == 1                                      # SCC017 not cached yet
        ws._map_last_ok = time.time() - 3600                                     # internet gone stale
        body = (await client.get("/api/weather/map_alerts")).json()
        assert sorted(a["source"] for a in body["alerts"]) == ["aprs", "goes_emwin"]
        st = (await client.get("/api/goes/status")).json()
        assert st["enabled"] is True and st["use_local_imagery"] is False
        assert (await client.get("/api/goes/image.png")).status_code == 404

    await router.stop()
    await db.close()


@pytest.mark.asyncio
async def test_aprs_is_bulletin_goes_to_store_and_symbol_kept(monkeypatch):
    from ech.adapters.aprs_is import APRSISAdapter
    store = aprs_wx.AprsWxStore()
    monkeypatch.setattr(aprs_wx, "store", store)
    a = APRSISAdapter({"name": "aprs-test", "callsign": "KN0O-9", "filter": "r/44.1/-69.1/60"})
    assert a._filter == "r/44.1/-69.1/60 g/NWS-*"
    queued = []

    async def _enqueue(msg):
        queued.append(msg)
    a._enqueue = _enqueue

    future = datetime.now(timezone.utc).strftime("%d") + "2359"
    await a._process_packet({"format": "message", "from": "CAEFLS", "addresse": "NWS-WARN",
                             "message_text": f"{future}z,FLOOD,SCC017"})
    assert queued == [] and "CAEFLS" not in a._nodes and len(store.active()) == 1

    await a._process_packet({"format": "uncompressed", "from": "W1AW-9", "latitude": 44.1,
                             "longitude": -69.1, "symbol_table": "/", "symbol": ">"})
    assert a._nodes["W1AW-9"].meta["aprs_symbol"] == "/>"
    assert APRSISAdapter({"callsign": "N0CALL", "nws_bulletins": False, "filter": "r/1/2/3"})._filter == "r/1/2/3"


def test_near_hub_radius_filter(tmp_path):
    from ech.core.weather import WeatherService
    ws = WeatherService({"weather_service": {"enabled": True, "nws_lat": 44.16, "nws_lon": -69.12,
                                             "zone_cache_path": str(tmp_path / "z.json")}})
    maine = {"type": "Polygon", "coordinates": [[[-69.5, 43.8], [-69.0, 43.8], [-69.0, 44.0], [-69.5, 43.8]]]}
    chesapeake = {"type": "MultiPolygon", "coordinates": [[[[-76.4, 37.0], [-76.0, 37.0], [-76.0, 37.5], [-76.4, 37.0]]]]}
    ws.zones.put("ANZ630", chesapeake)
    assert ws.near_hub(maine)
    assert not ws.near_hub(None, ["ANZ630"])
    assert ws.near_hub(None, ["ANZ999"])          # shape unknown → kept
