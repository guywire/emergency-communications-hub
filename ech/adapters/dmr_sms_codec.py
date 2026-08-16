"""
ech/adapters/dmr_sms_codec.py
--------------------------------
DMR short-data (SMS) payload codec: builds and parses the sequence of DMRD
burst payloads that carry a text message over the air, per ETSI TS 102 361
unconfirmed data delivery — CSBK preamble, data header, BPTC(196,96)-coded
12-byte data blocks.

This is the piece explicitly deferred in earlier DMR research sessions (see
ECH_REQUIREMENTS_AND_PROGRESS.md's DMR section) — vendored and adapted from
two GPL-3.0 sources, with attribution:

  - kf7eel/hbnet (data_gateway.py) — the SMS framing logic itself: header
    format, CSBK construction, block-transfer-following (BTF) fragment
    counting, and the extended BPTC(196,96) decode that recovers a full
    192-bit data block instead of just the 96-bit Link Control subset
    dmr_utils3 exposes for voice. Every function below that has a
    "port of hbnet's X()" docstring line is a close port of that function,
    confirmed against hbnet's actual GitHub source (branch "hbnet") — not
    guessed from the spec.
  - n0mjs710/dmr_utils3 (bptc.py, utils.py) — the underlying BPTC(196,96)
    matrix interleave/encode primitives and 3/4-byte ID packing, used
    directly rather than re-vendored (already an ECH/hblink3 dependency).

Deliberate simplifications vs. hbnet (see module docstring in
dmr_brandmeister.py for the full status):
  - hbnet learns each destination's SMS decoding quirk (ETSI big-endian
    UTF-16, or a Motorola-specific variant) per-subscriber from observed
    traffic, persisted to a file. This codec does not auto-learn — instead
    `encode_sms()`/`decode_sms_stream()` take an explicit `sms_format`
    ("etsi_be", the default, or "motorola", ported from hbnet's own
    'motorola' branch of format_sms() — added 2026-08-16 after confirming
    real popular hardware, e.g. AnyTone via BrandMeister self-care's "Brand"
    setting, commonly needs Motorola framing rather than the ETSI standard).
    decode_sms_stream() auto-detects which of the two a received message
    used from its header shape, so the caller doesn't have to know in
    advance which format an inbound sender chose.
  - hbnet builds the IP/UDP header wrapper with `scapy`. This module hand-
    builds the 28-byte IPv4+UDP header instead (with real checksums) rather
    than adding scapy — a large, privileged-capable networking framework —
    as a dependency just for two fixed-size headers.
  - Only single-block CSBK preambles are built (`csbk_gen2`'s own loop caps
    at 1 anyway in hbnet's source); multi-CSBK preambles aren't a normal
    case for short SMS text.

Round-trip self-tests (`decode_sms_stream(encode_sms(...)) == original
text`) are the verification method here, matching how the module docstring
of dmr_brandmeister.py explains this codec's testing approach — there is no
live DMR SMS traffic available to verify against instead.
"""

from __future__ import annotations

import random
import struct

import libscrc
from bitarray import bitarray
from dmr_utils3 import bptc as _bptc
from dmr_utils3.utils import bytes_3, bytes_4

# ── Constants ────────────────────────────────────────────────────────────

_SMS_UDP_PORT = 5016          # ETSI TS 102 361-3 port for SMS-over-IP/UDP
_CSBK_PREFIX = bytes.fromhex("BD0080")  # CSBK opcode/FID for a data-preamble block, per hbnet's csbk_gen2
_CRC16_HEADER_XOR = 0xCCCC   # ETSI TS 102 361-1 CRC-16/CCITT mask for data headers
_CRC16_CSBK_XOR = 0xA5A5     # ETSI TS 102 361-1 CRC-16/CCITT mask for CSBKs

# DMR burst sync patterns (48 bits each), per ETSI TS 102 361-1 table —
# ported from hbnet's dmr_encode(): data bursts use the same fixed sync
# regardless of timeslot in hbnet's implementation.
_SYNC_L = bitarray("0111011100")
_SYNC_R = bitarray("1101110001")
_SYNC_DATA = bitarray("110101011101011111110111011111111101011101010111")


class SMSCodecError(ValueError):
    """Raised when encoding/decoding a DMR SMS payload fails."""


# ── Low-level: 12-byte data block <-> 33-byte DMR burst ────────────────────
# BPTC(196,96) is one fixed bit-matrix operation; dmr_utils3.bptc already
# implements the generic interleave/encode primitives (used for voice Link
# Control) — reused here directly for data blocks, which apply the same
# matrix math to different bits.

def _encode_block(data12: bytes) -> bytes:
    """12 raw bytes -> 33-byte DMR data burst (BPTC-coded + sync inserted).
    Port of hbnet's dmr_encode() (single block)."""
    if len(data12) != 12:
        raise SMSCodecError(f"data block must be 12 bytes, got {len(data12)}")
    coded = _bptc.interleave_19696(_bptc.encode_19696(data12))
    burst = coded[:98] + _SYNC_L + _SYNC_DATA + _SYNC_R + coded[98:]
    return burst.tobytes()


def _decode_block(burst33: bytes) -> bytes:
    """33-byte DMR data burst -> 12 raw bytes. Port of hbnet's
    bptc_decode()/decode_full() — decode_full is hbnet's own extension of
    dmr_utils3.bptc.decode_full_lc: the LC version only extracts the first
    96 information bits (voice Link Control never needs the rest — that
    region is normally the RS(12,9) FEC parity for LC), but a DATA block
    uses the full 192 raw bits, so hbnet's version continues past bit 96
    to recover the second half too. Bit indices below are copied verbatim
    from hbnet's decode_full() (not re-derived), since a single wrong index
    would silently corrupt every message rather than error out."""
    if len(burst33) != 33:
        raise SMSCodecError(f"DMR burst must be 33 bytes, got {len(burst33)}")
    bits = bitarray(endian="big")
    bits.frombytes(burst33)
    del bits[98:166]   # strip the 68-bit slot-type + sync field, leaving the 196-bit BPTC block

    out = bitarray(endian="big")
    out.extend([bits[136], bits[121], bits[106], bits[91], bits[76], bits[61], bits[46], bits[31]])
    out.extend([bits[152], bits[137], bits[122], bits[107], bits[92], bits[77], bits[62], bits[47], bits[32], bits[17], bits[2]])
    out.extend([bits[123], bits[108], bits[93], bits[78], bits[63], bits[48], bits[33], bits[18], bits[3], bits[184], bits[169]])
    out.extend([bits[94], bits[79], bits[64], bits[49], bits[34], bits[19], bits[4], bits[185], bits[170], bits[155], bits[140]])
    out.extend([bits[65], bits[50], bits[35], bits[20], bits[5], bits[186], bits[171], bits[156], bits[141], bits[126], bits[111]])
    out.extend([bits[36], bits[21], bits[6], bits[187], bits[172], bits[157], bits[142], bits[127], bits[112], bits[97], bits[82]])
    out.extend([bits[7], bits[188], bits[173], bits[158], bits[143], bits[128], bits[113], bits[98], bits[83]])
    out.extend([bits[68], bits[53], bits[174], bits[159], bits[144], bits[129], bits[114], bits[99], bits[84], bits[69], bits[54], bits[39]])
    out.extend([bits[24], bits[145], bits[130], bits[115], bits[100], bits[85], bits[70], bits[55], bits[40], bits[25], bits[10], bits[191]])
    return out.tobytes()


# ── CRC helpers ─────────────────────────────────────────────────────────
# CRC-16/CCITT (poly 0x1021, via libscrc.gsm16) with the ETSI-specified XOR
# mask applied to the result — port of hbnet's create_crc16()/create_crc16_csbk().

def _append_crc16(block11: bytes, xor_mask: int) -> bytes:
    crc = libscrc.gsm16(block11) ^ xor_mask
    return block11 + crc.to_bytes(2, "big")


# ── IPv4 + UDP header (replaces hbnet's scapy dependency) ──────────────────

def _ip_checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\x00"
    total = sum(struct.unpack(f"!{len(data)//2}H", data))
    while total > 0xFFFF:
        total = (total & 0xFFFF) + (total >> 16)
    return (~total) & 0xFFFF


def _dmr_id_to_ip(radio_id: int) -> bytes:
    """DMR SMS-over-IP uses the CAI convention: radio ID as the low 3 octets
    of a 12.x.x.x address (the '0c' prefix in hbnet's hex_2_ip_src/dst)."""
    return b"\x0c" + bytes_3(radio_id)


def _build_ip_udp_header(src_id: int, dst_id: int, seq_id: int, payload_len: int) -> bytes:
    """28-byte IPv4 (20) + UDP (8) header wrapping an SMS payload, per ETSI
    TS 102 361-3's SMS-over-IP/UDP framing (port 5016 both ends, TTL 1 —
    this never actually routes, the DMR layer is the only carrier)."""
    udp_len = 8 + payload_len
    ip_total_len = 20 + udp_len
    src_ip = _dmr_id_to_ip(src_id)
    dst_ip = _dmr_id_to_ip(dst_id)

    ip_header_no_csum = struct.pack(
        "!BBHHHBBH4s4s",
        0x45, 0x00, ip_total_len, seq_id & 0xFFFF, 0x0000,
        1, 17, 0,          # TTL=1, protocol=UDP(17), checksum placeholder
        src_ip, dst_ip,
    )
    ip_checksum = _ip_checksum(ip_header_no_csum)
    ip_header = struct.pack(
        "!BBHHHBBH4s4s",
        0x45, 0x00, ip_total_len, seq_id & 0xFFFF, 0x0000,
        1, 17, ip_checksum,
        src_ip, dst_ip,
    )
    udp_header = struct.pack("!HHHH", _SMS_UDP_PORT, _SMS_UDP_PORT, udp_len, 0)  # checksum optional over IPv4, set 0
    return ip_header + udp_header


def _strip_ip_udp_header(data: bytes) -> bytes:
    if len(data) < 28:
        raise SMSCodecError(f"reassembled SMS payload too short for IP/UDP header ({len(data)} bytes)")
    return data[28:]


# ── SMS text payload ─────────────────────────────────────────────────────
# Two known on-air framings for the text itself, both wrapped in the same
# IP/UDP header (_build_ip_udp_header always uses port 5016 either way,
# matching hbnet's format_sms(), which does too for both branches).

_ETSI_MARKER = b"\x00\x0d\x00\x0a"      # CR/LF as UTF-16BE code units
_MOTOROLA_TAIL = b"\x04\x0d\x00\x0a"    # fixed trailer inside Motorola's longer header


def _encode_text_payload(text: str, sms_format: str = "etsi_be") -> bytes:
    """Build the text portion of the SMS payload (after the IP/UDP header).
    Port of hbnet's format_sms(), 'etsi_be' or 'motorola' branch."""
    if sms_format == "etsi_be":
        return _ETSI_MARKER + text.encode("utf-16-be") + b"\x00"
    if sms_format == "motorola":
        # Header: 0x00, unk_count (~2*(len+4), hbnet's own comment calls the
        # exact meaning unconfirmed but the value correlates with length),
        # 0xa0 0x00, a semi-random sequence byte (128 + 1..7, per hbnet),
        # then the same 4-byte CR/LF trailer ETSI uses on its own (with the
        # leading byte changed from 0x00 to 0x04).
        unk_count = ((len(text) + 4) * 2) & 0xFF
        hdr_seq_num = 128 + random.randint(1, 7)
        header = bytes([0x00, unk_count, 0xA0, 0x00, hdr_seq_num]) + _MOTOROLA_TAIL
        return header + text.encode("utf-16-be") + b"\x00"
    raise SMSCodecError(f"unknown sms_format: {sms_format!r} (use 'etsi_be' or 'motorola')")


def _decode_text_payload(data: bytes) -> str:
    """Auto-detects ETSI vs. Motorola framing from the header shape — a
    receiver doesn't get to choose which format an inbound sender used."""
    if data[:4] == _ETSI_MARKER:
        body = data[4:]
    elif len(data) >= 9 and data[5:9] == _MOTOROLA_TAIL:
        body = data[9:]
    else:
        raise SMSCodecError(f"unrecognized SMS payload header: {data[:9]!r}")
    if body[-1:] == b"\x00":
        body = body[:-1]
    if len(body) % 2:
        body = body[:-1]   # drop a trailing odd byte rather than fail to decode
    return body.decode("utf-16-be", errors="replace")


# ── Fragmentation (BTF/POC) ──────────────────────────────────────────────

def _fragment(data: bytes) -> list[bytes]:
    """Split into 12-byte blocks, zero-padding the last one. Port of
    hbnet's btf_poc(), operating on bytes instead of hex strings."""
    blocks = [data[i:i + 12] for i in range(0, len(data), 12)]
    if not blocks:
        blocks = [b""]
    last = blocks[-1]
    if len(last) < 12:
        blocks[-1] = last + b"\x00" * (12 - len(last))
    return blocks


def _padding_octet_count(data_len: int, num_blocks: int) -> int:
    return num_blocks * 12 - data_len


# ── Data header + CSBK ────────────────────────────────────────────────────

def _build_header(dst_id: int, src_id: int, is_private: bool, poc: int, btf: int) -> bytes:
    """12-byte (pre-CRC) unconfirmed-data header. Port of hbnet's
    gen_header2(): byte0 = 0x02 (individual) or 0x82 (group) with DPF/poc
    packed into byte1's nibbles, then dst/src DMR IDs, then a response-
    requested bit + 7-bit block-count, then a fixed SAP/DPF trailer byte."""
    if not (0 <= poc <= 15):
        raise SMSCodecError(f"padding octet count out of range: {poc}")
    if not (0 <= btf <= 127):
        raise SMSCodecError(f"blocks-to-follow out of range: {btf}")
    byte0 = 0x02 if is_private else 0x82
    byte1 = 0x40 | poc   # upper nibble 4 = Unconfirmed Data DPF, lower nibble = poc
    resp_btf = 0x80 | btf   # bit7 = response requested (always set, matching hbnet)
    header = bytes([byte0, byte1]) + bytes_3(dst_id) + bytes_3(src_id) + bytes([resp_btf, 0x50])
    return _append_crc16(header, _CRC16_HEADER_XOR)


def _build_csbk(dst_id: int, src_id: int, total_blocks: int) -> bytes:
    """12-byte (pre-CRC) CSBK preamble announcing how many blocks follow
    (including the header itself). Port of hbnet's csbk_gen2()."""
    body = _CSBK_PREFIX + bytes([total_blocks & 0xFF]) + bytes_3(dst_id) + bytes_3(src_id)
    return _append_crc16(body, _CRC16_CSBK_XOR)


def _is_csbk(block12: bytes) -> bool:
    return block12[:3] == _CSBK_PREFIX


def _is_header(block12: bytes) -> bool:
    return block12[0] in (0x02, 0x82)


# ── Public API ─────────────────────────────────────────────────────────

def encode_sms(dst_id: int, src_id: int, text: str, is_private: bool,
                sms_format: str = "etsi_be") -> list[bytes]:
    """Build the ordered list of 33-byte DMR data-burst payloads for a
    text message — CSBK preamble first, then the header block, then the
    data blocks. Each entry is exactly what goes in a DMRD frame's
    payload field (dmr_brandmeister.py wraps these with the DMRD/seq/
    src/dst/peer/bits/stream_id header per-frame).

    sms_format: "etsi_be" (default, the actual standard) or "motorola"
    (what real popular hardware, e.g. AnyTone, commonly expects instead —
    see module docstring)."""
    ip_udp_payload = _encode_text_payload(text, sms_format)
    full_payload = _build_ip_udp_header(src_id, dst_id, random.randint(1, 0xFFFF), len(ip_udp_payload)) + ip_udp_payload

    data_blocks = _fragment(full_payload)
    poc = _padding_octet_count(len(full_payload), len(data_blocks))
    btf = len(data_blocks)   # blocks to follow, per hbnet: count of data blocks (header block is separate)

    header = _build_header(dst_id, src_id, is_private, poc, btf)
    csbk = _build_csbk(dst_id, src_id, btf + 1)   # +1 accounts for the header block itself

    all_blocks = [csbk, header] + data_blocks
    return [_encode_block(b) for b in all_blocks]


def decode_sms_stream(bursts: list[bytes]) -> str:
    """Reverse of encode_sms(): a full sequence of received 33-byte DMR
    bursts (CSBK + header + data blocks, in order) -> the original text.
    Raises SMSCodecError on anything that doesn't parse — callers should
    treat that as "not decodable yet" rather than guess at partial content."""
    if not bursts:
        raise SMSCodecError("empty burst sequence")

    blocks = [_decode_block(b) for b in bursts]
    blocks = [b for b in blocks if not _is_csbk(b)]
    if not blocks:
        raise SMSCodecError("no header/data blocks found (CSBK-only sequence)")

    header, data_blocks = blocks[0], blocks[1:]
    if not _is_header(header):
        raise SMSCodecError(f"expected a data header block, got {header[:2].hex()}")

    # header layout (12 bytes, matching _build_header): byte0, byte1(poc),
    # dst(3), src(3), resp_btf(1), 0x50, crc(2) — resp_btf is index 8.
    poc = header[1] & 0x0F
    btf = header[8] & 0x7F
    if len(data_blocks) < btf:
        raise SMSCodecError(f"incomplete sequence: header declared {btf} block(s), got {len(data_blocks)}")

    payload = b"".join(data_blocks[:btf])
    if poc:
        payload = payload[:-poc]

    text_payload = _strip_ip_udp_header(payload)
    return _decode_text_payload(text_payload)
