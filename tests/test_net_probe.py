"""Network path probe (O168): traceroute parsing, hop geolocation, DB round trip."""
import json

import pytest

from ech.core.net_probe import NetProbeService, hop_location, parse_traceroute, path_signature

# Real traceroutes from the live server (2026-10-21), Montreal path trimmed
BOSTON = """traceroute to stosat-ndhm-01.sys.comcast.net.prod.hosts.ooklaserver.net (76.96.122.78), 20 hops max, 60 byte packets
 1  OpenWrt.lan (192.168.6.1)  0.434 ms  0.279 ms
 2  71.181.84.1 (71.181.84.1)  3.912 ms  3.836 ms
 3  7.l.burlvtma97w.vt.consolidated.net (64.222.213.131)  2.911 ms  2.780 ms
 4  38.104.52.78 (38.104.52.78)  7.167 ms  7.086 ms
 5  * *
 6  * comcast.bos01.atlas.cogentco.com (154.54.11.166)  6.048 ms
 7  be-1004-arsc1.woburn.ma.boston.comcast.net (68.86.90.61)  6.954 ms be-1005-arsc1.needham.ma.boston.comcast.net (68.86.90.65)  7.758 ms
 8  po-1-rur102.needham.ma.boston.comcast.net (96.108.44.222)  6.270 ms po-1-rur101.needham.ma.boston.comcast.net (68.85.88.114)  5.897 ms
 9  * *
10  * *
"""
MONTREAL = """traceroute to speedtest-mtl.beanfield.com.prod.hosts.ooklaserver.net (198.98.100.22), 20 hops max, 60 byte packets
 1  OpenWrt.lan (192.168.6.1)  0.595 ms  0.426 ms
 6  be8030.ccr22.alb02.atlas.cogentco.com (154.54.169.226)  9.302 ms be8029.ccr21.alb02.atlas.cogentco.com (154.54.169.222)  9.230 ms
 7  be2088.ccr21.ymq01.atlas.cogentco.com (154.54.43.17)  14.421 ms be2104.ccr22.ymq01.atlas.cogentco.com (154.54.43.21)  14.351 ms
10  po117.lsr01.800SquareVictoriaSt01.YUL.beanfield.com (72.15.49.90)  14.644 ms
"""


def test_parse_boston_stays_in_us():
    hops = parse_traceroute(BOSTON)
    assert len(hops) == 8                      # trailing silent hops dropped
    assert hops[4] == {"n": 5, "host": "", "ip": "", "rtt": None, "city": "", "country": ""}
    assert hops[5]["ip"] == "154.54.11.166" and hops[5]["rtt"] == 6.048   # "* host" partial reply
    assert hops[2]["city"] == "Burlington VT"  # Consolidated CLLI code
    assert {h["city"] for h in hops if h["city"]} == {"Burlington VT", "Boston"}
    assert not any(h["country"] == "CA" for h in hops)


def test_parse_montreal_flags_canada():
    hops = parse_traceroute(MONTREAL)
    assert [h["city"] for h in hops] == ["", "Albany", "Montreal", "Montreal"]
    assert [h["country"] for h in hops][-2:] == ["CA", "CA"]


@pytest.mark.parametrize("host,expected", [
    ("be2090.rcr21.ymq02.atlas.cogentco.com", ("Montreal", "CA")),
    ("be3260.ccr22.yyz02.atlas.cogentco.com", ("Toronto", "CA")),
    ("ae-1.r20.mtrlpq02.bell.ca", ("Montreal", "CA")),
    ("core1.example.ca", ("", "CA")),
    ("be2000.ccr41.jfk02.atlas.cogentco.com", ("New York", "US")),
    ("router.cogentco.com", ("", "")),       # "cogentco" must not read as a CLLI
    ("10.0.0.1", ("", "")),
])
def test_hop_location(host, expected):
    assert hop_location(host) == expected


def test_path_signature_changes_with_route():
    a = parse_traceroute(BOSTON)
    b = [dict(h) for h in a]
    b[3]["ip"] = "38.104.52.42"
    assert path_signature(a) and path_signature(a) != path_signature(b)
    assert path_signature([]) == ""


def test_expects_canada_default():
    assert NetProbeService._expects_canada({"label": "Montreal"})
    assert not NetProbeService._expects_canada({"label": "Boston"})
    assert NetProbeService._expects_canada({"label": "Toronto", "expect_canada": True})


def test_disabled_by_default():
    svc = NetProbeService({})
    assert not svc.enabled
    assert [t["label"] for t in svc.targets] == ["Boston", "Montreal"]


@pytest.mark.asyncio
async def test_db_round_trip():
    from datetime import datetime, timezone
    from ech.core.database import Database
    db = Database(":memory:")
    await db.connect()
    now = datetime.now(timezone.utc).isoformat()
    hops = parse_traceroute(BOSTON)
    await db.insert_net_probe({"ts": now, "target": "Boston", "kind": "speedtest",
                               "ping_ms": 6.7, "down_mbps": 938.5, "up_mbps": 934.5,
                               "server_host": "stosat-ndhm-01.sys.comcast.net"})
    await db.insert_net_probe({"ts": now, "target": "Boston", "kind": "traceroute",
                               "hops_json": json.dumps(hops), "hop_count": len(hops),
                               "via_canada": 0, "path_sig": path_signature(hops)})
    rows = await db.get_net_probe(hours=1)
    last = await db.last_net_probe("Boston", "speedtest")
    await db.close()
    assert [r["kind"] for r in rows] == ["speedtest", "traceroute"]
    assert last["server_host"] == "stosat-ndhm-01.sys.comcast.net"
    assert json.loads(rows[1]["hops_json"])[2]["city"] == "Burlington VT"


def test_netprobe_api_sends_hops_only_for_latest_and_changes(tmp_path):
    from unittest.mock import AsyncMock, MagicMock
    import yaml
    from starlette.testclient import TestClient
    from ech.api import app as app_mod

    a = parse_traceroute(BOSTON)
    b = [dict(h) for h in a]
    b[3]["ip"] = "38.104.52.42"
    mk = lambda ts, hops: {"ts": ts, "target": "Boston", "kind": "traceroute",
                           "hops_json": json.dumps(hops), "path_sig": path_signature(hops)}
    rows = [mk("2026-10-21T10:00:00+00:00", a), mk("2026-10-21T10:15:00+00:00", a),
            mk("2026-10-21T10:30:00+00:00", b), mk("2026-10-21T10:45:00+00:00", b),
            mk("2026-10-21T11:00:00+00:00", b)]
    db = MagicMock()
    db.get_net_probe = AsyncMock(return_value=rows)
    probe = NetProbeService({"network_probe": {"enabled": True}})
    cfg = tmp_path / "config.yaml"; cfg.write_text(yaml.safe_dump({}))
    app = app_mod.create_app(MagicMock(), db, net_probe=probe, config_path=str(cfg))
    with TestClient(app) as c:
        d = c.get("/api/analytics/netprobe", params={"hours": 24}).json()
    res = d["results"]
    assert d["status"]["enabled"] is True
    assert [("hops" in r) for r in res] == [True, False, True, False, True]  # first, change, latest
    assert [r["route_changed"] for r in res] == [False, False, True, False, False]
    assert all("hops_json" not in r for r in res)
