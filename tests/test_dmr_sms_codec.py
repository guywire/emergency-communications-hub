"""
tests/test_dmr_sms_codec.py
------------------------------
Round-trip tests for the DMR SMS codec (ech/adapters/dmr_sms_codec.py).

There's no live DMR SMS traffic to verify against, so round-tripping
(encode then decode recovers the original text) is the verification
method — see the codec module's own docstring for why. A real
over-the-wire test (two DMRBrandmeisterAdapter instances relaying a
message through a live hblink3 master) was also run manually during
development (2026-08-16) and passed, but isn't repeatable in CI.
"""

import pytest

from ech.adapters.dmr_sms_codec import (
    SMSCodecError,
    _decode_block,
    _encode_block,
    decode_sms_stream,
    encode_sms,
)


# ── BPTC block round-trip (the actual FEC math) ──────────────────────────

def test_bptc_block_roundtrip_zeros():
    data = b"\x00" * 12
    assert _decode_block(_encode_block(data)) == data


def test_bptc_block_roundtrip_arbitrary():
    data = bytes(range(12))
    assert _decode_block(_encode_block(data)) == data


def test_bptc_block_roundtrip_all_ff():
    data = b"\xff" * 12
    assert _decode_block(_encode_block(data)) == data


def test_encode_block_rejects_wrong_length():
    with pytest.raises(SMSCodecError):
        _encode_block(b"\x00" * 11)


def test_decode_block_rejects_wrong_length():
    with pytest.raises(SMSCodecError):
        _decode_block(b"\x00" * 32)


# ── Full SMS round-trip ───────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "Hello world",
    "a",
    "",
    "Emergency: shelter full, redirect to backup site on Main St",
    "Emoji test: \U0001F692\U0001F525",
    "x" * 500,   # long enough to force many data blocks
])
def test_sms_roundtrip(text):
    bursts = encode_sms(262995, 3129999, text, is_private=True)
    assert all(len(b) == 33 for b in bursts)
    recovered = decode_sms_stream(bursts)
    assert recovered == text


def test_sms_roundtrip_group_call():
    text = "Group call SMS test"
    bursts = encode_sms(9, 3129999, text, is_private=False)
    assert decode_sms_stream(bursts) == text


@pytest.mark.parametrize("text", [
    "Hello world",
    "a",
    "Motorola format test with a longer message spanning blocks",
    "Emoji test: \U0001F692\U0001F525",
])
def test_sms_roundtrip_motorola_format(text):
    bursts = encode_sms(262995, 3129999, text, is_private=True, sms_format="motorola")
    recovered = decode_sms_stream(bursts)
    assert recovered == text


def test_motorola_and_etsi_produce_different_bytes():
    """Sanity check that sms_format actually changes the on-air framing,
    not just a label — otherwise the whole feature would be a no-op."""
    text = "same text, different format"
    etsi = encode_sms(1, 2, text, True, sms_format="etsi_be")
    moto = encode_sms(1, 2, text, True, sms_format="motorola")
    assert etsi != moto


def test_unknown_sms_format_rejected():
    with pytest.raises(SMSCodecError):
        encode_sms(1, 2, "x", True, sms_format="bogus")


def test_block_count_scales_with_length():
    short = encode_sms(1, 2, "hi", True)
    long = encode_sms(1, 2, "x" * 200, True)
    assert len(long) > len(short)


def test_decode_incomplete_sequence_raises():
    bursts = encode_sms(1, 2, "a longer message needing multiple blocks here", True)
    with pytest.raises(SMSCodecError):
        decode_sms_stream(bursts[:-1])   # drop the last data block


def test_decode_empty_raises():
    with pytest.raises(SMSCodecError):
        decode_sms_stream([])


def test_decode_csbk_only_raises():
    bursts = encode_sms(1, 2, "x", True)
    with pytest.raises(SMSCodecError):
        decode_sms_stream(bursts[:1])   # just the CSBK block
