"""
tests/test_packet_compare.py
----------------------------
Packet-level comparison (local vs other MeshCore observers): the observer
packet hash is path-independent (verified separately against the
meshcore-packet-capture reference on 20,000 random packets), so the same
transmission heard after different hops matches; our own echo via a broker
is ignored; the bridge publishes raw packets in observer format.
"""

import asyncio
import hashlib
import json

from ech.core.meshcore_packet import packet_hash, parse
from ech.core.packet_compare import PacketCompare


def _pkt(payload_type: int, route: int, path: bytes, payload: bytes, per_hop: int = 1) -> bytes:
    header = bytes([(payload_type << 2) | route])
    transport = b"\x00\x00\x00\x00" if route in (0, 3) else b""
    path_len = (len(path) // per_hop) | ((per_hop - 1) << 6)
    return header + transport + bytes([path_len]) + path + payload


PAYLOAD = b"\x11\x22encrypted-channel-text"


def test_hash_is_path_independent_and_matches_definition():
    a = _pkt(5, 1, b"", PAYLOAD)                      # heard direct
    b = _pkt(5, 1, b"\xc2\xe2\x7a", PAYLOAD)           # heard after 3 relays
    c = _pkt(5, 1, b"\xc2\x01\xe2\x02", PAYLOAD, 2)    # 2-byte relay hashes
    expected = hashlib.sha256(bytes([5]) + PAYLOAD).hexdigest()[:16].upper()
    assert packet_hash(a) == packet_hash(b) == packet_hash(c) == expected
    assert parse(c)["path"] == ["C201", "E202"]
    # TRACE: path length is part of the hash
    t1, t2 = _pkt(9, 2, b"\x01", PAYLOAD), _pkt(9, 2, b"\x01\x02", PAYLOAD)
    assert packet_hash(t1) != packet_hash(t2)


def test_local_vs_observers_matching_and_own_echo():
    pc = PacketCompare()
    pc.set_own_id("C65FDBF8C77E")
    heard = _pkt(5, 1, b"\xc2", PAYLOAD)
    missed = _pkt(4, 1, b"", b"\x01" * 40)
    pc.record_local(heard, 9.5, -80, "meshcore-usb")
    pc.record_local(_pkt(2, 2, b"", b"dm-only-we-heard"), 3.0, -100)
    # Another observer heard the same packet via a different path (raw given)
    pc.record_observer({"origin_id": "AB" * 32, "origin": "Obs1", "raw": _pkt(5, 1, b"\x7a\xc2", PAYLOAD).hex(),
                        "hash": "ignored-recomputed"}, source="observers-compare")
    # …and one we never heard (hash only, no raw)
    pc.record_observer({"origin_id": "CD" * 32, "origin": "Obs2", "hash": packet_hash(missed), "packet_type": "4"})
    # Our own report coming back through the broker is not "another observer"
    assert pc.record_observer({"origin_id": "C65FDBF8C77E" + "0" * 52, "raw": heard.hex()}) is None
    s = pc.summary()
    assert (s["heard_by_both"], s["local_only"], s["observers_only"]) == (1, 1, 1)
    assert s["local_coverage_pct"] == 50.0
    assert s["by_type"]["ADVERT"]["observers_only"] == 1 and s["by_type"]["GRP_TXT"]["both"] == 1
    missing = pc.recent(only="observers_only")
    assert [m["type"] for m in missing] == ["ADVERT"] and missing[0]["observers"][0]["name"] == "Obs2"


def test_bridge_publishes_raw_packet_in_observer_format():
    from ech.core.meshcore_bridge import MeshCoreMQTTBridge
    b = MeshCoreMQTTBridge({"meshcore_bridge": {"enabled": True, "iata_code": "rkd"}})
    b._device_pubkey = "AB" * 32
    sent = []

    class Client:
        async def publish(self, topic, payload, qos=0):
            sent.append((topic, json.loads(payload)))

    raw = _pkt(5, 1, b"\xc2", PAYLOAD)
    asyncio.run(b._publish_raw_packet(Client(), raw, 9.5, -80, packet_hash(raw)))
    topic, pkt = sent[0]
    assert topic == f"meshcore/RKD/{'AB' * 32}/packets"
    assert pkt["raw"] == raw.hex().upper() and pkt["hash"] == packet_hash(raw)
    assert pkt["packet_type"] == "5" and pkt["route"] == "F" and pkt["type"] == "PACKET"
    assert "body" not in pkt                                   # no decrypted text in raw packets
    assert b._publish_decoded is False                         # decoded publishing is opt-in
