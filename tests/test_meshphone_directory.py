"""
tests/test_meshphone_directory.py
---------------------------------
AREDN MeshPhone offices + white pages (ech/core/meshphone_directory.py) and
the endpoints that serve them. All data below is made up; it only mirrors
the shape of the WT0F network_json export and N2MH's WhitePages XML.
"""

import asyncio
import socket
import struct
import threading

import pytest

from ech.core import directory_status as ds
from ech.core import meshphone_directory as mp

OFFICES_JSON = [
    {"_id": "x1", "name": "TESTVILLE", "location": "Testville, ZZ", "admin_callsign": "n0aaa",
     "admin_phone": "555-4111", "admin_email": "a@example.com", "admin_name": "A Person",
     "ip_address": "10.1.2.3", "pbx_status": "Active", "checkin_at": "2026-11-01T00:00:00Z",
     "office_codes": [{"office_code": "55541", "npa": "555", "dialplans": ["_55541xx"]}],
     "trunks": [{"pbx_name": "OTHERTOWN", "office_code": "55599"}]},
    {"_id": "x2", "name": "OTHERTOWN", "location": "Othertown, ZZ", "admin_phone": "",
     "office_codes": [{"office_code": "55599", "dialplans": ["_55599xx"]}], "trunks": []},
    {"_id": "x3"},   # nameless: skipped
]

WP_XML = """<?xml version="1.0" encoding="UTF-8"?>
<WhitePages>
  <administration><author>TEST</author><created>2025-10-13 02:27:01 -0400</created></administration>
  <listing><callsign>n0aaa</callsign><opname>A+Person</opname><meshphone>5554101</meshphone>
    <pbx>TESTVILLE</pbx><meshmail>a%40testville.local.mesh</meshmail><notes>private+note</notes></listing>
  <listing><callsign>N0BBB</callsign><opname>B</opname><meshphone>555-9901</meshphone><pbx>OTHERTOWN</pbx></listing>
  <listing><callsign></callsign><meshphone>5550000</meshphone></listing>
  <listing><callsign>N0CCC</callsign><meshphone>123</meshphone></listing>
</WhitePages>"""


def test_parse_offices_keeps_only_directory_fields():
    offs = mp.parse_offices(OFFICES_JSON)
    assert [o["name"] for o in offs] == ["OTHERTOWN", "TESTVILLE"]
    t = offs[1]
    assert t["office_codes"] == ["55541"] and t["dialplans"] == ["_55541xx"]
    assert t["trunks"] == ["OTHERTOWN"] and t["admin_callsign"] == "N0AAA"
    assert t["admin_phone"] == "5554111"
    flat = repr(offs)
    assert t["admin_email"] == "a@example.com"        # the contact the office published
    assert t["admin_name"] == "A Person"              # names are public on the mesh, like callsigns
    # the PBX address is kept: the MeshPhone map (meshphone_map.py) measures it,
    # and WT0F's own map already publishes it to the whole mesh
    assert t["ip"] == "10.1.2.3"


def test_parse_offices_rejects_non_list():
    with pytest.raises(ValueError):
        mp.parse_offices({"error": "nope"})


def test_parse_whitepages():
    people, created = mp.parse_whitepages(WP_XML)
    assert created.startswith("2025-10-13")
    assert people == [
        {"callsign": "N0AAA", "name": "A Person", "number": "5554101", "pbx": "TESTVILLE"},
        {"callsign": "N0BBB", "name": "B", "number": "5559901", "pbx": "OTHERTOWN"},
    ]
    assert "private" not in repr(people) and "local.mesh" not in repr(people)


def test_parse_whitepages_refuses_dtd():
    evil = '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><WhitePages></WhitePages>'
    with pytest.raises(ValueError):
        mp.parse_whitepages(evil)


def test_parse_whitepages_wrong_root():
    with pytest.raises(ValueError):
        mp.parse_whitepages("<html><body/></html>")


def test_dial_string():
    assert mp.dial_string("5554101") == "785554101"


def test_mesh_dns_from_leases(tmp_path):
    (tmp_path / "dhclient.eth0.leases").write_text(
        'lease {\n  option domain-name "example.lan";\n  option domain-name-servers 192.168.1.1;\n}\n')
    (tmp_path / "dhclient.vlan.leases").write_text(
        'lease {\n  option domain-name "local.mesh";\n  option domain-name-servers 10.9.9.1;\n}\n'
        'lease {\n  option domain-name-servers 10.99.0.1;\n  option domain-name "local.mesh";\n}\n')
    assert mp.mesh_dns_from_leases(str(tmp_path / "dhclient*.leases")) == "10.99.0.1"
    assert mp.mesh_dns_from_leases(str(tmp_path / "none*.leases")) is None


def test_resolve_url_passthrough_for_normal_hosts():
    assert mp.resolve_url("https://example.org/x", "auto") == ("https://example.org/x", {})


@pytest.fixture
def fake_dns():
    """A one-shot UDP DNS server on 127.0.0.1 answering any A query with 10.20.30.40."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(5)
    port = sock.getsockname()[1]

    def serve():
        try:
            data, addr = sock.recvfrom(512)
        except OSError:
            return
        ans = bytes([0xC0, 0x0C]) + struct.pack(">HHIH", 1, 1, 60, 4) + socket.inet_aton("10.20.30.40")
        sock.sendto(data[:2] + struct.pack(">HHHHH", 0x8180, 1, 1, 0, 0) + data[12:] + ans, addr)

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    yield port
    t.join(6)
    sock.close()


def test_dns_query_and_resolve(monkeypatch, fake_dns):
    monkeypatch.setattr(mp, "DNS_PORT", fake_dns)
    url, hdrs = mp.resolve_url("http://WT0F-freepbx.local.mesh/whitepages.xml", "127.0.0.1")
    assert url == "http://10.20.30.40/whitepages.xml"
    assert hdrs == {"Host": "wt0f-freepbx.local.mesh"}   # lowercased; HTTP Host matching is case-insensitive


def test_resolve_url_without_mesh_dns(monkeypatch):
    monkeypatch.setattr(mp, "mesh_dns_from_leases", lambda *a: None)
    with pytest.raises(RuntimeError):
        mp.resolve_url("http://x.local.mesh/", "auto")


# ── refresh integration ──

class _Resp:
    def __init__(self, body, code=200):
        self._body, self.status_code = body, code
        self.text = body if isinstance(body, str) else ""

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _many_people_xml(created, n=12):
    rows = "".join(f"<listing><callsign>N{i}AA</callsign><meshphone>55541{i:02d}</meshphone></listing>" for i in range(n))
    return f"<WhitePages><administration><created>{created}</created></administration>{rows}</WhitePages>"


@pytest.fixture
def live(monkeypatch, tmp_path):
    s = ds.DirectoryStatus()
    s.configure({"database": {"path": str(tmp_path / "ech.db")},
                 "radio_directory": {"meshphone": {
                     "whitepages_urls": ["http://old.local.mesh/whitepages.xml", "http://new.local.mesh/whitepages.xml",
                                         "http://dead.local.mesh/whitepages.xml"]}}})
    monkeypatch.setattr(ds, "status", s)
    monkeypatch.setattr(mp, "resolve_url", lambda url, dns: (url, {}))
    return s


def test_meshphone_refresh_picks_newest_whitepages(live, tmp_path):
    class Client:
        async def get(self, url, headers=None):
            if url.endswith("network_json"):
                return _Resp(OFFICES_JSON)
            if "old." in url:
                return _Resp(_many_people_xml("2025-01-01"))
            if "new." in url:
                return _Resp(_many_people_xml("2025-10-13", n=15))
            return _Resp("", 404)
    errors = asyncio.run(live._refresh_meshphone(Client()))
    assert errors == []   # one dead mirror is fine when another worked
    assert len(live.mp_offices) == 2
    assert live.mp_people_created == "2025-10-13" and len(live.mp_people) == 15
    assert live.mp_people_source == "new.local.mesh"
    d = live.meshphone()
    assert d["people"][0]["dial"].startswith("78") and len(d["people"][0]["dial"]) == 9
    assert {o["name"]: o["dial"] for o in d["offices"]} == {"OTHERTOWN": "", "TESTVILLE": "785554111"}


def test_meshphone_refresh_failure_keeps_old_data(live):
    live.mp_offices = [{"name": "KEEP"}]
    live.mp_people = [{"callsign": "KEEP", "number": "5554101"}]

    class Down:
        async def get(self, url, headers=None):
            raise RuntimeError("no route to host")
    errors = asyncio.run(live._refresh_meshphone(Down()))
    assert any("offices" in e for e in errors) and any("white pages" in e for e in errors)
    assert live.mp_offices == [{"name": "KEEP"}] and live.mp_people[0]["callsign"] == "KEEP"


def test_meshphone_disabled(live):
    live._config = {"radio_directory": {"meshphone": {"enabled": False}}}

    class Boom:
        async def get(self, *a, **k):
            raise AssertionError("must not fetch")
    assert asyncio.run(live._refresh_meshphone(Boom())) == []


def test_cache_roundtrip_and_permissions(live, tmp_path):
    import os
    live.mp_offices = mp.parse_offices(OFFICES_JSON)
    live.mp_people, live.mp_people_created = mp.parse_whitepages(WP_XML)
    live._save_cache(set())
    again = ds.DirectoryStatus()
    again.configure({"database": {"path": str(tmp_path / "ech.db")}})
    assert len(again.mp_offices) == 2 and len(again.mp_people) == 2
    if os.name == "posix":
        assert os.stat(tmp_path / "directory_status.json").st_mode & 0o077 == 0


# ── endpoints ──

async def _get(path, client_ip, headers=None):
    from ech.core.database import Database
    from ech.core.router import Router
    from ech.core.anomaly import AnomalyEngine
    from ech.api.app import create_app
    from httpx import AsyncClient, ASGITransport
    db = Database(":memory:")
    await db.connect()
    router = Router(db, anomaly_engine=AnomalyEngine({}))
    app = create_app(router, db)
    transport = ASGITransport(app=app, client=(client_ip, 5000))
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        return await c.get(path, headers=headers or {})


@pytest.mark.asyncio
async def test_phonebook_meshphone_local_only(live):
    live.mp_people, _ = mp.parse_whitepages(WP_XML)
    live.mp_offices = mp.parse_offices(OFFICES_JSON)
    r = await _get("/phonebook-meshphone.xml", "192.168.1.50")
    assert r.status_code == 200
    assert "<Telephone>785554101</Telephone>" in r.text
    assert "N0AAA A Person (TESTVILLE)" in r.text
    assert "[OFFICE] TESTVILLE N0AAA" in r.text
    assert (await _get("/phonebook-meshphone.xml", "10.99.0.86")).status_code == 200   # mesh phone
    assert (await _get("/phonebook-meshphone.xml", "192.168.1.50", {"X-Forwarded-For": "8.8.8.8"})).status_code == 403
    assert (await _get("/phonebook-meshphone.xml", "8.8.8.8")).status_code == 403


@pytest.mark.asyncio
async def test_api_pbx_meshphone(live):
    live.mp_people, _ = mp.parse_whitepages(WP_XML)
    d = (await _get("/api/pbx/meshphone", "127.0.0.1")).json()
    assert d["people"][0]["callsign"] == "N0AAA" and d["people"][0]["dial"] == "785554101"
    assert d["status"]["meshphone_people"] == 2


@pytest.mark.asyncio
async def test_meshphone_map_page_and_json_mesh_only(live):
    from ech.core.meshphone_map import service as mp_map, build_map
    live.mp_people, _ = mp.parse_whitepages(WP_XML)
    live.mp_offices = mp.parse_offices(OFFICES_JSON)
    mp_map.data = build_map(live.mp_offices, [], [], {}, {}, {}, {}, set(), "TESTVILLE",
                            people=live.mp_people, generated_at=1.0)
    page = await _get("/meshphone/", "10.99.0.86")                 # an AREDN mesh client, no login
    assert page.status_code == 200 and "MeshPhone Network Map" in page.text
    d = (await _get("/meshphone/map.json", "10.99.0.86")).json()
    assert d["map"]["measured_from"] == "TESTVILLE"
    p0 = d["people"][0]
    assert (p0["callsign"], p0["name"], p0["number"], p0["pbx"]) == ("N0AAA", "A Person", "5554101", "TESTVILLE")
    assert p0["location"] == "Testville, ZZ"          # from the person's home office
    mp_map.data = None


@pytest.mark.asyncio
async def test_meshphone_map_from_the_internet_needs_a_login():
    from ech.core.database import Database
    from ech.core.router import Router
    from ech.core.anomaly import AnomalyEngine
    from ech.core.auth import AuthManager, SESSION_COOKIE
    from ech.api.app import create_app
    from httpx import AsyncClient, ASGITransport
    db = Database(":memory:")
    await db.connect()
    auth = AuthManager(db)
    await auth.create_user("op", "pw-123456", "viewer")
    await auth.create_user("new", "pw-123456", "viewer", must_change_pw=True)
    app = create_app(Router(db, anomaly_engine=AnomalyEngine({})), db, auth=auth)

    async def get(path, ip, headers=None, user=None):
        cookies = {SESSION_COOKIE: await auth.login(user, "pw-123456")} if user else {}
        async with AsyncClient(transport=ASGITransport(app=app, client=(ip, 5000)), base_url="http://test",
                               cookies=cookies) as c:
            return await c.get(path, headers=headers or {})
    proxied = {"X-Forwarded-For": "8.8.8.8"}
    assert (await get("/meshphone/map.json", "10.99.0.86")).status_code == 200      # mesh: no login
    assert (await get("/meshphone/map.json", "8.8.8.8")).status_code == 401
    r = await get("/meshphone/", "192.168.1.50", proxied)
    assert r.status_code == 302 and r.headers["location"] == "/login?next=/meshphone/"
    assert (await get("/meshphone/", "192.168.1.50", proxied, user="op")).status_code == 200
    assert (await get("/meshphone/map.json", "192.168.1.50", proxied, user="op")).status_code == 200
    assert (await get("/meshphone/map.json", "192.168.1.50", proxied, user="new")).status_code == 401
    # the cameras data and the /cameras/ alias follow the same rule
    from ech.core.aredn_cameras import service as cams
    cams.data = {"generated_at": 1.0, "summary": {}, "cameras": [{"name": "Yard", "status": "open", "lat": 44.0, "lon": -69.0}]}
    try:
        r = await get("/meshphone/cameras.json", "10.99.0.86")
        assert r.status_code == 200 and r.json()["cameras"][0]["name"] == "Yard"
        assert (await get("/meshphone/cameras.json", "192.168.1.50", proxied)).status_code == 401
        assert (await get("/meshphone/cameras.json", "192.168.1.50", proxied, user="op")).status_code == 200
        r = await get("/cameras/", "10.99.0.86")
        assert r.status_code == 302 and r.headers["location"] == "/meshphone/?show=cameras"
        r = await get("/cameras", "192.168.1.50", proxied)
        assert r.status_code == 302 and r.headers["location"] == "/login?next=/cameras/"
    finally:
        cams.data = None


def test_meshphone_refresh_once_a_day(live):
    import time
    live.mp_offices = [{"name": "KEEP"}]
    live.mp_people = [{"callsign": "KEEP", "number": "5554101"}]
    live.mp_offices_refreshed_at = live.mp_people_refreshed_at = time.time() - 3 * 3600

    class Boom:
        async def get(self, *a, **k):
            raise AssertionError("fresh lists must not be fetched again")
    assert asyncio.run(live._refresh_meshphone(Boom())) == []

    live.mp_offices_refreshed_at = time.time() - 25 * 3600   # a day old: due

    class Down:
        async def get(self, url, headers=None):
            raise RuntimeError("no route to host")
    assert asyncio.run(live._refresh_meshphone(Down()))
