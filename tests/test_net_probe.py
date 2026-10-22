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


def test_path_signature_ignores_ecmp_but_not_city_changes():
    a = parse_traceroute(BOSTON)
    ecmp = [dict(h) for h in a]
    ecmp[3]["ip"] = "38.104.52.42"           # load-balanced sibling router
    assert path_signature(a) == path_signature(ecmp)
    via_mtl = [dict(h) for h in a]
    via_mtl.insert(4, {"n": 5, "host": "be2090.rcr21.ymq02.atlas.cogentco.com", "ip": "154.54.45.118",
                       "rtt": 15.1, "city": "Montreal", "country": "CA"})
    assert path_signature(a) != path_signature(via_mtl)
    assert path_signature(a, "yul") != path_signature(a, "iad")   # Google site is part of it
    assert path_signature([]) == ""


def test_google_dns_site_lookup():
    from ech.core.net_probe import google_site_for, parse_google_locations, site_city
    # Shape of `dig +short TXT locations.publicdns.goog` (live 2026-10-21)
    txt = '"34.64.0.0/24 icn" "172.217.43.0/26 yul" "172.217.45.0/25 yul" "172.253.238.128/25 yul" ' \
          '"172.253.8.0/25 iad" "2607:f8b0:4020::/48 yul"'
    nets = parse_google_locations(txt)
    assert len(nets) == 6
    assert google_site_for(["172.217.45.17", "172.253.238.157", "172.217.43.27"], nets) == "yul"
    assert google_site_for(["172.253.8.9"], nets) == "iad"
    assert google_site_for(["10.0.0.1", "nonsense"], nets) == ""
    assert site_city("yul") == ("Montreal", "CA") and site_city("BOS") == ("Boston", "US")
    assert site_city("cmh") == ("", "")


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
    b.insert(4, {"n": 5, "host": "be2090.rcr21.ymq02.atlas.cogentco.com", "ip": "154.54.45.118",
                 "rtt": 15.1, "city": "Montreal", "country": "CA"})
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


def test_validate_config():
    from ech.core.net_probe import validate_config
    ok = validate_config({"enabled": True, "interval_min": "30", "speedtest_interval_min": 0,
                          "targets": [{"label": "Boston", "server_id": "1774"},
                                      {"label": "Montreal", "server_id": 16045, "expect_canada": True}]})
    assert ok == {"enabled": True, "interval_min": 30.0, "speedtest_interval_min": 0.0,
                  "targets": [{"label": "Boston", "server_id": 1774},
                              {"label": "Montreal", "server_id": 16045, "expect_canada": True}],
                  "web_checks": []}
    g = validate_config({"interval_min": 30, "targets": [{"label": "Google DNS", "trace_host": "8.8.8.8", "server_id": ""}],
                         "web_checks": [{"label": "Maine Archives", "url": "https://digitalarchives.maine.gov/view/288672/sunlit-forest?offset=0&limit=10"}]})
    assert g["targets"] == [{"label": "Google DNS", "server_id": None, "trace_host": "8.8.8.8"}]
    assert g["web_checks"][0]["label"] == "Maine Archives"
    bad = [
        {"interval_min": 2, "targets": [{"label": "B", "server_id": 1}]},
        {"interval_min": 30, "speedtest_interval_min": 5, "targets": [{"label": "B", "server_id": 1}]},
        {"interval_min": 30, "targets": []},
        {"interval_min": 30, "targets": [{"label": "<b>", "server_id": 1}]},
        {"interval_min": 30, "targets": [{"label": "B", "server_id": "x"}]},
        {"interval_min": 30, "targets": [{"label": "B", "server_id": 1}, {"label": "b", "server_id": 2}]},
        {"interval_min": 30, "targets": [{"label": "B", "trace_host": "-oFoo"}]},      # option injection
        {"interval_min": 30, "targets": [{"label": "B", "trace_host": "a b"}]},
        {"interval_min": 30, "targets": [{"label": "B"}], "web_checks": [{"label": "W", "url": "file:///etc/passwd"}]},
        {"interval_min": 30, "targets": [{"label": "B"}], "web_checks": [{"label": "b", "url": "https://x.org"}]},
    ]
    for b in bad:
        with pytest.raises(ValueError):
            validate_config(b)


@pytest.mark.asyncio
async def test_apply_config_starts_and_stops_loop():
    from ech.core.net_probe import validate_config
    svc = NetProbeService({})
    assert svc.interval_min == 30 and svc._task is None
    cfg = validate_config({"enabled": True, "interval_min": 60, "speedtest_interval_min": 0,
                           "targets": [{"label": "Boston", "server_id": 1774}]})
    svc._hosts["Boston"] = "old.host"
    await svc.apply_config(cfg)
    assert svc._task is not None and not svc._task.done()
    assert svc._hosts == {"Boston": "old.host"}          # same server ID keeps its host
    await svc.apply_config({**cfg, "targets": [{"label": "Boston", "server_id": 99}]})
    assert "Boston" not in svc._hosts                     # re-pointed target re-learns it
    await svc.apply_config({**cfg, "enabled": False})
    assert svc._task is None and not svc.enabled


def test_netprobe_config_endpoint_saves_yaml(tmp_path, monkeypatch):
    from unittest.mock import MagicMock
    import yaml
    from starlette.testclient import TestClient
    from ech.api import app as app_mod

    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump({"logging": {"level": "INFO"}}))
    monkeypatch.setattr(app_mod.Path, "exists",
                        lambda self: False if str(self).replace("\\", "/") == "/etc/ech/config.yaml"
                        else __import__("os").path.exists(self))
    probe = NetProbeService({})
    app = app_mod.create_app(MagicMock(), MagicMock(), net_probe=probe, config_path=str(cfg))
    body = {"enabled": False, "interval_min": 45, "speedtest_interval_min": 0,
            "targets": [{"label": "Boston", "server_id": 1774, "expect_canada": False}]}
    with TestClient(app) as c:
        assert c.post("/api/analytics/netprobe/config", json=body).json() == {"status": "ok", "saved": True}
        got = c.get("/api/analytics/netprobe/config").json()
        bad = c.post("/api/analytics/netprobe/config", json={**body, "interval_min": 1}).json()
    assert got["interval_min"] == 45 and got["targets"][0]["server_id"] == 1774
    assert bad["status"] == "error"
    saved = yaml.safe_load(cfg.read_text())
    assert saved["logging"] == {"level": "INFO"}
    assert saved["network_probe"]["interval_min"] == 45 and saved["network_probe"]["enabled"] is False


@pytest.mark.asyncio
async def test_web_check_records_timings_and_cloudflare_colo(monkeypatch):
    svc = NetProbeService({"network_probe": {"web_checks": [{"label": "Archives", "url": "https://example.org/p?x=1"}]}})
    svc.curl_bin = __file__          # any existing file: _run is stubbed below
    calls, saved = [], []

    async def fake_run(argv, timeout):
        calls.append(argv)
        if argv[-1].endswith("/cdn-cgi/trace"):
            return 0, "fl=1\nh=example.org\ncolo=YUL\nloc=US\n", ""
        return 0, json.dumps({"time_namelookup": 0.0145, "time_connect": 0.0201, "time_appconnect": 0.2422,
                              "time_starttransfer": 0.2542, "time_total": 0.2552, "http_code": 403,
                              "size_download": 5628}), ""

    class DB:
        async def insert_net_probe(self, row): saved.append(row)

    monkeypatch.setattr(svc, "_run", fake_run)
    svc._db = DB()
    await svc._web_check(svc.web_checks[0])
    row = saved[0]
    assert row["kind"] == "web" and row["final_rtt_ms"] == 255.2 and row["ping_ms"] == 254.2
    assert row["server"] == "Cloudflare YUL (Montreal)" and row["via_canada"] == 1
    d = json.loads(row["hops_json"])
    assert d["code"] == 403 and d["dns"] == 14.5 and d["colo"] == "YUL"
    assert calls[0][-2:] == ["--", "https://example.org/p?x=1"]          # URL never parsed as an option
    assert calls[1][-1] == "https://example.org/cdn-cgi/trace"


@pytest.mark.asyncio
async def test_trace_to_1111_reports_cloudflare_colo(monkeypatch):
    svc = NetProbeService({"network_probe": {"targets": [{"label": "Cloudflare DNS", "trace_host": "1.1.1.1"}]}})
    svc.traceroute_bin = svc.curl_bin = __file__
    saved = []
    trace = """traceroute to 1.1.1.1 (1.1.1.1), 16 hops max, 60 byte packets
 1  OpenWrt.lan (192.168.6.1)  0.206 ms  0.172 ms
 3  7.l.burlvtma97w.vt.consolidated.net (64.222.213.131)  2.821 ms  2.815 ms
 6  172.68.52.15 (172.68.52.15)  5.997 ms 172.68.52.17 (172.68.52.17)  5.283 ms
 7  one.one.one.one (1.1.1.1)  5.240 ms  5.331 ms
"""

    async def fake_run(argv, timeout):
        if argv[-1] == "https://1.1.1.1/cdn-cgi/trace":
            return 0, "fl=1\ncolo=BOS\nloc=US\n", ""
        return 0, trace, ""

    class DB:
        async def insert_net_probe(self, row): saved.append(row)
        async def last_net_probe(self, *a): return None

    monkeypatch.setattr(svc, "_run", fake_run)
    svc._db = DB()
    await svc._trace(svc.targets[0])
    row = saved[0]
    assert row["server"] == "Cloudflare BOS (Boston)"
    assert row["via_canada"] == 0 and row["final_rtt_ms"] == 5.24


def test_traceroute_only_targets():
    from ech.core.net_probe import validate_config
    cfg = validate_config({"interval_min": 30, "targets": [
        {"label": "Cloudflare DNS", "trace_host": "1.1.1.1", "speedtest": True},
        {"label": "Google DNS", "trace_host": "8.8.8.8", "speedtest": False}]})
    assert cfg["targets"][0] == {"label": "Cloudflare DNS", "server_id": None, "trace_host": "1.1.1.1"}
    assert cfg["targets"][1]["speedtest"] is False
    with pytest.raises(ValueError):   # nothing to trace without a speed test to learn the host from
        validate_config({"interval_min": 30, "targets": [{"label": "X", "speedtest": False}]})


@pytest.mark.asyncio
async def test_run_once_skips_speedtest_for_trace_only(monkeypatch):
    svc = NetProbeService({"network_probe": {"targets": [
        {"label": "Cloudflare DNS", "trace_host": "1.1.1.1"},
        {"label": "Google DNS", "trace_host": "8.8.8.8", "speedtest": False}]}})
    svc.speedtest_bin = __file__
    traced, tested = [], []

    async def fake_trace(t): traced.append(t["label"])
    async def fake_speed(t): tested.append(t["label"])
    monkeypatch.setattr(svc, "_trace", fake_trace)
    monkeypatch.setattr(svc, "_speedtest", fake_speed)
    await svc.run_once(force_speed=True)
    assert traced == ["Cloudflare DNS", "Google DNS"] and tested == ["Cloudflare DNS"]
