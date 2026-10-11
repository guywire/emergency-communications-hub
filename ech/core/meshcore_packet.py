"""
ech/core/meshcore_packet.py
---------------------------
MeshCore over-the-air packet parsing and the PACKET HASH observers use.

The hash is what makes packet-level comparison possible: it identifies one
transmission no matter which station heard it or after how many hops,
because the relay path is NOT part of it. Ported line-for-line from
meshcore-packet-capture's `calculate_packet_hash` / `_decode_packed_path_length`
(agessaman/meshcore-packet-capture, src/meshcore_packet_capture/
packet_capture.py — itself "exactly like MeshCore Packet::calculatePacketHash()"),
the software LetsMesh-style observers run, so a hash computed here from a
packet this station heard matches the "hash" field other observers publish
for the same packet:

    hash = SHA-256( payload_type:1
                    [+ path_len as uint16 little-endian — TRACE (type 9) only]
                    + payload ).hexdigest()[:16].upper()

Wire layout (also matches meshcore_py's parser used in ech/adapters/meshcore.py):
header:1 [route b0-1, payload type b2-5] · 4-byte transport code when the
route is TRANSPORT_FLOOD(0)/TRANSPORT_DIRECT(3) · path_len:1 [hop count b0-5,
bytes-per-hop−1 b6-7] · path bytes · payload.
"""

from __future__ import annotations

import hashlib

PAYLOAD_TYPES = ("REQ", "RESPONSE", "TXT_MSG", "ACK", "ADVERT", "GRP_TXT", "GRP_DATA",
                 "ANON_REQ", "PATH", "TRACE", "MULTIPART", "CONTROL",
                 "Type12", "Type13", "Type14", "RAW_CUSTOM")
ROUTE_LETTERS = {0: "T", 1: "F", 2: "D", 3: "T"}   # observers' "route" field; 0 is transport-flood
_ROUTE_NAMES = {0: "TRANSPORT_FLOOD", 1: "FLOOD", 2: "DIRECT", 3: "TRANSPORT_DIRECT"}
_NULL_HASH = "0000000000000000"


def decode_path_length(path_len_byte: int, max_path_size: int = 64) -> tuple[int, int]:
    """(path byte length, bytes per hop). Mode 3 (4 bytes/hop) is reserved in
    the firmware and an oversized packed value is invalid — both fall back to
    the legacy one-byte-per-hop reading, exactly as the reference does."""
    hop_count = path_len_byte & 0x3F
    bytes_per_hop = (path_len_byte >> 6) + 1
    if bytes_per_hop == 4:
        return path_len_byte, 1
    path_byte_len = hop_count * bytes_per_hop
    if path_byte_len > max_path_size:
        return path_len_byte, 1
    return path_byte_len, bytes_per_hop


def parse(raw: bytes) -> dict | None:
    """Split an OTA packet into header fields, relay path and payload.
    None if it's too short to contain what its header claims."""
    if len(raw) < 2:
        return None
    header = raw[0]
    route_type = header & 0x03
    payload_type = (header >> 2) & 0x0F
    off = 1 + (4 if route_type in (0, 3) else 0)
    if len(raw) <= off:
        return None
    path_len_byte = raw[off]
    off += 1
    path_byte_len, per_hop = decode_path_length(path_len_byte)
    if off + path_byte_len > len(raw):
        return None
    path_bytes = raw[off:off + path_byte_len]
    path = [path_bytes[i:i + per_hop].hex().upper() for i in range(0, len(path_bytes), per_hop)]
    return {
        "route_type": route_type, "route": ROUTE_LETTERS[route_type], "route_name": _ROUTE_NAMES[route_type],
        "payload_type": payload_type, "payload_name": PAYLOAD_TYPES[payload_type],
        "path_len_byte": path_len_byte, "path": path, "payload": raw[off + path_byte_len:],
    }


def packet_hash(raw: bytes) -> str:
    """The observer packet hash (16 upper-case hex chars); all zeros if the
    packet is malformed — same sentinel the reference returns."""
    p = parse(raw)
    if p is None:
        return _NULL_HASH
    h = hashlib.sha256()
    h.update(bytes([p["payload_type"]]))
    if p["payload_type"] == 9:      # TRACE: path_len is part of the hash (as uint16 LE)
        h.update(p["path_len_byte"].to_bytes(2, "little"))
    h.update(p["payload"])
    return h.hexdigest()[:16].upper()


def packet_hash_hex(raw_hex: str) -> str:
    try:
        return packet_hash(bytes.fromhex(raw_hex.strip()))
    except ValueError:
        return _NULL_HASH
