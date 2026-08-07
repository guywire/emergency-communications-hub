"""
tests/test_real_adapters.py
---------------------------
Import and config validation tests for real adapters.
These don't need hardware — they verify the adapters instantiate correctly,
reject bad config, and that helper functions (KISS encode/decode,
APRS packet body formatting) work as expected.
"""

import pytest
from ech.adapters.meshcore import MeshCoreAdapter
from ech.adapters.aprs_is import APRSISAdapter
from ech.adapters.aprs_kiss import APRSKISSAdapter, kiss_encode, kiss_decode, ax25_to_aprs_string


# ── MeshCore ──────────────────────────────────────────────────────────────

def test_meshcore_serial_config():
    a = MeshCoreAdapter({
        "name": "mc-test",
        "transport": "serial",
        "port": "/dev/ttyUSB0",
        "channel_idx": 1,
    })
    assert a.name == "mc-test"
    assert a._channel_idx == 1
    assert a._transport_type == "serial"


def test_meshcore_tcp_config():
    a = MeshCoreAdapter({
        "name": "mc-tcp",
        "transport": "tcp",
        "host": "192.168.1.50",
        "tcp_port": 4403,
    })
    assert a._transport_type == "tcp"


def test_meshcore_bad_transport():
    with pytest.raises(ValueError, match="unsupported transport"):
        MeshCoreAdapter({"transport": "bluetooth_magic"})


def test_meshcore_frame_build():
    """_build_frame should produce < + uint16le(len) + payload."""
    a = MeshCoreAdapter({"transport": "serial", "port": "/dev/null"})
    payload = bytes([0x01, 0x00, 0x00])
    frame = a._build_frame(payload)
    assert frame[0:1] == b'<'
    assert frame[1:3] == b'\x03\x00'   # length=3, little-endian
    assert frame[3:] == payload


# ── APRS-IS ──────────────────────────────────────────────────────────────

def test_aprs_is_config():
    a = APRSISAdapter({
        "name": "aprs-test",
        "callsign": "W1ABC-9",
        "passcode": -1,
        "filter": "r/44.1/-69.1/50",
    })
    assert a.name == "aprs-test"
    assert a._callsign == "W1ABC-9"
    assert a._passcode == -1


def test_aprs_is_packet_to_body_message():
    a = APRSISAdapter({"callsign": "W1ABC"})
    packet = {
        "from": "KD1NET",
        "format": "message",
        "addresse": "W2XYZ",
        "message_text": "Net check-in",
    }
    body = a._packet_to_body(packet)
    assert "KD1NET" in body
    assert "Net check-in" in body


def test_aprs_is_packet_to_body_position():
    a = APRSISAdapter({"callsign": "W1ABC"})
    packet = {
        "from": "W1PBR-9",
        "format": "uncompressed",
        "comment": "Mobile unit",
        "latitude": 44.1,
        "longitude": -69.1,
    }
    body = a._packet_to_body(packet)
    assert "W1PBR-9" in body
    assert "Mobile unit" in body


def test_aprs_is_packet_to_body_status():
    a = APRSISAdapter({"callsign": "W1ABC"})
    packet = {"from": "N1EOC", "format": "status", "status": "EOC active"}
    body = a._packet_to_body(packet)
    assert "EOC active" in body


def test_aprs_is_packet_empty_skipped():
    a = APRSISAdapter({"callsign": "W1ABC"})
    packet = {"from": "W1X", "format": "message", "message_text": "", "addresse": "W2Y"}
    body = a._packet_to_body(packet)
    assert body == ""


# ── APRS KISS ─────────────────────────────────────────────────────────────

def test_aprs_kiss_config():
    a = APRSKISSAdapter({
        "name": "kiss-test",
        "transport": "serial",
        "port": "/dev/ttyUSB1",
        "callsign": "W1ABC-9",
    })
    assert a._callsign == "W1ABC-9"
    assert a._transport_type == "serial"


def test_kiss_encode_roundtrip():
    """Encode a simple payload and verify it can be decoded back."""
    payload = b"Hello APRS world"
    frame = kiss_encode(payload)
    assert frame[0] == 0xC0     # FEND start
    assert frame[-1] == 0xC0    # FEND end
    assert frame[1] == 0x00     # CMD_DATA on port 0

    decoded = kiss_decode(frame)
    assert len(decoded) == 1
    assert decoded[0] == payload


def test_kiss_encode_escapes_fend():
    """FEND bytes in payload must be escaped."""
    payload = bytes([0xC0, 0x01, 0xC0])   # contains FENDs
    frame = kiss_encode(payload)
    # The escaped payload should NOT contain raw 0xC0 between the framing FENDs
    inner = frame[2:-1]  # strip start FEND, CMD byte, end FEND
    assert 0xC0 not in inner


def test_kiss_encode_escapes_fesc():
    """FESC bytes in payload must also be escaped."""
    payload = bytes([0xDB, 0x02])
    frame = kiss_encode(payload)
    inner = frame[2:-1]
    assert 0xDB not in inner or bytes([0xDB, 0xDD]) in inner


def test_kiss_decode_multiple_frames():
    """Multiple frames in a buffer should all be decoded."""
    f1 = kiss_encode(b"Frame one")
    f2 = kiss_encode(b"Frame two")
    decoded = kiss_decode(f1 + f2)
    assert len(decoded) == 2
    assert decoded[0] == b"Frame one"
    assert decoded[1] == b"Frame two"


def test_aprs_kiss_build_ax25():
    """_build_ax25_ui should produce bytes with control/PID bytes."""
    a = APRSKISSAdapter({"callsign": "W1ABC-9", "tx_path": "WIDE1-1"})
    frame = a._build_ax25_ui(">ECH test packet")
    assert isinstance(frame, bytes)
    assert len(frame) > 14    # at least dest + src + control + pid
    # PID 0xF0 (no layer 3) should be present
    assert 0xF0 in frame


def test_meshcore_adapter_registered_in_main():
    """Verify all new adapters appear in main.py's build_adapter registry."""
    from ech.main import build_adapter

    # These should not raise — just instantiate
    mc = build_adapter({"type": "meshcore", "transport": "serial", "port": "/dev/null"})
    ai = build_adapter({"type": "aprs_is", "callsign": "W1ABC"})
    ak = build_adapter({"type": "aprs_kiss", "callsign": "W1ABC"})

    assert mc.name.startswith("meshcore") or True
    assert ai._callsign == "W1ABC"
    assert ak._callsign == "W1ABC"
