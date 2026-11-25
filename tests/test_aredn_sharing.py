"""Regions, signing, what a shared result must pass, and the region schedule."""
import asyncio
import json
import os
import time

import pytest

from ech.core import aredn_sharing as sharing
from ech.core import aredn_services as asv

HOSTS = [{"name": "n0abc-cam", "ip": "10.1.0.20"}, {"name": "n0abc-node", "ip": "10.1.0.1"},
         {"name": "k1map-pi", "ip": "10.2.0.9"}]


@pytest.mark.parametrize("ip,k", [("10.0.1.1", 0), ("10.63.255.1", 0), ("10.64.0.1", 1), ("10.127.9.9", 1),
                                  ("10.128.0.1", 2), ("10.200.3.4", 3), ("44.4.1.1", 0), ("44.130.0.1", 2),
                                  ("172.16.0.1", 0)])
def test_region_by_second_octet(ip, k):
    assert sharing.region_of(ip) == k


@pytest.mark.parametrize("bad", ["", "n0abc-node", "fe80::1", "10.1.2"])
def test_region_of_non_ipv4(bad):
    assert sharing.region_of(bad) is None


def test_region_labels_cover_the_octet():
    assert [sharing.region_range(k) for k in range(4)] == [(0, 63), (64, 127), (128, 191), (192, 255)]
    assert sharing.region_label(2) == "10.128-191.x.x / 44.128-191.x.x"
    assert sharing.region_label(3) == "10.192-255.x.x"


def test_service_id_is_stable_and_specific():
    s = {"ip": "10.1.0.1", "name": "Yard cam [camera]", "link": "http://n0abc-cam/snap.jpg"}
    assert sharing.svc_id(s) == sharing.svc_id(dict(s)) and len(sharing.svc_id(s)) == 20
    assert sharing.svc_id(s) != sharing.svc_id({**s, "link": "http://n0abc-cam/other.jpg"})


def test_identity_signs_and_keeps_its_key(tmp_path):
    path = str(tmp_path / "identity.key")
    a = sharing.Identity(path)
    if os.name == "posix":
        assert os.stat(path).st_mode & 0o777 == 0o600
    sig = a.sign(b"hello")
    assert sharing.verify(a.key, b"hello", sig)
    assert not sharing.verify(a.key, b"hellO", sig)
    assert not sharing.verify("not base64!", b"hello", sig)
    assert sharing.Identity(path).key == a.key                 # the same key after a restart


# ── a region's result and what it must pass ───────────────────────────────────
def _svc(n, region_octet=1, status="open"):
    s = {"ip": f"10.{region_octet}.0.{n}", "name": f"Service {n}", "link": f"http://10.{region_octet}.0.{n}:8080/"}
    rec = {**asv.base_record(s, HOSTS, "other"), "status": status, "kind": "page", "software": "", "view": "browser",
           "ms": 12, "open_ports": [], "other_apps": [], "node": f"n0abc-{n}", "lat": 44.1, "lon": -69.1,
           "grid": "FN54", "place": "Somewhere, Maine"}
    return s, rec


def _doc(identity, recs, region=0, at=None, station="k1map"):
    return sharing.make_doc(identity, station, region, at or time.time(), recs)


def test_signed_result_opens(tmp_path):
    me, peer = sharing.Identity(str(tmp_path / "me")), sharing.Identity(str(tmp_path / "peer"))
    recs = [_svc(n)[1] for n in range(5)]
    p = sharing.open_doc(_doc(peer, recs), 0, time.time(), sharing.MAX_AGE_S, me.key)
    assert p["scanner"]["station"] == "k1map" and len(p["services"]) == 5
    assert set(p["services"][0]) <= {"id", *sharing.MEASURED} and "link" not in p["services"][0]       # names and links are never shared


@pytest.mark.parametrize("case,msg", [
    ("tampered", "bad signature"), ("wrong_region", "a different region"), ("old", "too old"),
    ("future", "in the future"), ("own", "our own result"), ("scheme", "different region scheme"),
    ("layer", "not a services result"),
    ("garbage", "not a signed result")])
def test_bad_results_are_refused(tmp_path, case, msg):
    me, peer = sharing.Identity(str(tmp_path / "me")), sharing.Identity(str(tmp_path / "peer"))
    now = time.time()
    recs = [_svc(1)[1]]
    doc = _doc(peer, recs)
    if case == "tampered":
        doc = {**doc, "payload": doc["payload"].replace('"open"', '"closed"')}
    elif case == "wrong_region":
        doc = _doc(peer, recs, region=1)
    elif case == "old":
        doc = _doc(peer, recs, at=now - 5 * 3600)
    elif case == "future":
        doc = _doc(peer, recs, at=now + 3600)
    elif case == "own":
        doc = _doc(me, recs)
    elif case == "scheme":
        p = json.loads(doc["payload"])
        p["scheme"] = "octet2/8"
        text = json.dumps(p)
        doc = {"payload": text, "sig": peer.sign(text.encode())}
    elif case == "layer":
        p = json.loads(doc["payload"])
        p["layer"] = "adsb"
        text = json.dumps(p)
        doc = {"payload": text, "sig": peer.sign(text.encode())}
    elif case == "garbage":
        doc = {"services": []}
    with pytest.raises(sharing.Rejected, match=msg):
        sharing.open_doc(doc, 0, now, sharing.MAX_AGE_S, me.key)


def test_only_known_services_and_known_words_are_taken():
    s1, r1 = _svc(1)
    s2, r2 = _svc(2)
    ours = {r1["id"]: asv.base_record(s1, HOSTS, "other"), r2["id"]: asv.base_record(s2, HOSTS, "other")}
    payload = {"services": [
        sharing.shareable(r1),
        {**sharing.shareable(r2), "status": "pwned"},                              # not a status we write
        {"id": "f" * 20, "status": "open"},                                         # a service we don't list
        {**sharing.shareable(r1), "status": "closed"},                              # a second copy: first wins
    ]}
    got = sharing.measured_from(payload, ours)
    assert list(got) == [r1["id"]] and got[r1["id"]]["status"] == "open"
    base = ours[r1["id"]]
    for bad in ({"software": "<img src=x onerror=alert(1)>"}, {"view": "javascript:alert(1)"},
                {"place": "<script>x</script>"}, {"lat": 95.0}, {"ms": -1}, {"open_ports": [22]},
                {"other_apps": ["evil"]}, {"grid": "ZZ99"}, {"node": "bad name!"}, {"kind": "exploit"},
                {"feed": {"path": "/x", "count": 3}}):
        assert sharing.clean_measured({**sharing.shareable(r1), **bad}, base) is None, bad
    ok = sharing.clean_measured({**sharing.shareable(r1), "href": "javascript:alert(1)", "name": "spoof"}, base)
    assert ok and "href" not in ok and "name" not in ok                  # identity fields never come from a peer
    assert sharing.clean_measured({**sharing.shareable(r1), "software": "NTP stratum 2", "kind": "ntp"}, base)


def test_feed_must_be_a_known_location():
    s = {"ip": "10.1.0.5", "name": "ADS-B", "link": "http://10.1.0.5:8080/"}
    base = asv.base_record(s, HOSTS, "adsb")
    m = {**sharing.shareable({**_svc(5)[1], "id": base["id"]}), "feed": {"path": "/data/aircraft.json", "count": 12}}
    assert sharing.clean_measured(m, base)["feed"] == {"path": "/data/aircraft.json", "count": 12}
    assert sharing.clean_measured({**m, "feed": {"path": "/etc/passwd", "count": 1}}, base) is None


def test_trust_pins_and_distrusts(tmp_path):
    t = sharing.Trust(str(tmp_path / "peers.json"))
    now = time.time()
    t.check("K1MAP", "key-a", now)
    t.accept("K1MAP", "key-a", now, "ok")
    with pytest.raises(sharing.Rejected, match="different key"):
        t.check("k1map", "key-b", now)
    t.reject("N0BAD", "key-c", now, "spot check: only 1 of 8 agree")
    with pytest.raises(sharing.Rejected, match="not trusted"):
        t.check("N0BAD", "key-c", now)
    t.check("N0BAD", "key-c", now + sharing.DISTRUST_S + 1)                 # forgiven after 7 days
    again = sharing.Trust(str(tmp_path / "peers.json"))
    assert again.pins == {"k1map": "key-a"} and "key-c" in again.distrust


def test_sample_size_and_agreement():
    assert len(sharing.sample_ids([str(i) for i in range(5)])) == 5
    assert len(sharing.sample_ids([str(i) for i in range(100)])) == 8
    assert len(sharing.sample_ids([str(i) for i in range(300)])) == 15
    assert len(sharing.sample_ids([str(i) for i in range(5000)])) == 30
    a = {"status": "open", "lat": 44.0, "lon": -69.0}
    assert sharing.agrees(a, {"status": "password", "lat": 44.005, "lon": -69.0})
    assert not sharing.agrees(a, {"status": "closed", "lat": 44.0, "lon": -69.0})
    assert not sharing.agrees(a, {"status": "open", "lat": 44.5, "lon": -69.0})       # 55 km away
    assert sharing.agrees(a, {"status": "open", "lat": None, "lon": None})


def test_peers_are_found_by_their_advertised_service():
    svcs = [{"name": "AREDN Services Map", "ip": "10.2.0.1", "link": "http://k1map-pi.local.mesh:8088/"},
            {"name": "aredn-services-map", "ip": "10.2.0.1", "link": "http://10.2.0.9:8088/"},   # same box twice
            {"name": "Yard cam", "ip": "10.1.0.1", "link": "http://n0abc-cam/"}]
    t = sharing.peer_targets(svcs, HOSTS, ["http://10.3.3.3:8088/"])
    assert t == [("http://10.2.0.9:8088", "k1map-pi.local.mesh:8088"), ("http://10.3.3.3:8088", "10.3.3.3:8088")]


# ── using a peer's result (the services pass) ────────────────────────────────
def _setup(tmp_path, monkeypatch, peer_says="open", here_says="open", n=40, at=None, extra=0):
    svc = asv.ArednServices()
    svc.configure({"database": {"path": str(tmp_path / "me" / "x.db")}})
    peer = sharing.Identity(str(tmp_path / "peer.key"))
    mine, recs = [], []
    for i in range(n):
        s, r = _svc(i, status=peer_says)
        mine.append((s, "other"))
        recs.append(r)
    recs += [_svc(100 + i, region_octet=5)[1] for i in range(extra)]
    doc = _doc(peer, recs, at=at)
    info = {"app": sharing.APP, "proto": sharing.PROTO, "key": peer.key,
            "layers": {"services": {"scheme": sharing.SCHEME,
                                    "regions": {"1": {"scanned_at": json.loads(doc["payload"])["scanned_at"]}}}}}

    async def get_json(client, base, host, path, cap):
        return info if path == sharing.INFO_PATH else doc if path == "/peer/v1/services/region/1" else None
    monkeypatch.setattr(sharing, "get_json", get_json)
    checked = []

    async def check_here(client, todo, hosts):
        checked.extend(todo)
        return [{**asv.base_record(s, hosts, c), "status": here_says, "lat": 44.1, "lon": -69.1} for s, c in todo]
    monkeypatch.setattr(svc, "_check_and_locate", check_here)
    mesh = [{"name": "AREDN Services Map", "ip": "10.2.0.1", "link": "http://10.2.0.9:8088/"}]
    svc.peer_targets = sharing.peer_targets(mesh, HOSTS, [])
    return svc, peer, mine, mesh, checked


def test_an_honest_peer_is_used(tmp_path, monkeypatch):
    svc, peer, mine, mesh, checked = _setup(tmp_path, monkeypatch)
    got = asyncio.run(svc._from_peers(None, 0, mine, HOSTS, mesh, time.time()))
    assert got is not None
    recs, state = got
    assert len(recs) == 40 and not state["own"] and state["station"] == "k1map"
    assert len(checked) == 8                                  # only the spot-check sample went over the mesh
    assert "8 of 8 spot checks agree" in state["note"]
    assert recs[0]["link"] == "http://10.1.0.0:8080/"         # from our own list
    assert svc.trust.pins == {"k1map": peer.key}


def test_a_lying_peer_is_refused_and_distrusted(tmp_path, monkeypatch):
    svc, peer, mine, mesh, _ = _setup(tmp_path, monkeypatch, peer_says="open", here_says="closed")
    assert asyncio.run(svc._from_peers(None, 0, mine, HOSTS, mesh, time.time())) is None
    assert peer.key in svc.trust.distrust
    assert "not trusted" in svc.peer_log[-1]["note"]
    # the next time it isn't even spot-checked
    assert asyncio.run(svc._from_peers(None, 0, mine, HOSTS, mesh, time.time())) is None
    assert "not trusted until" in svc.peer_log[-1]["note"]


def test_old_or_incomplete_results_are_not_used(tmp_path, monkeypatch):
    svc, _, mine, mesh, _ = _setup(tmp_path, monkeypatch, at=time.time() - 5 * 3600)
    assert asyncio.run(svc._from_peers(None, 0, mine, HOSTS, mesh, time.time())) is None
    svc, _, mine, mesh, _ = _setup(tmp_path, monkeypatch)
    mine += [_svc(200 + i) for i in range(10)]                 # we list 50, it covers 40: 80%
    mine = [(m[0], "other") if isinstance(m[1], dict) else m for m in mine]
    assert asyncio.run(svc._from_peers(None, 0, mine, HOSTS, mesh, time.time())) is None
    assert "covers 40 of the region's 50" in svc.peer_log[-1]["note"]


def test_entries_outside_our_list_are_ignored(tmp_path, monkeypatch):
    svc, _, mine, mesh, _ = _setup(tmp_path, monkeypatch, extra=5)
    recs, _ = asyncio.run(svc._from_peers(None, 0, mine, HOSTS, mesh, time.time()))
    assert len(recs) == 40


# ── the schedule ─────────────────────────────────────────────────────────────
def test_regions_take_turns_6_hours_apart(tmp_path):
    svc = asv.ArednServices()
    svc.configure({"database": {"path": str(tmp_path / "x.db")}})
    k, due = svc.next_region()
    assert k == 0 and due <= time.time()
    t = time.time()
    svc.regions[0] = {"scanned_at": t, "services": []}
    svc._last_pass = t
    k, due = svc.next_region()
    assert k == 1 and abs(due - (t + 6 * 3600)) < 1
    for r in (1, 2, 3):
        svc.regions[r] = {"scanned_at": t + r * 6 * 3600, "services": []}
    svc._last_pass = t + 18 * 3600
    assert svc.next_region() == (0, t + 24 * 3600)            # region 1 again a day later
    svc.regions[0] = {"scanned_at": t - 2 * 3600, "services": []}   # taken from a peer, checked 2 h before
    assert svc.next_region()[0] == 0 and svc.next_region()[1] == t + 24 * 3600   # still 6 h after the last pass


def test_merged_view_keeps_the_old_pass_for_regions_not_yet_redone(tmp_path):
    svc = asv.ArednServices()
    svc.configure({"database": {"path": str(tmp_path / "x.db")}})
    svc.data = {"services": [{"node_ip": "10.1.0.1", "name": "old r1", "category": "other"},
                             {"node_ip": "10.70.0.1", "name": "old r2", "category": "other"}]}
    svc.regions[0] = {"scanned_at": time.time(), "station": "here", "own": True, "count": 1,
                      "services": [{"node_ip": "10.1.0.1", "name": "new r1", "category": "camera"}]}
    svc._rebuild()
    assert [c["name"] for c in svc.data["services"]] == ["new r1", "old r2"]
    assert [r["region"] for r in svc.data["regions"]] == [1, 2, 3, 4]
    from ech.core.aredn_cameras import service as cams
    assert [c["name"] for c in cams.data["cameras"]] == ["new r1"]
    cams.data = None


class _Resp:
    def __init__(self, j):
        self._j = j

    def json(self):
        return self._j


def _fake_httpx(monkeypatch, sysinfo):
    import httpx

    class C:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, timeout=None):
            return _Resp(sysinfo)
    monkeypatch.setattr(httpx, "AsyncClient", C)


def test_an_empty_mesh_list_is_an_error_not_a_result(tmp_path, monkeypatch):
    svc = asv.ArednServices()
    svc.configure({"database": {"path": str(tmp_path / "x.db")},
                   "radio_directory": {"aredn_services": {"hosts_url": "http://node/a/sysinfo"}}})
    _fake_httpx(monkeypatch, {"hosts": [], "services": []})
    with pytest.raises(RuntimeError, match="listed 0 hosts and 0 services"):
        asyncio.run(svc.refresh_region(1))
    assert 1 not in svc.regions and not (tmp_path / "aredn_regions" / "r2.json").exists()


def test_a_region_that_had_services_is_not_emptied(tmp_path, monkeypatch):
    svc = asv.ArednServices()
    svc.configure({"database": {"path": str(tmp_path / "x.db")},
                   "radio_directory": {"aredn_services": {"hosts_url": "http://node/a/sysinfo"}}})
    svc.regions[1] = {"count": 40, "services": [], "scanned_at": 1.0}
    _fake_httpx(monkeypatch, {"hosts": HOSTS, "services": [_svc(1)[0]]})        # only region 1 (10.1.x)
    with pytest.raises(RuntimeError, match="kept the last result"):
        asyncio.run(svc.refresh_region(1))
    assert svc.regions[1]["count"] == 40


def test_default_station_names_are_unique(tmp_path):
    names = []
    for n in ("a", "b"):
        svc = asv.ArednServices()
        svc.configure({"database": {"path": str(tmp_path / n / "x.db")}})
        svc._node_name = "same-supernode"
        names.append(svc.station_name())
    assert names[0] != names[1] and all(x.startswith("SignalMatrix/") for x in names)
    svc.station = "N0CALL-map"
    assert svc.station_name() == "N0CALL-map"


@pytest.mark.asyncio
async def test_ech_peer_and_tile_routes(tmp_path):
    from ech.core.database import Database
    from ech.core.router import Router
    from ech.core.anomaly import AnomalyEngine
    from ech.api.app import create_app
    from ech.core.tile_cache import tiles
    from httpx import AsyncClient, ASGITransport
    db = Database(":memory:")
    await db.connect()
    app = create_app(Router(db, anomaly_engine=AnomalyEngine({})), db)
    asv.service.configure({"database": {"path": str(tmp_path / "ech.db")}})
    tiles.configure({"internet": False}, str(tmp_path), peers_fn=lambda: [])
    doc = sharing.make_doc(asv.service.identity, "SignalMatrix/abc123", 0, time.time(), [])
    asv.service.regions[0] = {"doc": doc, "scanned_at": time.time(), "station": "SignalMatrix/abc123",
                              "count": 0, "services": []}

    def client(ip):
        return AsyncClient(transport=ASGITransport(app=app, client=(ip, 5000)), base_url="http://t")
    try:
        async with client("10.99.0.86") as c:
            info = (await c.get("/peer/v1/info")).json()
            assert info["app"] == "aredn-services-map" and info["station"].startswith("SignalMatrix/")
            assert info["layers"]["services"]["regions"]["1"]["station"] == "SignalMatrix/abc123"
            assert "tiles" in info["layers"]
            assert (await c.get("/peer/v1/services/region/1")).json() == doc
            assert (await c.get("/peer/v1/services/region/2")).status_code == 404
            assert (await c.get("/peer/v1/tiles/3/1/1.png")).status_code == 404
            assert (await c.get("/meshphone/tiles/3/1/1.png")).status_code == 404
            assert [r["region"] for r in (await c.get("/meshphone/services.json")).json()["regions"]] == [1, 2, 3, 4]
        async with client("44.10.0.5") as c:                    # amateur 44-net: on the mesh
            assert (await c.get("/peer/v1/info")).status_code == 200
        for ip in ("8.8.8.8", "44.200.0.1"):                      # the internet, and the sold part of 44/8
            async with client(ip) as c:
                assert (await c.get("/peer/v1/info")).status_code == 404
                assert (await c.get("/peer/v1/services/region/1")).status_code == 404
    finally:
        asv.service.regions.clear()
        await db.close()                    # its worker thread would keep pytest from exiting


def test_stream_must_be_a_known_rtsp_path_on_a_camera():
    s = {"ip": "10.1.0.5", "name": "Cam", "link": "http://10.1.0.6/"}
    base = asv.base_record(s, HOSTS, "camera")
    m = {**sharing.shareable({**_svc(5)[1], "id": base["id"]}),
         "stream": {"port": 554, "path": "/h264Preview_01_main", "status": "open"}}
    assert sharing.clean_measured(m, base)["stream"] == {"port": 554, "path": "/h264Preview_01_main", "status": "open"}
    for bad in ({"port": 554, "path": "/../evil", "status": "open"}, {"port": 80, "path": "/", "status": "open"},
                {"port": 554, "path": "/", "status": "pwned"}, "rtsp://elsewhere/"):
        assert sharing.clean_measured({**m, "stream": bad}, base) is None, bad
    other = asv.base_record(s, HOSTS, "other")
    assert sharing.clean_measured({**m, "id": other["id"]}, other) is None   # only cameras have streams


def test_status_lists_the_other_copies_and_survives_a_restart(tmp_path):
    def fresh():
        s = asv.ArednServices()
        s.configure({"database": {"path": str(tmp_path / "x.db")}})
        return s
    s = fresh()
    targets = [("http://10.1.0.9:8766", "copy-a:8766"), ("http://10.2.0.9:8088", "copy-b:8088"),
               ("http://10.3.0.9:8766", "us:8766")]
    infos = [{"app": sharing.APP, "station": "Copy A/123abc", "version": "0.3.1", "key": "k1"},
             None,                                                      # didn't answer
             {"app": sharing.APP, "station": "us", "key": s.identity.key}]  # our own node's advert
    s.peer_targets = targets
    s._record_peers(targets, infos, 1000.0)
    m = s.sharing_meta()
    # copy-b never answered as a copy (another site's page): asked, but not counted
    assert (m["found"], m["candidates"]) == (1, 2) and m["station"] and m["next"]["region"] in (1, 2, 3, 4)
    assert [(p["host"], p["answered"]) for p in m["peers"]] == [("copy-a:8766", True)]
    s2 = fresh()                                                         # after a restart
    s2.peer_targets = targets
    assert s2.sharing_meta()["found"] == 1
    s2._record_peers(targets[:1], [None], 2000.0)                         # a copy that stops answering keeps its name
    p = s2.sharing_meta()["peers"]
    assert p[0]["station"] == "Copy A/123abc" and p[0]["answered"] is False and p[0]["last_answer"] == 1000.0



def test_a_target_at_our_own_address_is_us(tmp_path, monkeypatch):
    s = asv.ArednServices()
    s.configure({"database": {"path": str(tmp_path / "x.db")}})
    monkeypatch.setattr(asv.ArednServices, "_own_ips", staticmethod(lambda: {"127.0.0.1", "10.13.53.44"}))
    s.peer_targets = [("http://10.13.53.44:8766", "kn0o-pbx-ech:8766"), ("http://10.4.236.70:80", "other-map")]
    assert s.sharing_meta()["candidates"] == 1



def test_a_shared_region_comes_due_after_the_copy_that_checked_it():
    s = asv.ArednServices()
    now = 1_000_000.0
    s.regions = {k: {"scanned_at": now, "done_at": now} for k in range(4)}
    s.regions[2] = {"scanned_at": now - 3600, "done_at": now, "own": False}         # taken from another copy
    assert s.next_region() == (2, now - 3600 + s.refresh_s)      # without the delay: due with the other copy
    s.regions[2]["defer_s"] = 7200
    k, due = s.next_region()
    assert k != 2 and s.regions[2]["scanned_at"] + s.refresh_s + 7200 > due
    lo, hi = asv.ADOPTED_DEFER_S
    assert 0 < lo <= hi < asv.sharing.MAX_AGE_S                  # the original's result is still fresh enough then

def test_find_peers_records_who_answers(tmp_path, monkeypatch):
    import asyncio
    s = asv.ArednServices()
    s.configure({"database": {"path": str(tmp_path / "x.db")},
                 "radio_directory": {"aredn_services": {"hosts_url": "http://node/a/sysinfo"}}})
    monkeypatch.setattr(asv.ArednServices, "_own_ips", staticmethod(lambda: {"127.0.0.1"}))
    monkeypatch.setattr(sharing, "peer_targets", lambda svcs, hosts, extra: [("http://10.5.0.9:8088", "copy-c:8088")])

    async def fake_get_json(client, base, host, path, limit):
        return {"app": sharing.APP, "station": "Copy C/abc123", "version": "0.3.1", "key": "kc"}
    monkeypatch.setattr(sharing, "get_json", fake_get_json)

    class Resp:
        def json(self):
            return {"hosts": [{"name": "copy-c", "ip": "10.5.0.9"}], "services": [{"name": "AREDN Services Map"}]}

    class Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, timeout=None): return Resp()
    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", Client)
    assert asyncio.run(s.find_peers()) == 1
    m = s.sharing_meta()
    assert m["found"] == 1 and m["peers"][0]["station"] == "Copy C/abc123"
