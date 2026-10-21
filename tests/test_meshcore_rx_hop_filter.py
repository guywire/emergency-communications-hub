"""MeshCore rx_max_hops: drop far-relayed channel messages, never DMs."""

import asyncio
import struct

from ech.adapters.meshcore import MeshCoreAdapter, PACKET_CHANNEL_MSG_V3


def _adapter(**cfg):
    a = MeshCoreAdapter({"name": "mc-test", "transport": "serial", "port": "/dev/null", **cfg})
    got = []

    async def fake_enqueue(msg):
        got.append(msg)
    a._enqueue = fake_enqueue
    return a, got


def _ch_v3(hops: int, text: str = "Faraway: hello") -> bytes:
    # snr, reserved x2, ch_idx, path_len, txt_type, ts, text
    return bytes([20, 0, 0, 0, hops, 0]) + struct.pack("<I", 1_700_000_000) + text.encode()


def _dm_v3(hops: int) -> bytes:
    return (bytes([20, 0, 0]) + bytes.fromhex("AABBCCDDEEFF") + bytes([hops, 0])
            + struct.pack("<I", 1_700_000_000) + b"hi")


def test_off_by_default_passes_everything():
    a, got = _adapter()
    asyncio.run(a._handle_channel_msg(_ch_v3(9), PACKET_CHANNEL_MSG_V3))
    assert len(got) == 1 and got[0].hop_count == 9


def test_far_channel_msgs_dropped_near_kept():
    a, got = _adapter(rx_max_hops=3)
    asyncio.run(a._handle_channel_msg(_ch_v3(3, "Near: ok"), PACKET_CHANNEL_MSG_V3))
    asyncio.run(a._handle_channel_msg(_ch_v3(4, "Far: ducted"), PACKET_CHANNEL_MSG_V3))
    assert [m.body for m in got] == ["ok"]
    assert a._rx_hop_filtered == 1


def test_unknown_hop_count_passes():
    a, got = _adapter(rx_max_hops=1)
    asyncio.run(a._handle_channel_msg(_ch_v3(255), PACKET_CHANNEL_MSG_V3))
    assert len(got) == 1


def test_dms_never_filtered():
    a, got = _adapter(rx_max_hops=1)
    asyncio.run(a._handle_contact_msg(_dm_v3(8), v3=True))
    assert len(got) == 1


# ── Region (transport code) labelling ─────────────────────────────────────

from ech.adapters.meshcore import region_key, region_transport_code


def _grp_txt_pkt(code0: int, route_type: int = 0, payload: bytes = b"\x11" * 20) -> bytes:
    header = (0x05 << 2) | route_type             # GRP_TXT
    tc = code0.to_bytes(2, "little") + b"\x00\x00" if route_type in (0, 3) else b""
    return bytes([header]) + tc + bytes([0]) + payload


def _hear(a, pkt, snr=5.0):
    a._router_broadcast = lambda *args, **kw: None
    a._recent_log_data.append({"snr": snr, "rssi": -90})
    a._emit_rf_traffic(pkt, snr, -90)
    return a._recent_log_data[-1].get("region")


def test_region_key_matches_firmware_hashtag_rule():
    import hashlib
    assert region_key("RKD") == hashlib.sha256(b"#RKD").digest()[:16]
    assert region_key("#RKD") == region_key("RKD")


def test_heard_packet_labelled_with_known_region():
    a, _ = _adapter(region="RKD", known_regions=["MSP"])
    payload = b"\x22" * 20
    code = region_transport_code(region_key("MSP"), 0x05, payload)
    assert _hear(a, _grp_txt_pkt(code, payload=payload)) == "MSP"


def test_unscoped_and_unknown_regions():
    a, _ = _adapter(region="RKD")
    assert _hear(a, _grp_txt_pkt(0, route_type=1)) == ""          # legacy flood
    assert _hear(a, _grp_txt_pkt(0)) == ""                        # TC_FLOOD, null scope
    assert _hear(a, _grp_txt_pkt(0x1234)) == "?1234"


def test_region_attached_to_channel_message():
    a, got = _adapter(region="RKD")
    payload = b"\x33" * 20
    code = region_transport_code(region_key("RKD"), 0x05, payload)
    _hear(a, _grp_txt_pkt(code, payload=payload), snr=5.0)
    asyncio.run(a._handle_channel_msg(_ch_v3(0, "Local: hi"), PACKET_CHANNEL_MSG_V3))
    assert got[0].raw["region"] == "RKD"


def test_long_ducting_paths_keep_their_hop_count():
    from ech.adapters.meshcore import _hop_count
    assert _hop_count(0x40 | 25) == 25        # 25 hops, 2-byte hashes (50 path bytes)
    assert _hop_count(22) == 22               # 1-byte hashes
    assert _hop_count(0x80 | 20) == 20        # 3-byte, 60 bytes
    assert _hop_count(0x80 | 22) is None      # 66 bytes > MAX_PATH_SIZE
    assert _hop_count(255) is None            # unknown / direct sentinel
    a, got = _adapter()
    asyncio.run(a._handle_channel_msg(_ch_v3(0x40 | 21), PACKET_CHANNEL_MSG_V3))
    assert got[0].hop_count == 21
