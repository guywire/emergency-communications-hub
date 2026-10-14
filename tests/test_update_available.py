"""Nav "update available" notification: version comparison + cached GitHub check."""
import asyncio

import pytest

from ech.core import self_update as su


def test_parse_version():
    assert su.parse_version("1.0.0-rc279") == (1, 0, 0, 279)
    assert su.parse_version(" v1.2.3 \n")[:3] == (1, 2, 3)
    assert su.parse_version("garbage") is None
    assert su.parse_version("") is None


@pytest.mark.parametrize("latest,current,expected", [
    ("1.0.0-rc280", "1.0.0-rc279", True),
    ("1.0.0-rc279", "1.0.0-rc279", False),
    ("1.0.0-rc278", "1.0.0-rc279", False),   # deployed ahead of GitHub: no nag
    ("1.0.0-rc1000", "1.0.0-rc999", True),   # numeric, not string, comparison
    ("1.0.0", "1.0.0-rc279", True),          # final release beats its RCs
    ("1.0.1-rc1", "1.0.0", True),
    ("not-a-version", "1.0.0-rc279", False),
    ("1.0.0-rc280", "dev", False),
])
def test_is_newer(latest, current, expected):
    assert su.is_newer(latest, current) is expected


class _Resp:
    def __init__(self, text, status=200):
        self.text, self.status_code = text, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _fake_client(calls, text="1.0.0-rc300\n", status=200):
    class C:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, **k):
            calls.append(url)
            return _Resp(text, status)
    return C


@pytest.fixture(autouse=True)
def _reset_cache():
    su._avail_cache.update(ts=0.0, ttl=0.0, latest="", error="")
    yield


def test_update_available_true_and_cached(monkeypatch):
    calls = []
    monkeypatch.setattr(su.httpx, "AsyncClient", _fake_client(calls))
    r1 = asyncio.run(su.update_available("1.0.0-rc299"))
    r2 = asyncio.run(su.update_available("1.0.0-rc299"))
    assert r1["available"] is True and r1["latest"] == "1.0.0-rc300" and r1["error"] == ""
    assert r2["available"] is True
    assert len(calls) == 1                      # second call served from cache


def test_up_to_date_is_not_available(monkeypatch):
    monkeypatch.setattr(su.httpx, "AsyncClient", _fake_client([], text="1.0.0-rc299"))
    assert asyncio.run(su.update_available("1.0.0-rc299"))["available"] is False


def test_github_error_is_reported_not_raised_and_retried_sooner(monkeypatch):
    monkeypatch.setattr(su.httpx, "AsyncClient", _fake_client([], status=503))
    r = asyncio.run(su.update_available("1.0.0-rc299"))
    assert r["available"] is False and r["error"]
    assert su._avail_cache["ttl"] == su.UPDATE_CHECK_RETRY < su.UPDATE_CHECK_TTL


def test_force_bypasses_cache(monkeypatch):
    calls = []
    monkeypatch.setattr(su.httpx, "AsyncClient", _fake_client(calls))
    asyncio.run(su.update_available("1.0.0-rc299"))
    asyncio.run(su.update_available("1.0.0-rc299", force=True))
    assert len(calls) == 2
