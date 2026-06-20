"""
tests/test_pat_winlink.py
--------------------------
Tests for PatWinlinkAdapter and MockPatWinlinkAdapter.
Uses MockPatServer to exercise the real adapter against a local HTTP
server — no internet, no Pat installation required.
"""

import asyncio
import pytest

from ech.adapters.pat_winlink import PatWinlinkAdapter, _priority, _parse_pat_date
from ech.adapters.mock_pat_winlink import MockPatWinlinkAdapter, MockPatServer
from ech.core.models import NormalizedMessage, Priority


# ── Helper fixtures ───────────────────────────────────────────────────────

@pytest.fixture
async def pat_server():
    """Spin up a MockPatServer on a random port, yield it, then stop it."""
    server = MockPatServer(host="127.0.0.1", port=0)
    await server.start()
    yield server
    await server.stop()


# ── Utility function tests ────────────────────────────────────────────────

def test_priority_normal():
    assert _priority("Net check-in", "All stations report in") == Priority.NORMAL


def test_priority_elevated():
    assert _priority("URGENT resource request", "Need cots immediately") == Priority.ELEVATED


def test_priority_emergency():
    assert _priority("EMERGENCY TRAFFIC", "Mayday structure collapse") == Priority.EMERGENCY


def test_priority_keyword_in_body():
    assert _priority("Status update", "SOS need immediate assistance") == Priority.EMERGENCY


def test_parse_pat_date_iso():
    dt = _parse_pat_date("2024-05-10T14:30:00Z")
    assert dt.year == 2024
    assert dt.month == 5
    assert dt.day == 10


def test_parse_pat_date_empty():
    from datetime import datetime
    dt = _parse_pat_date("")
    assert isinstance(dt, datetime)


def test_parse_pat_date_bad():
    from datetime import datetime
    dt = _parse_pat_date("not-a-date")
    assert isinstance(dt, datetime)


# ── Config tests ──────────────────────────────────────────────────────────

def test_pat_adapter_requires_callsign():
    with pytest.raises(ValueError, match="callsign"):
        PatWinlinkAdapter({})


def test_pat_adapter_config_defaults():
    a = PatWinlinkAdapter({"callsign": "W1ABC"})
    assert a._callsign == "W1ABC"
    assert a._pat_url == "http://127.0.0.1:8080"
    assert a._poll_interval == 300
    assert a._auto_connect is False
    assert a._connect_alias == "telnet"


def test_pat_adapter_config_override():
    a = PatWinlinkAdapter({
        "callsign": "W1XYZ-9",
        "pat_url": "http://10.0.0.1:8080",
        "poll_interval": 60,
        "auto_connect": True,
        "connect_alias": "ardop",
    })
    assert a._callsign == "W1XYZ-9"
    assert a._pat_url == "http://10.0.0.1:8080"
    assert a._poll_interval == 60
    assert a._auto_connect is True
    assert a._connect_alias == "ardop"


def test_pat_registered_in_main():
    from ech.main import build_adapter
    a = build_adapter({"type": "pat_winlink", "callsign": "W1TEST"})
    assert isinstance(a, PatWinlinkAdapter)
    b = build_adapter({"type": "mock_pat_winlink", "callsign": "W1TEST"})
    assert isinstance(b, MockPatWinlinkAdapter)


# ── Real adapter against MockPatServer ───────────────────────────────────

@pytest.mark.asyncio
async def test_pat_adapter_connects_to_mock_server(pat_server):
    """PatWinlinkAdapter should connect and read the 3 seeded messages."""
    port = pat_server.port
    adapter = PatWinlinkAdapter({
        "callsign": "W1TEST",
        "pat_url": f"http://127.0.0.1:{port}",
        "poll_interval": 9999,   # don't auto-poll during test
        "auto_connect": False,
    })
    await adapter.connect()
    assert adapter._connected
    # All 3 seeded messages marked as seen on startup
    assert len(adapter._seen_mids) == 3
    await adapter.disconnect()


@pytest.mark.asyncio
async def test_pat_adapter_polls_new_messages(pat_server):
    """New messages added to the server after connect should be emitted on poll."""
    port = pat_server.port
    adapter = PatWinlinkAdapter({
        "callsign": "W1TEST",
        "pat_url": f"http://127.0.0.1:{port}",
        "poll_interval": 9999,
        "auto_connect": False,
    })
    await adapter.connect()

    # Add a new message to the mock server after connect
    new_mid = "NEWMSG001"
    pat_server.add_message(new_mid, {
        "mid": new_mid,
        "subject": "Emergency drill",
        "body": "All ARES units please check in via Winlink.",
        "from": "W1NET",
        "date": "2024-05-10T15:00:00Z",
        "files": [],
    })

    # Manually trigger a poll
    await adapter._poll_inbox()

    # Collect the emitted message from the queue
    msgs = []
    try:
        while True:
            msg = adapter._rx_queue.get_nowait()
            msgs.append(msg)
    except Exception:
        pass

    await adapter.disconnect()

    assert len(msgs) == 1
    assert msgs[0].from_id == "W1NET"
    assert "Emergency drill" in msgs[0].body
    assert new_mid in adapter._seen_mids


@pytest.mark.asyncio
async def test_pat_adapter_send_to_outbox(pat_server):
    """send() should POST to /api/mailbox/out."""
    port = pat_server.port
    adapter = PatWinlinkAdapter({
        "callsign": "W1TEST",
        "pat_url": f"http://127.0.0.1:{port}",
        "poll_interval": 9999,
        "auto_connect": False,
    })
    await adapter.connect()

    msg = NormalizedMessage(
        source_adapter="winlink",
        source_channel="Winlink",
        from_id="W1TEST",
        body="SITREP: shelter at 40% capacity, all ok.",
        to_id="W1EOC",
    )
    result = await adapter.send(msg)
    await adapter.disconnect()

    assert result is True
    assert len(pat_server.outbox) == 1
    assert pat_server.outbox[0]["to"] == "W1EOC"


@pytest.mark.asyncio
async def test_pat_adapter_send_without_to_id_fails(pat_server):
    """send() without to_id should return False."""
    port = pat_server.port
    adapter = PatWinlinkAdapter({
        "callsign": "W1TEST",
        "pat_url": f"http://127.0.0.1:{port}",
        "poll_interval": 9999,
    })
    await adapter.connect()
    msg = NormalizedMessage(
        source_adapter="winlink",
        source_channel="Winlink",
        from_id="W1TEST",
        body="No recipient",
    )
    result = await adapter.send(msg)
    await adapter.disconnect()
    assert result is False


@pytest.mark.asyncio
async def test_pat_adapter_unreachable_raises():
    """Connect should raise ConnectionError when Pat isn't running."""
    adapter = PatWinlinkAdapter({
        "callsign": "W1TEST",
        "pat_url": "http://127.0.0.1:19999",  # nothing listening here
        "poll_interval": 9999,
    })
    with pytest.raises(ConnectionError, match="Pat not reachable"):
        await adapter.connect()


@pytest.mark.asyncio
async def test_pat_adapter_health(pat_server):
    port = pat_server.port
    adapter = PatWinlinkAdapter({
        "callsign": "W1TEST",
        "pat_url": f"http://127.0.0.1:{port}",
        "poll_interval": 9999,
    })
    await adapter.connect()
    h = await adapter.health()
    await adapter.disconnect()
    assert h.state == "connected"
    assert h.detail["callsign"] == "W1TEST"
    assert "1.0.0-mock" in h.detail.get("pat_version", "")


# ── Mock adapter end-to-end ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_mock_winlink_produces_messages():
    adapter = MockPatWinlinkAdapter({
        "name": "wl-test",
        "callsign": "W1ABC",
        "interval_sec": 0.05,
    })
    await adapter.connect()

    msgs = []
    async def collect():
        async for m in adapter.receive():
            msgs.append(m)
            if len(msgs) >= 2:
                break

    await asyncio.wait_for(collect(), timeout=15.0)
    await adapter.disconnect()

    assert len(msgs) >= 2
    for m in msgs:
        assert m.source_adapter == "wl-test"
        assert m.source_channel == "Winlink"
        assert "[" in m.body   # subject prefix


@pytest.mark.asyncio
async def test_mock_winlink_send():
    adapter = MockPatWinlinkAdapter({"name": "wl-send-test", "callsign": "W1ABC"})
    await adapter.connect()
    msg = NormalizedMessage(
        source_adapter="wl-send-test",
        source_channel="Winlink",
        from_id="W1ABC",
        body="SITREP: all clear",
        to_id="W1EOC",
    )
    assert await adapter.send(msg) is True
    assert await adapter.send(NormalizedMessage(
        source_adapter="wl-send-test", source_channel="Winlink",
        from_id="W1ABC", body="no recipient"  # no to_id
    )) is False
    await adapter.disconnect()
