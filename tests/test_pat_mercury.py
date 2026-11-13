"""Pat connect sessions, incl. Mercury as an alternative to VARA HF (v1.1.48)."""

import asyncio

import pytest

from ech.adapters.mock_pat_winlink import MockPatServer
from ech.adapters.pat_winlink import PatWinlinkAdapter, build_connect_url


def test_mercury_rides_on_pats_vara_hf_transport():
    assert build_connect_url("mercury", "w1abc-10", 2300) == "varahf:///W1ABC-10?bw=2300"
    assert build_connect_url("varahf", "W1ABC", None) == "varahf:///W1ABC"
    assert build_connect_url("varafm", "W1ABC-10", 2300) == "varafm:///W1ABC-10"   # no bw for FM
    assert build_connect_url("ardop", "W1ABC") == "ardop:///W1ABC"
    assert build_connect_url("telnet") == "telnet"
    assert build_connect_url("my-gateway") == "my-gateway"                        # a Pat alias


@pytest.mark.parametrize("mode,target,bw", [
    ("mercury", "", None),                 # radio modes need a callsign
    ("mercury", "W1ABC/../x", None),
    ("varahf", "W1ABC", 1200),             # not a VARA HF bandwidth
    ("bad mode!", "", None),
    ("telnet://evil:1/x", "", None),       # raw URLs only from config
])
def test_bad_connect_requests_are_refused(mode, target, bw):
    with pytest.raises(ValueError):
        build_connect_url(mode, target, bw)


def test_raw_url_allowed_from_config():
    a = PatWinlinkAdapter({"callsign": "n0call", "connect_alias": "varahf:///W1ABC?bw=500"})
    assert a.connect_url() == "varahf:///W1ABC?bw=500"
    with pytest.raises(ValueError):
        a.connect_url("telnet://x:1/y")


@pytest.fixture
async def pat_server():
    server = MockPatServer(host="127.0.0.1", port=0)
    await server.start()
    yield server
    await server.stop()


async def test_session_sends_url_form_value_pat_reads(pat_server):
    a = PatWinlinkAdapter({"callsign": "N0CALL", "pat_url": f"http://127.0.0.1:{pat_server.port}",
                           "connect_alias": "mercury", "connect_target": "w1abc-10", "bandwidth": 2300})
    await a.connect()
    try:
        r = await a._trigger_connect()
        assert r["ok"] and r["received"] == 0 and r["url"] == "varahf:///W1ABC-10?bw=2300"
        assert pat_server.connects == ["varahf:///W1ABC-10?bw=2300"]
        r = await a._trigger_connect("telnet")
        assert pat_server.connects[-1] == "telnet" and a.connect_info()["last"]["mode"] == "telnet"
        r = await a._trigger_connect("ardop", "")
        assert not r["ok"] and "callsign" in r["error"] and len(pat_server.connects) == 2
    finally:
        await a.disconnect()


async def test_one_session_at_a_time(pat_server):
    a = PatWinlinkAdapter({"callsign": "N0CALL", "pat_url": f"http://127.0.0.1:{pat_server.port}"})
    await a.connect()
    try:
        async with a._connect_lock:
            r = await a._trigger_connect()
            assert not r["ok"] and "already running" in r["error"]
            assert a.connect_info()["running"]
    finally:
        await a.disconnect()


def test_api_starts_session_and_reports_it():
    from unittest.mock import MagicMock
    from starlette.testclient import TestClient
    from ech.api.app import create_app

    router = MagicMock()
    router._adapters = {}
    with TestClient(create_app(router, MagicMock())) as c:
        server = MockPatServer(host="127.0.0.1", port=0)
        c.portal.call(server.start)      # same event loop as the app
        a = PatWinlinkAdapter({"callsign": "N0CALL", "pat_url": f"http://127.0.0.1:{server.port}"})
        router._adapters["winlink"] = a
        c.portal.call(a.connect)
        try:
            d = c.post("/api/winlink/connect", json={"mode": "mercury", "target": "W1ABC-10",
                                                     "bandwidth": 500}).json()
            assert d == {"status": "started", "url": "varahf:///W1ABC-10?bw=500"}
            for _ in range(50):
                info = c.get("/api/winlink/connect").json()
                if info["last"].get("finished"):
                    break
                c.portal.call(asyncio.sleep, 0.05)
            assert info["last"]["ok"] and "mercury" in info["modes"]
            assert server.connects == ["varahf:///W1ABC-10?bw=500"]
            d = c.post("/api/winlink/connect", json={"mode": "mercury"}).json()
            assert d["status"] == "error" and "callsign" in d["detail"]
            assert "connect" in c.get("/api/winlink/inbox").json()
        finally:
            c.portal.call(a.disconnect)
            c.portal.call(server.stop)
