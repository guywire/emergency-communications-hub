"""
tests/test_dmr_brandmeister.py
--------------------------------
Tests for DMRBrandmeisterAdapter.

The login-handshake byte layout was verified once against a REAL hblink3
master (see ECH_REQUIREMENTS_AND_PROGRESS.md's DMR section, O78) — that's
not repeatable in CI, so this file exercises the adapter against a small
local UDP mock master built from that same verified layout, matching this
repo's established pattern (see test_pat_winlink.py's MockPatServer) of
testing the real adapter against a local double rather than mocking the
adapter itself.
"""

import asyncio
from hashlib import sha256

import pytest

from ech.adapters.dmr_brandmeister import DMRBrandmeisterAdapter, _pad
from ech.core.models import NormalizedMessage


# ── Config validation ────────────────────────────────────────────────────

def test_requires_master_host():
    with pytest.raises(ValueError, match="master_host"):
        DMRBrandmeisterAdapter({
            "master_port": 54000, "passphrase": "x", "radio_id": 1, "callsign": "W1ABC",
        })


def test_requires_master_port():
    with pytest.raises(ValueError, match="master_port"):
        DMRBrandmeisterAdapter({
            "master_host": "127.0.0.1", "passphrase": "x", "radio_id": 1, "callsign": "W1ABC",
        })


def test_requires_passphrase():
    with pytest.raises(ValueError, match="passphrase"):
        DMRBrandmeisterAdapter({
            "master_host": "127.0.0.1", "master_port": 54000, "radio_id": 1, "callsign": "W1ABC",
        })


def test_requires_radio_id():
    with pytest.raises(ValueError, match="radio_id"):
        DMRBrandmeisterAdapter({
            "master_host": "127.0.0.1", "master_port": 54000, "passphrase": "x", "callsign": "W1ABC",
        })


def test_requires_callsign():
    with pytest.raises(ValueError, match="callsign"):
        DMRBrandmeisterAdapter({
            "master_host": "127.0.0.1", "master_port": 54000, "passphrase": "x", "radio_id": 1,
        })


def test_config_defaults():
    a = DMRBrandmeisterAdapter({
        "master_host": "127.0.0.1", "master_port": 54000,
        "passphrase": "x", "radio_id": 3129999, "callsign": "w1abc",
    })
    assert a._callsign == "W1ABC"           # uppercased
    assert a._radio_id == 3129999
    assert a._radio_id_bytes == (3129999).to_bytes(4, "big")
    assert a._slots == 1
    assert a._ping_interval == 5.0
    assert a._sms_gateway_id == 262995
    assert a._sms_format == "etsi_be"


def test_sms_format_motorola_accepted():
    a = DMRBrandmeisterAdapter({
        "master_host": "127.0.0.1", "master_port": 54000,
        "passphrase": "x", "radio_id": 1, "callsign": "W1ABC", "sms_format": "motorola",
    })
    assert a._sms_format == "motorola"


def test_sms_format_invalid_rejected():
    with pytest.raises(ValueError, match="sms_format"):
        DMRBrandmeisterAdapter({
            "master_host": "127.0.0.1", "master_port": 54000,
            "passphrase": "x", "radio_id": 1, "callsign": "W1ABC", "sms_format": "bogus",
        })


def test_registered_in_main():
    from ech.main import build_adapter
    a = build_adapter({
        "type": "dmr_brandmeister", "master_host": "127.0.0.1", "master_port": 54000,
        "passphrase": "x", "radio_id": 1, "callsign": "W1ABC",
    })
    assert isinstance(a, DMRBrandmeisterAdapter)


class _FakeTransport:
    """Records sendto() calls instead of touching a real socket, so send()
    can be tested (and its output fed straight into a receiver's
    _handle_dmrd()) without any network at all."""
    def __init__(self):
        self.sent: list[bytes] = []

    def sendto(self, data, addr=None):
        self.sent.append(data)


@pytest.mark.asyncio
async def test_send_transmits_dmrd_frames():
    a = DMRBrandmeisterAdapter({
        "master_host": "127.0.0.1", "master_port": 54000,
        "passphrase": "x", "radio_id": 3129999, "callsign": "W1ABC",
    })
    a._transport = _FakeTransport()
    a._connected = True
    msg = NormalizedMessage(source_adapter="dmr", source_channel="DMR", from_id="local",
                             body="Test message", to_id="262995")
    result = await a.send(msg)
    assert result is True
    assert a._tx_count == 1
    assert len(a._transport.sent) >= 3   # CSBK + header + at least one data block
    assert all(f[:4] == b"DMRD" for f in a._transport.sent)


@pytest.mark.asyncio
async def test_send_without_connection_fails():
    a = DMRBrandmeisterAdapter({
        "master_host": "127.0.0.1", "master_port": 54000,
        "passphrase": "x", "radio_id": 1, "callsign": "W1ABC",
    })
    msg = NormalizedMessage(source_adapter="dmr", source_channel="DMR", from_id="local",
                             body="test", to_id="262995")
    result = await a.send(msg)
    assert result is False


@pytest.mark.asyncio
async def test_send_then_receive_full_loopback():
    """The real end-to-end path: adapter A's send() builds DMRD frames,
    feed those frames straight into adapter B's _handle_dmrd() (as if B
    were the master relaying them), and confirm B reconstructs the exact
    original text via decode_sms_stream(). Exercises send() and receive()
    together, not just the codec in isolation."""
    sender = DMRBrandmeisterAdapter({
        "master_host": "127.0.0.1", "master_port": 54000,
        "passphrase": "x", "radio_id": 3129999, "callsign": "SENDER",
    })
    sender._transport = _FakeTransport()
    sender._connected = True

    receiver = DMRBrandmeisterAdapter({
        "master_host": "127.0.0.1", "master_port": 54000,
        "passphrase": "x", "radio_id": 262995, "callsign": "RECEIVER",
    })

    text = "This is a longer end to end loopback test spanning multiple data blocks."
    msg = NormalizedMessage(source_adapter="dmr", source_channel="DMR", from_id="local",
                             body=text, to_id="262995")
    await sender.send(msg)

    received = []
    orig_enqueue = receiver._enqueue
    async def capture(m):
        received.append(m)
    receiver._enqueue = capture

    for frame in sender._transport.sent:
        receiver._handle_dmrd(frame)
    await asyncio.sleep(0)   # let the ensure_future'd _enqueue coroutines run

    assert len(received) == 1
    assert received[0].body == text
    assert received[0].from_id == "3129999"


@pytest.mark.asyncio
async def test_send_then_receive_loopback_motorola_format():
    """Same as above but with sms_format: motorola on the sender — the
    receiver doesn't know or care which format was used (auto-detected in
    decode_sms_stream()), so this also proves that auto-detection actually
    works through the real adapter path, not just the codec directly."""
    sender = DMRBrandmeisterAdapter({
        "master_host": "127.0.0.1", "master_port": 54000,
        "passphrase": "x", "radio_id": 3129999, "callsign": "SENDER",
        "sms_format": "motorola",
    })
    sender._transport = _FakeTransport()
    sender._connected = True

    receiver = DMRBrandmeisterAdapter({
        "master_host": "127.0.0.1", "master_port": 54000,
        "passphrase": "x", "radio_id": 262995, "callsign": "RECEIVER",
    })

    text = "Motorola-format end to end loopback test"
    msg = NormalizedMessage(source_adapter="dmr", source_channel="DMR", from_id="local",
                             body=text, to_id="262995")
    await sender.send(msg)

    received = []
    async def capture(m):
        received.append(m)
    receiver._enqueue = capture

    for frame in sender._transport.sent:
        receiver._handle_dmrd(frame)
    await asyncio.sleep(0)

    assert len(received) == 1
    assert received[0].body == text


# ── Config block byte layout ─────────────────────────────────────────────

def test_pad_truncates_and_pads():
    assert _pad("ABC", 8) == b"ABC     "
    assert _pad("TOOLONGNAME", 4) == b"TOOL"


def test_config_block_length():
    """294 bytes of fields + 4-byte radio_id = 298; RPTC(4) + that = 302 on
    the wire, matching hblink3's own byte-offset table (verified 2026-08-16
    against a live master, not just computed)."""
    a = DMRBrandmeisterAdapter({
        "master_host": "127.0.0.1", "master_port": 54000,
        "passphrase": "x", "radio_id": 3129999, "callsign": "ECHTEST",
    })
    block = a._build_config_block()
    assert len(block) == 298
    assert block[:4] == a._radio_id_bytes
    assert block[4:12] == b"ECHTEST "   # CALLSIGN field, space-padded to 8


# ── Login handshake against a local mock master ──────────────────────────
# Mirrors hblink3's own byte layout exactly (const.py + hblink.py, verified
# live 2026-08-16) — not a from-scratch guess.

class MockHBMaster(asyncio.DatagramProtocol):
    def __init__(self, passphrase: bytes, reject_login=False, reject_auth=False):
        self.passphrase = passphrase
        self.reject_login = reject_login
        self.reject_auth = reject_auth
        self.transport = None
        self.salt = b"\x01\x02\x03\x04"
        self.logged_in_radio_id = None
        self.configured = False
        self.pings_received = 0

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        if data[:4] == b"RPTL":
            radio_id = data[4:8]
            if self.reject_login:
                self.transport.sendto(b"MSTNAK" + radio_id, addr)
                return
            self.logged_in_radio_id = radio_id
            self.transport.sendto(b"RPTACK" + self.salt, addr)
        elif data[:4] == b"RPTK":
            radio_id = data[4:8]
            sent_hash = data[8:]
            if self.reject_auth or sent_hash != sha256(self.salt + self.passphrase).digest():
                self.transport.sendto(b"MSTNAK" + radio_id, addr)
                return
            self.transport.sendto(b"RPTACK" + radio_id, addr)
        elif data[:4] == b"RPTC":
            radio_id = data[4:8]
            self.configured = True
            self.transport.sendto(b"RPTACK" + radio_id, addr)
        elif data[:7] == b"RPTPING":
            radio_id = data[7:11]
            self.pings_received += 1
            self.transport.sendto(b"MSTPONG" + radio_id, addr)


@pytest.fixture
async def mock_master():
    loop = asyncio.get_event_loop()
    proto = MockHBMaster(passphrase=b"testpass123")
    transport, _ = await loop.create_datagram_endpoint(lambda: proto, local_addr=("127.0.0.1", 0))
    port = transport.get_extra_info("sockname")[1]
    yield proto, port
    transport.close()


@pytest.mark.asyncio
async def test_full_login_handshake_succeeds(mock_master):
    proto, port = mock_master
    a = DMRBrandmeisterAdapter({
        "master_host": "127.0.0.1", "master_port": port,
        "passphrase": "testpass123", "radio_id": 3129999, "callsign": "ECHTEST",
        "ping_interval": 1000,  # don't fire during the test
    })
    await a.connect()
    assert a._connected
    assert proto.logged_in_radio_id == a._radio_id_bytes
    assert proto.configured
    await a.disconnect()


@pytest.mark.asyncio
async def test_login_rejected_raises():
    loop = asyncio.get_event_loop()
    proto = MockHBMaster(passphrase=b"testpass123", reject_login=True)
    transport, _ = await loop.create_datagram_endpoint(lambda: proto, local_addr=("127.0.0.1", 0))
    port = transport.get_extra_info("sockname")[1]
    try:
        a = DMRBrandmeisterAdapter({
            "master_host": "127.0.0.1", "master_port": port,
            "passphrase": "testpass123", "radio_id": 1, "callsign": "W1ABC",
        })
        with pytest.raises(ConnectionError, match="rejected"):
            await a.connect()
        assert not a._connected
    finally:
        transport.close()


@pytest.mark.asyncio
async def test_wrong_passphrase_rejected():
    loop = asyncio.get_event_loop()
    proto = MockHBMaster(passphrase=b"realpass")
    transport, _ = await loop.create_datagram_endpoint(lambda: proto, local_addr=("127.0.0.1", 0))
    port = transport.get_extra_info("sockname")[1]
    try:
        a = DMRBrandmeisterAdapter({
            "master_host": "127.0.0.1", "master_port": port,
            "passphrase": "wrongpass", "radio_id": 1, "callsign": "W1ABC",
        })
        with pytest.raises(ConnectionError, match="[Aa]uthentication"):
            await a.connect()
    finally:
        transport.close()


@pytest.mark.asyncio
async def test_keepalive_ping_answered(mock_master):
    proto, port = mock_master
    a = DMRBrandmeisterAdapter({
        "master_host": "127.0.0.1", "master_port": port,
        "passphrase": "testpass123", "radio_id": 3129999, "callsign": "ECHTEST",
        "ping_interval": 0.2,
    })
    await a.connect()
    await asyncio.sleep(0.7)
    assert proto.pings_received >= 2
    h = await a.health()
    assert h.state == "connected"
    await a.disconnect()


# ── DMRD frame parsing ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dmrd_data_frame_increments_counter_not_rx_content():
    a = DMRBrandmeisterAdapter({
        "master_host": "127.0.0.1", "master_port": 54000,
        "passphrase": "x", "radio_id": 1, "callsign": "W1ABC",
    })
    # DMRD(4) seq(1) rf_src(3) dst_id(3) peer_id(4) bits(1) stream_id(4) payload
    # bits=0x23 -> slot1, group... actually 0x23 & 0x40 == 0 (group), frame_type=(0x23&0x30)>>4=2 (data-sync)
    frame = (
        b"DMRD" + b"\x00" +
        (555).to_bytes(3, "big") +      # rf_src
        (262995).to_bytes(3, "big") +   # dst_id (SMS gateway)
        (1).to_bytes(4, "big") +        # peer_id
        bytes([0x23]) +                 # bits: data-sync, group
        b"\x00\x00\x00\x01" +           # stream_id
        b"\x00" * 33                    # payload
    )
    a._handle_dmrd(frame)
    assert a._data_frame_count == 1
    assert a._rx_count == 1
