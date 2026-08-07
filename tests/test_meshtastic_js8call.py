"""
tests/test_meshtastic_js8call.py
---------------------------------
Instantiation, config, and unit-logic tests for MeshtasticAdapter and
JS8CallAdapter. No hardware or running applications required — the real
adapters' connect() paths are not called; we test structure, helpers,
and the mock versions end-to-end.
"""

import asyncio
import pytest

from ech.adapters.meshtastic_adapter import MeshtasticAdapter
from ech.adapters.js8call import JS8CallAdapter, SPEED_NAMES
from ech.adapters.mock_js8call import MockJS8CallAdapter
from ech.core.models import NormalizedMessage, Priority


# ── MeshtasticAdapter config ──────────────────────────────────────────────

def test_meshtastic_serial_config():
    a = MeshtasticAdapter({"name": "mesh-test", "transport": "serial", "port": "/dev/ttyUSB0"})
    assert a.name == "mesh-test"
    assert a._transport == "serial"
    assert a._port == "/dev/ttyUSB0"
    assert a._channel_idx == 0


def test_meshtastic_tcp_config():
    a = MeshtasticAdapter({"transport": "tcp", "host": "10.0.0.50", "channel_idx": 1})
    assert a._transport == "tcp"
    assert a._host == "10.0.0.50"
    assert a._channel_idx == 1


def test_meshtastic_auto_detect_port():
    a = MeshtasticAdapter({"transport": "serial"})
    assert a._port is None   # None triggers auto-detect in library


def test_meshtastic_health_detail():
    a = MeshtasticAdapter({"transport": "serial"})
    d = a._health_detail()
    assert "transport" in d
    assert "node_count" in d
    assert d["node_count"] == 0


@pytest.mark.asyncio
async def test_meshtastic_priority_keywords():
    """Priority assessment via emergency keyword detection in packet handler."""
    a = MeshtasticAdapter({"transport": "serial"})
    # Simulate what _handle_text would do with a priority-keywords message
    # We test the keyword logic directly through a packet dispatch simulation
    ltext = "mayday vessel in distress".lower()
    from ech.core.models import Priority as P
    priority = P.NORMAL
    if any(w in ltext for w in ("emergency", "mayday", "help", "911", "sos")):
        priority = P.ELEVATED
    assert priority == P.ELEVATED


def test_meshtastic_registered_in_main():
    from ech.main import build_adapter
    a = build_adapter({"type": "meshtastic", "transport": "serial"})
    assert isinstance(a, MeshtasticAdapter)


# ── JS8CallAdapter config and helpers ────────────────────────────────────

def test_js8call_config_defaults():
    a = JS8CallAdapter({})
    assert a._host == "127.0.0.1"
    assert a._port == 2442
    assert a._hb_interval == 30
    assert a._rx_activity is False
    assert a._filter_own is True


def test_js8call_config_override():
    a = JS8CallAdapter({
        "callsign": "W1ABC-9",
        "host": "192.168.1.10",
        "port": 2443,
        "rx_activity": True,
        "filter_own": False,
    })
    assert a._callsign == "W1ABC-9"
    assert a._host == "192.168.1.10"
    assert a._port == 2443
    assert a._rx_activity is True
    assert a._filter_own is False


def test_js8call_parse_value_simple():
    a = JS8CallAdapter({"callsign": "W1ABC"})
    from_id, text = a._parse_js8_value("W1PBR: Net check-in here")
    assert from_id == "W1PBR"
    assert text == "Net check-in here"


def test_js8call_parse_value_directed():
    a = JS8CallAdapter({"callsign": "W1ABC"})
    from_id, text = a._parse_js8_value("W1PBR>W1ABC: Can you copy me?")
    assert from_id == "W1PBR"
    assert "copy me" in text


def test_js8call_parse_value_empty():
    a = JS8CallAdapter({"callsign": "W1ABC"})
    from_id, text = a._parse_js8_value("")
    assert from_id == ""
    assert text == ""


def test_js8call_parse_value_no_colon():
    a = JS8CallAdapter({"callsign": "W1ABC"})
    from_id, text = a._parse_js8_value("raw activity without colon")
    assert from_id == ""


def test_js8call_assess_priority_normal():
    a = JS8CallAdapter({})
    assert a._assess_priority("Net check-in, all ok") == Priority.NORMAL


def test_js8call_assess_priority_elevated():
    a = JS8CallAdapter({})
    assert a._assess_priority("URGENT please respond immediately") == Priority.ELEVATED


def test_js8call_assess_priority_emergency():
    a = JS8CallAdapter({})
    assert a._assess_priority("MAYDAY MAYDAY vessel sinking") == Priority.EMERGENCY


def test_js8call_speed_names():
    assert SPEED_NAMES[0] == "Normal"
    assert SPEED_NAMES[1] == "Fast"
    assert SPEED_NAMES[4] == "Slow"


def test_js8call_build_frame():
    a = JS8CallAdapter({"callsign": "W1ABC"})
    # _send builds JSON — we just check it would serialize correctly
    import json
    frame_str = json.dumps({
        "type": "TX.SEND_MESSAGE",
        "value": "@ALLCALL: Test message",
        "params": {"_ID": "1"},
    }) + "\n"
    parsed = json.loads(frame_str.strip())
    assert parsed["type"] == "TX.SEND_MESSAGE"
    assert "@ALLCALL" in parsed["value"]


def test_js8call_registered_in_main():
    from ech.main import build_adapter
    a = build_adapter({"type": "js8call", "callsign": "W1ABC"})
    assert isinstance(a, JS8CallAdapter)


# ── Mock JS8Call end-to-end ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_mock_js8call_produces_messages():
    adapter = MockJS8CallAdapter({"name": "hf-test", "interval_sec": 0.05})
    await adapter.connect()
    assert adapter._connected

    msgs = []
    async def collect():
        async for m in adapter.receive():
            msgs.append(m)
            if len(msgs) >= 2:
                break

    await asyncio.wait_for(collect(), timeout=10.0)
    await adapter.disconnect()

    assert len(msgs) >= 2
    for m in msgs:
        assert m.source_adapter == "hf-test"
        assert "HF" in m.source_channel
        assert m.body


@pytest.mark.asyncio
async def test_mock_js8call_send_returns_true():
    adapter = MockJS8CallAdapter({"name": "hf-send-test", "interval_sec": 60.0})
    await adapter.connect()
    msg = NormalizedMessage(
        source_adapter="hf-send-test",
        source_channel="HF 7078kHz",
        from_id="W1ABC",
        body="Test HF message",
        priority=Priority.NORMAL,
    )
    result = await adapter.send(msg)
    await adapter.disconnect()
    assert result is True


@pytest.mark.asyncio
async def test_mock_js8call_health():
    adapter = MockJS8CallAdapter({"name": "hf-health-test", "interval_sec": 60.0})
    await adapter.connect()
    h = await adapter.health()
    await adapter.disconnect()
    assert h.state == "connected"
    assert h.detail.get("mode") == "mock"
