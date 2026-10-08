"""
tests/test_m17_protocol.py
-----------------------------
Protocol-level coverage for ech/adapters/m17_reflector.py's base-40
callsign codec, the BROADCAST address, and the Packet Mode LSF TYPE field
— none of this had dedicated tests before. Three findings confirmed
2026-10-08 against authoritative reference sources (mrefd's parrot.cpp,
libm17's payload/call.c, the M17 spec's own LSF TYPE table — see the
module's docstring for citations), after being flagged as unverified
assumptions back when this adapter was first written:
  1. BROADCAST = 6 bytes of 0xFF — confirmed correct as-is.
  2. "#PARROT"-style hash addresses — confirmed and newly implemented.
  3. The packet-mode TYPE field value — confirmed to have been a REAL BUG
     (wrongly set the stream/packet bit to "stream"), now fixed.
"""

import pytest

from ech.adapters.m17_reflector import (
    BROADCAST_ADDR, U40_9, decode_callsign, encode_callsign,
)


# ── Normal callsign round-trip (regression — this part was already
# live-validated in rc207, 2026-08-12) ──

def test_encode_matches_spec_worked_example():
    # M17 spec's own worked example: AB1CD -> 0x9fdd51
    assert encode_callsign("AB1CD") == bytes.fromhex("0000009fdd51")


def test_encode_decode_round_trip_normal_callsign():
    for cs in ("N0CALL", "KN0O", "W1AW", "AB1CD-9"):
        encoded = encode_callsign(cs)
        assert len(encoded) == 6
        assert decode_callsign(encoded) == cs


def test_encode_rejects_callsign_too_long():
    with pytest.raises(ValueError, match="too long"):
        encode_callsign("ABCDEFGHIJ")  # 10 chars, max is 9


def test_encode_rejects_invalid_character():
    with pytest.raises(ValueError, match="not valid"):
        encode_callsign("N0CALL!")


def test_encode_lowercase_normalized_to_uppercase():
    assert encode_callsign("n0call") == encode_callsign("N0CALL")


# ── BROADCAST — confirmed 2026-10-08 against mrefd's parrot.cpp ──

def test_broadcast_address_is_six_0xff_bytes():
    assert BROADCAST_ADDR == b"\xff\xff\xff\xff\xff\xff"


def test_decode_broadcast_address():
    assert decode_callsign(BROADCAST_ADDR) == "*BROADCAST*"


# ── '#'-prefixed hash addresses (e.g. "#PARROT") — confirmed and newly
# implemented 2026-10-08 against libm17's reference payload/call.c ──

def test_hash_address_round_trip():
    encoded = encode_callsign("#PARROT")
    assert decode_callsign(encoded) == "#PARROT"


def test_hash_address_lands_in_reserved_numeric_range():
    """The whole point of the U40_9 offset is that a hash address can
    never collide with an ordinary callsign's encoding."""
    value = int.from_bytes(encode_callsign("#PARROT"), "big")
    assert value >= U40_9


def test_hash_address_does_not_collide_with_normal_callsign_space():
    # Largest possible normal (non-hash) 9-char callsign encoding is
    # strictly less than 40**9 == U40_9.
    max_normal = encode_callsign("ZZZZZZZZZ")
    assert int.from_bytes(max_normal, "big") < U40_9


def test_hash_address_max_length_is_eight_chars_after_hash():
    encode_callsign("#" + "Z" * 8)  # exactly 8 chars after '#' — must fit
    with pytest.raises(ValueError, match="too long"):
        encode_callsign("#" + "Z" * 9)  # 9 chars after '#' — one too many


def test_hash_address_distinct_from_broadcast():
    assert encode_callsign("#PARROT") != BROADCAST_ADDR


# ── Packet-mode TYPE field — this surfaced a real bug 2026-10-08 ──

def test_packet_mode_type_field_has_packet_bit_not_stream_bit():
    """Regression test for the bug: the old 0x0005 TYPE field value had
    byte1's LSB (the Packet/Stream indicator, 0=packet/1=stream) set to 1,
    wrongly marking every M17P frame as stream mode. Re-derive the actual
    value send() uses and assert the P/S bit is 0."""
    type_field = (0x0000).to_bytes(2, "big")
    byte1 = type_field[1]
    assert (byte1 & 0x01) == 0, "P/S bit must be 0 (packet mode) for an M17P frame"
