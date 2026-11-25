"""AREDN services: sorting the mesh's services into kinds and checking them (data made up)."""
import asyncio

import pytest

from ech.core import aredn_services as asv

HOSTS = [{"name": "N0SVC-NODE", "ip": "10.1.0.1"}, {"name": "n0svc-adsb", "ip": "10.1.0.7"},
         {"name": "n0svc-bbs", "ip": "10.1.0.8"}]


def svc(name, link="", ip="10.1.0.1"):
    return {"name": name, "link": link, "ip": ip}


@pytest.mark.parametrize("s, cat", [
    (svc("ADS-B Plane Map", "http://n0svc-adsb:8080/"), "adsb"),
    (svc("AIS-Catcher - Ship Tracking [map]", "http://x:8100/"), "ais"),
    (svc("N0SVC GOES [weather]", "http://x/"), "satellite"),
    (svc("Yard [camera]", "http://cam/"), "camera"),
    (svc("AllStar 12345 Supermon [server]", "http://x/supermon"), "radio"),
    (svc("BPQ BBS", "http://n0svc-bbs:8008/"), "packet"),
    (svc("N0SVC Winlink Post Office", "winlink://n0svc-bbs:8772/"), "winlink"),
    (svc("N0SVC NTP [time]", ""), "time"),
    (svc("Meshtastic Web UI App", "http://x/"), "meshradio"),
    (svc("Tower site [solar]", "http://x/"), "power"),
    (svc("MeshChat [chat]", "http://x:8080/meshchat"), "chat"),
    (svc("Wiki [wiki]", "http://x/"), "wiki"),
    (svc("Gateway [internet]", "http://x/"), "internet"),
    (svc("Tickets CAD", "http://x/tickets/"), "dispatch"),
    (svc("Linn County EOC", "http://x/"), "eoc"),
    (svc("KC6EOC-EOC-FileBrowser", "http://x/"), "files"),          # the more specific kind wins
    (svc("NextCloud", "http://x/"), "files"),
    (svc("Asterisk PBX", "http://x/"), "voip"),
    (svc("MCS7-Etherpad", "http://x/"), "collab"),
    (svc("N2KGC Hamclock", "http://x/"), "hamtools"),
    (svc("Uptime Kuma", "http://x:3001/"), "status"),
    (svc("KD6WXC MeshMap", "http://x/"), "status"),
    (svc("IPERF & IPERF3", "http://x/"), "speedtest"),
    (svc("Orcutt Hill West Edgeswitch", "http://x/"), "infra"),
    (svc("Files [weather]", "http://x/"), "weather"),                # the owner's tag wins
    (svc("Shack server [cad]", "http://x/"), "dispatch"),
    (svc("Some server [server]", "http://x/"), "other"),
])
def test_categorize(s, cat):
    assert asv.categorize(s) == cat


@pytest.mark.parametrize("name", ["Shack [phone]", "503-4409", "Meshphone 405-2003", "10.198.104.60 x6225"])
def test_phones_are_left_to_the_meshphone_map(name):
    assert asv.categorize(svc(name)) is None


def test_feed_paths_and_counts():
    paths = asv.feed_paths("http://n0svc-adsb:8080/tar1090/", "adsb")
    assert paths[0] == "/tar1090/data/aircraft.json" and "/data/aircraft.json" in paths
    assert asv.feed_paths("http://x:8100/", "ais")[0] == "/api/ships.json"
    assert asv.feed_count("adsb", {"now": 1, "aircraft": [{}, {}]}) == 2
    assert asv.feed_count("adsb", {"planes": []}) is None
    assert asv.feed_count("ais", {"ships": [{}]}) == 1 and asv.feed_count("ais", [{}, {}]) == 2


class _Resp:
    def __init__(self, status, ctype, data=None):
        self.status_code, self.headers, self._data = status, {"content-type": ctype}, data
        self.text = "{" if data is not None else ""
        self.url = "http://x/"

    def json(self):
        return self._data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def aiter_bytes(self):
        yield b"<html><title>tar1090</title></html>"


class _Client:
    """Answers the page with HTML and /data/aircraft.json with 3 aircraft."""
    def __init__(self):
        self.gets = []

    def stream(self, method, url, **kw):
        return _Resp(200, "text/html")

    async def get(self, url, **kw):
        self.gets.append(url)
        if url.endswith("/data/aircraft.json") and "tar1090" not in url:
            return _Resp(200, "application/json", {"aircraft": [{}, {}, {}]})
        return _Resp(404, "text/html")


def test_adsb_receiver_feed_is_found():
    c = _Client()
    out = asyncio.run(asv.ArednServices()._check(c, svc("ADS-B", "http://n0svc-adsb:8080/"), HOSTS, "adsb"))
    assert out["status"] == "open" and out["category"] == "adsb"
    assert out["feed"] == {"path": "/data/aircraft.json", "count": 3}
    assert all(u.startswith("http://10.1.0.7:8080/") for u in c.gets)          # by IP: no mesh DNS needed


def test_non_web_service_gets_a_tcp_check():
    async def run():
        srv = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        try:
            hosts = HOSTS + [{"name": "local-bbs", "ip": "127.0.0.1"}]
            up = await asv.ArednServices()._check(None, svc("BBS", f"telnet://local-bbs:{port}/"), hosts, "packet")
        finally:
            srv.close()
        listed = await asv.ArednServices()._check(None, svc("NTP [time]"), HOSTS, "time")
        return up, listed
    up, listed = asyncio.run(run())
    assert up["status"] == "open" and up["port"] and up["href"] == ""
    assert listed["status"] == "listed"


@pytest.mark.asyncio
async def test_services_json_and_aredn_alias():
    from ech.core.database import Database
    from ech.core.router import Router
    from ech.core.anomaly import AnomalyEngine
    from ech.api.app import create_app
    from httpx import AsyncClient, ASGITransport
    db = Database(":memory:")
    await db.connect()
    app = create_app(Router(db, anomaly_engine=AnomalyEngine({})), db)
    asv.service.data = {"generated_at": 1.0, "summary": {}, "categories": [],
                        "services": [{"name": "BPQ BBS", "category": "packet"}]}
    try:
        async with AsyncClient(transport=ASGITransport(app=app, client=("10.99.0.86", 5000)), base_url="http://t") as c:
            r = await c.get("/meshphone/services.json")
            assert r.status_code == 200 and r.json()["services"][0]["category"] == "packet"
            r = await c.get("/aredn/")
            assert r.status_code == 302 and r.headers["location"] == "/meshphone/?show=services"
    finally:
        asv.service.data = None
        await db.close()


def test_time_servers_get_an_ntp_query():
    async def run():
        loop = asyncio.get_event_loop()

        class Ntp(asyncio.DatagramProtocol):
            stratum = 2

            def connection_made(self, t):
                self.t = t

            def datagram_received(self, data, addr):
                assert data[0] == 0x1B and len(data) == 48
                self.t.sendto(bytes([0x1C, Ntp.stratum]) + 46 * b"\0", addr)
        t, _ = await loop.create_datagram_endpoint(Ntp, local_addr=("127.0.0.1", 0))
        port = t.get_extra_info("sockname")[1]
        try:
            hosts = HOSTS + [{"name": "local-ntp", "ip": "127.0.0.1"}]
            ok = await asv.ArednServices()._check(None, svc("NTP [time]", f"ntp://local-ntp:{port}"), hosts, "time")
            Ntp.stratum = 16
            unsynced = await asv.ntp_check("127.0.0.1", port, timeout=1)
        finally:
            t.close()
        silent = await asv.ntp_check("127.0.0.1", port, timeout=0.3, tries=1)
        return ok, unsynced, silent
    ok, unsynced, silent = asyncio.run(run())
    assert ok["status"] == "open" and ok["software"] == "NTP stratum 2" and ok["kind"] == "ntp"
    assert unsynced["status"] == "error" and silent["status"] == "no_reply"


def test_saved_other_results_move_to_the_new_kinds(monkeypatch):
    monkeypatch.setattr(asv.cams.service, "data", None)          # _rebuild also sets the camera layer
    monkeypatch.setattr(asv.cams.service, "cache_path", None)
    svc = asv.ArednServices()
    svc.regions = {0: {"region": 0, "scanned_at": 1.0, "done_at": 1.0, "station": "", "own": True, "count": 3,
                       "services": [{"name": "Tickets CAD", "link": "http://x/", "node_ip": "10.1.0.1", "category": "other"},
                                    {"name": "Shack", "tag": "eoc", "link": "http://x/", "node_ip": "10.1.0.2", "category": "other"},
                                    {"name": "Some server", "link": "http://x/", "node_ip": "10.1.0.3", "category": "other"}]}}
    svc._rebuild()
    assert [c["category"] for c in svc.data["services"]] == ["dispatch", "eoc", "other"]


def test_every_kind_is_in_a_known_group_and_listed_once():
    keys = [c[0] for c in asv.CATEGORIES]
    assert len(keys) == len(set(keys)) and set(asv.OTHER_SPLIT) <= set(keys)
    assert all(c[3] in asv.GROUPS for c in asv.CATEGORIES)
    assert [g for g in asv.GROUPS if any(c[3] == g for c in asv.CATEGORIES)] == asv.GROUPS
    # the checklist shows groups in GROUPS order, so kinds must be listed group by group
    order = [asv.GROUPS.index(c[3]) for c in asv.CATEGORIES]
    assert order == sorted(order)


def test_newer_kinds_ignore_the_host_name():
    assert asv.categorize({"name": "Shack server", "link": "http://kn0o-hap.local.mesh:8000/"}) == "other"
    assert asv.categorize({"name": "Club page", "link": "http://xx-fire-station.local.mesh/"}) == "other"
    assert asv.categorize({"name": "Calendar", "link": "http://x/calendar.ics"}) == "other"
    assert asv.categorize({"name": "Forms", "link": "http://x/ics-213/"}) == "dispatch"
    assert asv.categorize({"name": "Admin", "link": "http://x/router/"}) == "infra"     # a path still counts


def test_a_rebuild_keeps_the_saved_time_when_nothing_was_checked(monkeypatch):
    monkeypatch.setattr(asv.cams.service, "data", None)
    monkeypatch.setattr(asv.cams.service, "cache_path", None)
    svc = asv.ArednServices()
    svc.data = {"generated_at": 1234.0, "services": [{"name": "Tickets CAD", "link": "http://x/", "node_ip": "10.1.0.1",
                                                      "category": "other"}]}
    svc._rebuild()
    assert svc.data["generated_at"] == 1234.0 and svc.data["services"][0]["category"] == "dispatch"
