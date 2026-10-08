"""
tests/test_meshcore_hub_compare.py
-------------------------------------
Covers O96: comparing a meshcore-hub instance's observed messages against
ECH's own log to find reception gaps. No live meshcore-hub instance was
available this session (see module docstring in meshcore_hub_compare.py) —
these tests exercise the defensive multi-schema field parsing and the gap
heuristic against a few plausible response shapes, using a mocked HTTP
transport rather than a real hub.
"""

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from ech.core.meshcore_hub_compare import MeshCoreHubCompare, _extract_message


# ── field extraction (schema tolerance) ─────────────────────────────────

def test_extract_message_snake_case_schema():
    rec = {"timestamp": "2026-10-08T12:00:00Z", "text": "hello mesh", "channel": 2, "observer": "KN0O"}
    out = _extract_message(rec)
    assert out["body"] == "hello mesh"
    assert out["channel"] == 2
    assert out["observer"] == "KN0O"
    assert out["timestamp"].tzinfo is not None


def test_extract_message_alternate_field_names():
    rec = {"received_at": "2026-10-08T12:00:00+00:00", "body": "other names", "gateway": "W1ABC"}
    out = _extract_message(rec)
    assert out["body"] == "other names"
    assert out["observer"] == "W1ABC"


def test_extract_message_unix_timestamp():
    rec = {"ts": 1759924800, "message": "epoch time variant"}
    out = _extract_message(rec)
    assert out is not None
    assert out["body"] == "epoch time variant"


def test_extract_message_missing_required_fields_returns_none():
    assert _extract_message({"channel": 2}) is None          # no timestamp or body
    assert _extract_message({"timestamp": "bad-date", "text": "x"}) is None


# ── fetch_hub_messages / find_gaps ──────────────────────────────────────

class _FakeDB:
    def __init__(self, messages):
        self._messages = messages

    async def get_messages(self, limit=100, offset=0, adapter=None, since=None,
                            priority_min=None, from_id=None, per_adapter_limit=None):
        return self._messages


def _compare_with_mock(handler, db=None):
    c = MeshCoreHubCompare({"meshcore_hub": {"enabled": True, "base_url": "http://hub.local"}}, db=db)
    c._client = httpx.AsyncClient(base_url="http://hub.local", transport=httpx.MockTransport(handler))
    return c


@pytest.mark.asyncio
async def test_fetch_hub_messages_parses_list_response():
    def handler(request):
        return httpx.Response(200, json=[
            {"timestamp": "2026-10-08T12:00:00Z", "text": "msg one", "channel": 0},
            {"timestamp": "2026-10-08T12:05:00Z", "text": "msg two", "channel": 0},
        ])
    c = _compare_with_mock(handler)
    msgs = await c.fetch_hub_messages(hours=24)
    assert len(msgs) == 2
    assert {m["body"] for m in msgs} == {"msg one", "msg two"}


@pytest.mark.asyncio
async def test_fetch_hub_messages_parses_wrapped_response():
    def handler(request):
        return httpx.Response(200, json={"messages": [
            {"timestamp": "2026-10-08T12:00:00Z", "text": "wrapped"},
        ]})
    c = _compare_with_mock(handler)
    msgs = await c.fetch_hub_messages(hours=24)
    assert len(msgs) == 1
    assert msgs[0]["body"] == "wrapped"


@pytest.mark.asyncio
async def test_fetch_hub_messages_skips_unparseable_records():
    def handler(request):
        return httpx.Response(200, json=[
            {"timestamp": "2026-10-08T12:00:00Z", "text": "good one"},
            {"nonsense_field": "garbage"},
        ])
    c = _compare_with_mock(handler)
    msgs = await c.fetch_hub_messages(hours=24)
    assert len(msgs) == 1
    assert msgs[0]["body"] == "good one"


@pytest.mark.asyncio
async def test_find_gaps_flags_messages_not_in_own_log():
    now = datetime.now(timezone.utc)

    def handler(request):
        return httpx.Response(200, json=[
            {"timestamp": now.isoformat(), "text": "we heard this too", "channel": 0},
            {"timestamp": now.isoformat(), "text": "only the hub heard this", "channel": 0},
        ])
    own = [{"body": "we heard this too", "timestamp": now.isoformat()}]
    db = _FakeDB(own)
    c = _compare_with_mock(handler, db=db)
    gaps = await c.find_gaps(hours=24)
    assert len(gaps) == 1
    assert gaps[0]["body"] == "only the hub heard this"


@pytest.mark.asyncio
async def test_find_gaps_respects_time_window_not_just_body_match():
    """A matching body far outside the 5-minute correlation window should
    NOT count as 'we heard this too' — different transmission, not a match."""
    now = datetime.now(timezone.utc)
    far_away = now - timedelta(hours=3)

    def handler(request):
        return httpx.Response(200, json=[
            {"timestamp": now.isoformat(), "text": "same text, different time"},
        ])
    own = [{"body": "same text, different time", "timestamp": far_away.isoformat()}]
    db = _FakeDB(own)
    c = _compare_with_mock(handler, db=db)
    gaps = await c.find_gaps(hours=24)
    assert len(gaps) == 1


@pytest.mark.asyncio
async def test_disabled_service_returns_empty():
    c = MeshCoreHubCompare({}, db=_FakeDB([]))
    assert c.enabled is False
    assert await c.fetch_hub_messages() == []
    assert await c.find_gaps() == []
