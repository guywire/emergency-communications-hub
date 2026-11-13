"""Remote extensions on a second PBX reached over an IAX2 trunk (v1.1.45)."""

import asyncio

from ech.adapters.asterisk_adapter import AsteriskAdapter


class _Writer:
    def __init__(self):
        self.buf = b""

    def write(self, b):
        self.buf += b

    async def drain(self):
        pass


def _adapter(**extra):
    cfg = {"name": "pbx", "ami_secret": "x", "local_extension": "101",
           "remote_extensions": ["201", "202", "bad&PJSIP/1"],
           "remote_iax_peer": "mesh-pi2", "remote_pbx_label": "AREDN PBX", **extra}
    a = AsteriskAdapter(cfg)
    a._writer = _Writer()
    a._connected = True
    return a


def _feed(a, *pkts):
    """Run the event loop over canned AMI packets, then stop."""
    queue = list(pkts)

    async def read():
        if not queue:
            a._connected = False
            return {}
        return queue.pop(0)

    a._read_packet = read
    asyncio.run(a._run())


def test_invalid_remote_extensions_are_dropped():
    assert _adapter().remote_extensions() == ["201", "202"]


def test_no_remote_config_means_no_remote_rows():
    a = AsteriskAdapter({"name": "pbx", "ami_secret": "x"})
    assert a.list_remote_status() == []


def test_remote_status_follows_trunk_qualify():
    a = _adapter()
    assert [e["device_state"] for e in a.list_remote_status()] == ["trunk unknown"] * 2
    _feed(a,
          {"Event": "PeerEntry", "Channeltype": "IAX", "ObjectName": "hoip-iax", "Status": "OK (74 ms)"},
          {"Event": "PeerEntry", "Channeltype": "IAX", "ObjectName": "mesh-pi2", "Status": "OK (9 ms)"},
          {"Event": "PeerlistComplete"})
    rows = a.list_remote_status()
    assert all(r["online"] and r["remote"] and r["pbx"] == "AREDN PBX" for r in rows)
    assert rows[0]["device_state"] == "trunk OK (9 ms)"

    a._connected = True
    _feed(a,
          {"Event": "PeerEntry", "Channeltype": "IAX", "ObjectName": "mesh-pi2", "Status": "UNREACHABLE"},
          {"Event": "PeerlistComplete"})
    assert not any(r["online"] for r in a.list_remote_status())


def test_unmonitored_trunk_is_not_shown_online():
    a = _adapter()
    _feed(a, {"Event": "PeerEntry", "Channeltype": "IAX", "ObjectName": "mesh-pi2", "Status": "Unmonitored"},
          {"Event": "PeerlistComplete"})
    r = a.list_remote_status()[0]
    assert not r["online"] and r["device_state"] == "trunk unmonitored"


def test_click_to_call_remote_goes_through_dialplan():
    a = _adapter()
    assert asyncio.run(a.originate("201", "101"))
    act = a._writer.buf.decode()
    assert "Exten: 201" in act and "Context: from-internal" in act and "Channel: PJSIP/101" in act


def test_api_lists_remote_and_refuses_intercom_tts_to_them():
    from unittest.mock import MagicMock
    from starlette.testclient import TestClient
    from ech.api.app import create_app
    from ech.adapters.mock_asterisk import MockAsteriskAdapter
    pbx = MockAsteriskAdapter({"name": "pbx", "local_extension": "101"})
    pbx._connected = True
    router = MagicMock()
    router._adapters = {"pbx": pbx}
    with TestClient(create_app(router, MagicMock())) as c:
        d = c.get("/api/pbx/endpoints").json()
        assert [e["extension"] for e in d["remote"]] == ["201", "202"]
        assert all(e["remote"] for e in d["remote"])
        d = c.post("/api/pbx/intercom", json={"targets": ["201"], "text": "hi"}).json()
        assert d["status"] == "error" and "calls work" in d["detail"]
        d = c.post("/api/pbx/call", json={"destination": "201", "caller_extension": "102"}).json()
        assert d["status"] == "ok"
        xml = c.get("/phonebook.xml").text
        assert "<Telephone>201</Telephone>" in xml and "<Telephone>104</Telephone>" in xml
        assert "<Telephone>105</Telephone>" not in xml    # live list (101-104) replaces static roster
