"""Channel messages only an observer heard -> Messages, without duplicates."""

import asyncio
import hashlib
import hmac
import struct

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from ech.adapters.meshcore import MeshCoreAdapter, PACKET_CHANNEL_MSG_V3
from ech.core.meshcore_packet import packet_hash
from ech.core.packet_compare import registry

KEY = bytes(range(16))


def _grp_txt(text: str, hops=(b"\xab\xcd",)) -> bytes:
    plain = struct.pack("<I", 1_700_000_000) + b"\x00" + text.encode()
    plain += b"\x00" * (-len(plain) % 16)
    enc = Cipher(algorithms.AES(KEY), modes.ECB()).encryptor()
    ct = enc.update(plain) + enc.finalize()
    mac = hmac.new(KEY + bytes(16), ct, hashlib.sha256).digest()[:2]
    hs = len(hops[0]) if hops else 1
    path = bytes([((hs - 1) << 6) | len(hops)]) + b"".join(hops)
    return bytes([(0x05 << 2) | 1]) + path + bytes([0x11]) + mac + ct


def _adapter():
    a = MeshCoreAdapter({"name": "mc-test", "transport": "serial", "port": "/dev/null",
                         "observer_msg_delay_sec": 0})
    a._channel_keys[0] = KEY + bytes(16)
    a._channels[0] = "Public"
    got = []

    async def fake_enqueue(m):
        got.append(m)
    a._enqueue = fake_enqueue
    return a, got


async def _run(a, pkt, obs="MQTT Listener"):
    a.note_observer_packet(pkt, obs, packet_hash(pkt))
    await asyncio.sleep(0.01)


def test_missed_message_shown_once_with_observer_tag():
    a, got = _adapter()
    pkt = _grp_txt("Bob: hello from afar")

    async def go():
        await _run(a, pkt)
        await _run(a, pkt, "Other observer")     # same packet again -> ignored
    asyncio.run(go())
    assert len(got) == 1
    m = got[0]
    assert (m.from_display, m.body, m.source_channel) == ("Bob", "hello from afar", "ch0:Public")
    assert m.raw["via_observer"] == "MQTT Listener" and m.hop_count == 1 and m.path == "abcd"


def test_skipped_when_our_radio_heard_the_packet():
    a, got = _adapter()
    pkt = _grp_txt("Bob: local one")
    registry.record_local(pkt, 5.0, -90)
    asyncio.run(_run(a, pkt))
    assert got == []


def test_skipped_when_radio_got_same_text():
    a, got = _adapter()
    raw = bytes([20, 0, 0, 0, 1, 0]) + struct.pack("<I", 1_700_000_000) + b"Bob: dup test"
    asyncio.run(a._handle_channel_msg(raw, PACKET_CHANNEL_MSG_V3))
    asyncio.run(_run(a, _grp_txt("Bob: dup test")))
    assert [m.body for m in got] == ["dup test"]


def test_unknown_channel_ignored():
    a, got = _adapter()
    a._channel_keys = {0: bytes(32)}
    asyncio.run(_run(a, _grp_txt("Bob: secret")))
    assert got == []
