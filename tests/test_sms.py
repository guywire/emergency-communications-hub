"""
tests/test_sms.py
-----------------
SMS adapter tests — AT engine logic, GSM7 encoding, contact import,
PDU builder, priority detection, and mock end-to-end.
No physical modem or SIM required.
"""

import asyncio
import io
import tempfile
from pathlib import Path

import pytest

from ech.adapters.sms import (
    SMSAdapter,
    _assess_priority,
    _gsm7_encode,
    _parse_sms_timestamp,
    import_vcf,
    import_csv,
)
from ech.adapters.mock_sms import MockSMSAdapter
from ech.adapters.at_engine import ATEngine, _is_final
from ech.core.models import NormalizedMessage, Priority


# ── AT engine helpers ─────────────────────────────────────────────────────

def test_is_final_ok():
    assert _is_final("OK")
    assert _is_final("  OK  ")
    assert _is_final("ok")


def test_is_final_error():
    assert _is_final("ERROR")
    assert _is_final("+CME ERROR: 10")
    assert _is_final("+CMS ERROR: 304")
    assert _is_final("NO CARRIER")
    assert _is_final("BUSY")


def test_is_final_intermediate():
    assert not _is_final("+CMGR: 0,,25")
    assert not _is_final("+CMTI: \"SM\",3")
    assert not _is_final("Hello world")
    assert not _is_final("")


# ── GSM 7-bit encoding ────────────────────────────────────────────────────

def test_gsm7_encode_basic():
    result = _gsm7_encode("Hello")
    assert isinstance(result, bytes)
    assert len(result) > 0


def test_gsm7_encode_roundtrip_length():
    """7 characters should pack into 7*7/8 = ~7 bytes (ceil)."""
    result = _gsm7_encode("ABCDEFG")
    assert len(result) == 7   # ceil(7*7/8) = 7 bytes... 49 bits → 7 bytes


def test_gsm7_encode_empty():
    assert _gsm7_encode("") == b""


def test_gsm7_encode_special_chars():
    # Known GSM7 characters should not raise
    text = "Hello World! 0123456789 @£$¥"
    result = _gsm7_encode(text)
    assert isinstance(result, bytes)
    assert len(result) > 0


def test_gsm7_encode_non_gsm_falls_back():
    # Chinese characters aren't in GSM7 — should encode as '?' without raising
    result = _gsm7_encode("你好")
    assert isinstance(result, bytes)


# ── SMS timestamp parsing ─────────────────────────────────────────────────

def test_parse_sms_timestamp_standard():
    ts = _parse_sms_timestamp("24/05/10,14:30:00+00")
    assert ts.year == 2024
    assert ts.month == 5
    assert ts.day == 10
    assert ts.hour == 14


def test_parse_sms_timestamp_bad_input():
    # Should return datetime.now() without raising
    ts = _parse_sms_timestamp("garbage")
    from datetime import datetime
    assert isinstance(ts, datetime)


def test_parse_sms_timestamp_empty():
    ts = _parse_sms_timestamp("")
    from datetime import datetime
    assert isinstance(ts, datetime)


# ── Priority assessment ───────────────────────────────────────────────────

def test_priority_normal():
    assert _assess_priority("Net check-in, all ok") == Priority.NORMAL
    assert _assess_priority("ETA 15 minutes to staging") == Priority.NORMAL


def test_priority_elevated():
    assert _assess_priority("URGENT need cots at shelter B") == Priority.ELEVATED
    assert _assess_priority("IMMEDIATE response required") == Priority.ELEVATED


def test_priority_emergency():
    assert _assess_priority("911 fire at main shelter NOW") == Priority.EMERGENCY
    assert _assess_priority("MAYDAY vessel in distress") == Priority.EMERGENCY
    assert _assess_priority("SOS send help") == Priority.EMERGENCY


# ── PDU builder ───────────────────────────────────────────────────────────

def test_pdu_encode_us_number():
    a = SMSAdapter({"port": "/dev/null"})
    pdu_hex, pdu_len = a._encode_pdu("+12075551234", "Test message")
    assert isinstance(pdu_hex, str)
    assert all(c in "0123456789ABCDEF" for c in pdu_hex)
    assert pdu_len > 0


def test_pdu_encode_local_number():
    a = SMSAdapter({"port": "/dev/null"})
    pdu_hex, pdu_len = a._encode_pdu("2075551234", "Hello")
    assert pdu_len > 0


def test_pdu_encode_starts_with_smsc():
    a = SMSAdapter({"port": "/dev/null"})
    pdu_hex, _ = a._encode_pdu("+12075551234", "Hi")
    # First byte is SMSC length = 00 (use modem default)
    assert pdu_hex.startswith("00")


# ── VCF contact import ────────────────────────────────────────────────────

SAMPLE_VCF = """BEGIN:VCARD
VERSION:3.0
FN:John EOC Director
N:Director;John;;;
TEL;TYPE=CELL:+12075551001
NOTE:APRS callsign W1JOH-9
END:VCARD
BEGIN:VCARD
VERSION:3.0
FN:Sarah CERT Lead
TEL;TYPE=WORK:+12075551002
END:VCARD
BEGIN:VCARD
VERSION:3.0
FN:No Phone Contact
END:VCARD
"""


def test_import_vcf_basic():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".vcf", delete=False) as f:
        f.write(SAMPLE_VCF)
        path = f.name
    try:
        contacts = import_vcf(path)
        assert len(contacts) == 3   # all 3 cards parsed (even no-phone)
        names = [c["display_name"] for c in contacts]
        assert "John EOC Director" in names
        assert "Sarah CERT Lead" in names
    finally:
        Path(path).unlink()


def test_import_vcf_phone_extraction():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".vcf", delete=False) as f:
        f.write(SAMPLE_VCF)
        path = f.name
    try:
        contacts = import_vcf(path)
        john = next(c for c in contacts if c["display_name"] == "John EOC Director")
        assert john["sms_number"] == "+12075551001"
    finally:
        Path(path).unlink()


def test_import_vcf_callsign_from_note():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".vcf", delete=False) as f:
        f.write(SAMPLE_VCF)
        path = f.name
    try:
        contacts = import_vcf(path)
        john = next(c for c in contacts if c["display_name"] == "John EOC Director")
        # W1JOH-9 should be extracted from the NOTE field
        assert john["aprs_callsign"] == "W1JOH-9"
    finally:
        Path(path).unlink()


# ── CSV contact import ────────────────────────────────────────────────────

SAMPLE_CSV = """name,phone,callsign,notes
Mike Shelter Mgr,+12075551003,W1MIK,Shelter coordinator
Lisa DEM Liaison,+12075551004,,DEM office contact
Tom Mobile,+12075551005,N1TOM-9,
"""


def test_import_csv_basic():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
        f.write(SAMPLE_CSV)
        path = f.name
    try:
        contacts = import_csv(path, column_map={"name": "name", "phone": "phone",
                                                 "callsign": "callsign", "notes": "notes"})
        assert len(contacts) == 3
        names = [c["display_name"] for c in contacts]
        assert "Mike Shelter Mgr" in names
        assert "Lisa DEM Liaison" in names
    finally:
        Path(path).unlink()


def test_import_csv_phone_cleaned():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
        f.write(SAMPLE_CSV)
        path = f.name
    try:
        contacts = import_csv(path, column_map={"name": "name", "phone": "phone",
                                                 "callsign": "callsign", "notes": "notes"})
        mike = next(c for c in contacts if c["display_name"] == "Mike Shelter Mgr")
        assert mike["sms_number"] == "+12075551003"
        assert mike["aprs_callsign"] == "W1MIK"
    finally:
        Path(path).unlink()


# ── SMS config ────────────────────────────────────────────────────────────

def test_sms_adapter_requires_port():
    with pytest.raises(KeyError):
        SMSAdapter({})   # port is required


def test_sms_adapter_config_defaults():
    a = SMSAdapter({"port": "/dev/ttyUSB0"})
    assert a._port == "/dev/ttyUSB0"
    assert a._baud == 115200
    assert a._pdu_mode is False
    assert a._delete_on_read is True
    assert a._poll_interval == 60


def test_sms_adapter_pdu_mode():
    a = SMSAdapter({"port": "/dev/ttyUSB0", "pdu_mode": True})
    assert a._pdu_mode is True


def test_sms_registered_in_main():
    from ech.main import build_adapter
    a = build_adapter({"type": "sms", "port": "/dev/null"})
    assert isinstance(a, SMSAdapter)
    b = build_adapter({"type": "mock_sms"})
    assert isinstance(b, MockSMSAdapter)


# ── Mock SMS end-to-end ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_mock_sms_produces_messages():
    adapter = MockSMSAdapter({"name": "sms-test", "interval_sec": 0.05})
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
        assert m.source_adapter == "sms-test"
        assert m.source_channel == "SMS"
        assert m.body
        assert m.from_id.startswith("+")


@pytest.mark.asyncio
async def test_mock_sms_send_requires_to_id():
    adapter = MockSMSAdapter({"name": "sms-send-test"})
    await adapter.connect()
    msg_no_to = NormalizedMessage(
        source_adapter="sms-send-test",
        source_channel="SMS",
        from_id="+12075550000",
        body="Test without to_id",
    )
    result = await adapter.send(msg_no_to)
    await adapter.disconnect()
    assert result is False


@pytest.mark.asyncio
async def test_mock_sms_send_with_to_id():
    adapter = MockSMSAdapter({"name": "sms-send-test2"})
    await adapter.connect()
    msg = NormalizedMessage(
        source_adapter="sms-send-test2",
        source_channel="SMS",
        from_id="local",
        body="Test message",
        to_id="+12075551001",
    )
    result = await adapter.send(msg)
    await adapter.disconnect()
    assert result is True


@pytest.mark.asyncio
async def test_mock_sms_health():
    adapter = MockSMSAdapter({"name": "sms-health-test"})
    await adapter.connect()
    h = await adapter.health()
    await adapter.disconnect()
    assert h.state == "connected"
    assert h.detail.get("operator") == "MockCell"
    assert h.detail.get("mode") == "mock"
