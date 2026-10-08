"""
tests/test_aredn_service_discovery.py
----------------------------------------
Covers the AREDN mesh PBX/phone service-discovery feature (O101/queue):
querying a local AREDN node's sysinfo.json for services advertised
mesh-wide via OLSR, filtering to PBX/phone-like ones, and converting the
one structured case (sip: URIs) into a dialable destination. The fixture
data below is a trimmed-down but VERBATIM subset of real entries pulled
live from the operator's own AREDN supernode on 2026-10-08 (confirmed
schema, not guessed) — see ech/core/aredn_service_discovery.py's module
docstring for the full research note.
"""

import httpx
import pytest

from ech.core.aredn_service_discovery import (
    fetch_services, looks_pbx_like, to_directory_entries,
)

# Real entries (trimmed) from a live sysinfo.json?services=1 response.
_REAL_SERVICES_SAMPLE = [
    {"name": "KJ5DFD PBX [phone]", "ip": "10.87.121.109",
     "link": "sip://KJ5DFD-home:5060/", "protocol": "tcp"},
    {"name": "Allstar Node 529990 ( ALLMON ) [radio]", "ip": "10.23.108.122",
     "link": "http://RX-TX-4C:80/supermon/link.php?nodes=529990", "protocol": "tcp"},
    {"name": "Dial by IP: 10.87.121.109 [phone]", "ip": "10.87.121.109",
     "link": "", "protocol": "tcp"},
    {"name": "VOIP", "ip": "10.113.117.117",
     "link": "http://VE7LSE-AREDN-PBX:80/", "protocol": "tcp"},
    {"name": "meshcore-web-ui", "ip": "10.7.21.187",
     "link": "http://meshcore-web-ui:80/", "protocol": "tcp"},
    {"name": "US1-North [camera]", "ip": "10.12.244.102",
     "link": "http://psj-northbound:80/snap.jpeg", "protocol": "tcp"},
]


def test_looks_pbx_like_matches_phone_tagged_and_sip_entries():
    assert looks_pbx_like(_REAL_SERVICES_SAMPLE[0]) is True   # [phone] + sip:
    assert looks_pbx_like(_REAL_SERVICES_SAMPLE[1]) is True   # "Allstar"
    assert looks_pbx_like(_REAL_SERVICES_SAMPLE[2]) is True   # "Dial by IP"
    assert looks_pbx_like(_REAL_SERVICES_SAMPLE[3]) is True   # "PBX" in link


def test_looks_pbx_like_rejects_unrelated_services():
    assert looks_pbx_like(_REAL_SERVICES_SAMPLE[4]) is False  # meshcore-web-ui
    assert looks_pbx_like(_REAL_SERVICES_SAMPLE[5]) is False  # camera


def test_to_directory_entries_extracts_sip_destination():
    entries = to_directory_entries({"services": _REAL_SERVICES_SAMPLE, "services_local": []})
    by_name = {e["name"]: e for e in entries}
    assert "KJ5DFD PBX [phone]" in by_name
    assert by_name["KJ5DFD PBX [phone]"]["destination"] == "SIP/KJ5DFD-home:5060"
    assert by_name["KJ5DFD PBX [phone]"]["category"] == "aredn_pbx"


def test_to_directory_entries_leaves_unstructured_entries_undialable():
    """'Dial by IP: ...' is free text, not a structured sip: URI — must NOT
    be guessed into a destination, per the module's honesty stance."""
    entries = to_directory_entries({"services": _REAL_SERVICES_SAMPLE, "services_local": []})
    by_name = {e["name"]: e for e in entries}
    assert by_name["Dial by IP: 10.87.121.109 [phone]"]["destination"] == ""
    assert by_name["VOIP"]["destination"] == ""  # http:// link, not sip:


def test_to_directory_entries_excludes_non_pbx_services():
    entries = to_directory_entries({"services": _REAL_SERVICES_SAMPLE, "services_local": []})
    names = {e["name"] for e in entries}
    assert "meshcore-web-ui" not in names
    assert "US1-North [camera]" not in names


def test_to_directory_entries_merges_services_local():
    local_only = [{"name": "Shack PBX [phone]", "ip": "10.1.1.1",
                   "link": "sip://shack:5060/", "protocol": "tcp"}]
    entries = to_directory_entries({"services": [], "services_local": local_only})
    assert len(entries) == 1
    assert entries[0]["destination"] == "SIP/shack:5060"


def test_sip_destination_without_explicit_port():
    svc = [{"name": "No-port PBX [phone]", "ip": "10.1.1.2", "link": "sip://node/", "protocol": "tcp"}]
    entries = to_directory_entries({"services": svc, "services_local": []})
    assert entries[0]["destination"] == "SIP/node"


def test_sip_destination_with_user_part():
    """Real mesh links seen so far omit the user@ part, but the URI scheme
    technically allows one — handle it if present."""
    svc = [{"name": "User PBX [phone]", "ip": "10.1.1.3", "link": "sip://user@node:5061/", "protocol": "tcp"}]
    entries = to_directory_entries({"services": svc, "services_local": []})
    assert entries[0]["destination"] == "SIP/user@node:5061"


def _mock_client_factory(handler):
    class _MockAsyncClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)
    return _MockAsyncClient


@pytest.mark.asyncio
async def test_fetch_services_follows_redirect_and_passes_query_params(monkeypatch):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.path == "/cgi-bin/sysinfo.json":
            return httpx.Response(307, headers={"Location": "/a/sysinfo?services=1&services_local=1"})
        return httpx.Response(200, json={"node": "TEST-NODE", "services": [], "services_local": []})

    import ech.core.aredn_service_discovery as mod
    monkeypatch.setattr(mod.httpx, "AsyncClient", _mock_client_factory(handler))

    result = await fetch_services("http://192.168.1.1/cgi-bin/sysinfo.json")
    assert result["node"] == "TEST-NODE"
    assert any("services=1" in c for c in calls)


@pytest.mark.asyncio
async def test_fetch_services_raises_on_http_error(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    import ech.core.aredn_service_discovery as mod
    monkeypatch.setattr(mod.httpx, "AsyncClient", _mock_client_factory(handler))

    with pytest.raises(httpx.HTTPStatusError):
        await fetch_services("http://192.168.1.1/cgi-bin/sysinfo.json")
