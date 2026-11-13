"""
tests/test_directory_status.py
------------------------------
Live directory refresh (ech/core/directory_status.py): HOIP wiki table
parsing, AllStarLink registered-node list parsing, which AllStar node sits
behind an entry, and the up/down/unknown status get_directory() reports.
The HTML below is a minimal stand-in with the same table shape as HOIP's
pages, not a copy of them.
"""

import asyncio
import time

import pytest

from ech.core import directory_status as ds
from ech.core.radio_directory import get_directory

RF_HTML = """<html><body><table>
<thead><tr><th>Extension</th><th>Name</th><th>Description</th><th>Owner Contact</th></tr></thead>
<tbody>
<tr><td>15000</td><td>N8EMA ALLSTAR 49050</td><td></td><td>Someone <a href="mailto:x@example.com">x@example.com</a></td></tr>
<tr><td>15001</td><td>EAST COAST REFLECTOR</td><td></td><td></td></tr>
<tr><td>15053</td><td>KE8SEW WELCOME 500 572061</td><td>Welcome &amp; hub</td><td></td></tr>
<tr><td>25002</td><td>M7CWN ALLSTAR 56646</td><td></td><td></td></tr>
<tr><td>35005</td><td>4F0X ALLSTAR 41960</td><td></td><td></td></tr>
</tbody></table></body></html>"""

CONF_HTML = """<table><tr><th>Extension</th><th>Name</th><th>Description</th><th>Type</th><th>Owner Contact</th></tr>
<tr><td>10001</td><td>PUBLIC 1</td><td></td><td>Open</td><td>a@b.c</td></tr>
<tr><td>20001</td><td>PUBLIC EU 1</td><td></td><td></td><td></td></tr></table>"""

ASL_TEXT = "[extnodes]\n\n49050=radio@1.2.3.4:4569/49050,1.2.3.4\n2002=radio@5.6.7.8:4569/2002,5.6.7.8\n"


@pytest.fixture
def live(monkeypatch, tmp_path):
    s = ds.DirectoryStatus()
    # MeshPhone fetching has its own tests (test_meshphone_directory.py)
    s.configure({"database": {"path": str(tmp_path / "ech.db")},
                 "radio_directory": {"meshphone": {"enabled": False}}})
    monkeypatch.setattr(ds, "status", s)
    return s


def test_parse_rf_links_categories_and_no_contact_data():
    rows = ds.parse_hoip_page("rf-links-list", RF_HTML)
    assert [r["destination"] for r in rows] == ["15000", "15001", "15053", "25002", "35005"]
    cats = {r["destination"]: r["category"] for r in rows}
    assert cats["15000"] == "hoip_rf_link_us"
    assert cats["25002"] == "hoip_rf_link_eu"
    assert cats["35005"] == "hoip_rf_link_ap"
    assert rows[2]["description"] == "Welcome & hub"
    # the owner-contact column never makes it into a stored entry
    assert not any("example.com" in str(v) for r in rows for v in r.values())


def test_parse_conference_regions():
    rows = ds.parse_hoip_page("conference-bridge-list", CONF_HTML)
    assert [(r["destination"], r["category"]) for r in rows] == [
        ("10001", "hoip_conference_us"), ("20001", "hoip_conference_eu")]


def test_parse_asl_nodes():
    assert ds.parse_asl_nodes(ASL_TEXT) == {"49050", "2002"}


@pytest.mark.parametrize("entry,node", [
    ({"category": "hoip_rf_link_us", "name": "N8EMA ALLSTAR 49050"}, "49050"),
    ({"category": "hoip_rf_link_us", "name": "KE8SEW WELCOME 500 572061"}, "572061"),
    ({"category": "hoip_rf_link_us", "name": "EAST COAST REFLECTOR"}, None),
    ({"category": "hoip_conference_us", "name": "REGION 1 HAMS NET 10029"}, None),
    ({"category": "test", "destination": "55553"}, "55553"),
    ({"category": "group", "destination": "999", "allstar_node": 12345}, "12345"),
])
def test_allstar_node_for(entry, node):
    assert ds.allstar_node_for(entry) == node


def test_status_unknown_until_asl_fetched(live):
    assert live.annotate({"category": "test", "destination": "2002"})["asl_status"] == "unknown"


def test_status_up_down_unknown(live):
    live.asl_registered = {"49050", "2002"}
    live.asl_refreshed_at = 1.0
    assert live.annotate({"category": "hoip_rf_link_us", "name": "N8EMA ALLSTAR 49050"})["asl_status"] == "up"
    assert live.annotate({"category": "hoip_rf_link_us", "name": "M7DPD ALLSTAR 57206"})["asl_status"] == "down"
    assert live.annotate({"category": "hoip_rf_link_us", "name": "EAST COAST REFLECTOR"})["asl_status"] == "unknown"
    assert live.annotate({"category": "hoip_conference_us", "name": "PUBLIC 1"})["asl_status"] == "unknown"
    # trailing number without ALLSTAR in the name: may be up, never down
    assert live.annotate({"category": "hoip_rf_link_eu", "name": "M0UKB EXTENDED FREEDOM NETWORK 23525"})["asl_status"] == "unknown"
    live.asl_registered.add("23525")
    assert live.annotate({"category": "hoip_rf_link_eu", "name": "M0UKB EXTENDED FREEDOM NETWORK 23525"})["asl_status"] == "up"


def test_get_directory_uses_refreshed_hoip_list(live):
    live.hoip_entries = ds.parse_hoip_page("rf-links-list", RF_HTML)
    live.asl_registered = {"49050"}
    live.asl_refreshed_at = 1.0
    d = get_directory({})
    dests = [e["destination"] for e in d["hamvoip"]]
    assert "15000" in dests and "10001" not in dests   # refreshed list replaces the built-in HOIP copy
    by = {e["destination"]: e for e in d["hamvoip"]}
    assert by["15000"]["asl_status"] == "up"
    assert by["25002"]["asl_status"] == "down"
    assert by["15053"]["asl_status"] == "unknown"   # "KE8SEW WELCOME 500 572061" doesn't say ALLSTAR
    assert d["status"]["asl_refreshed_at"] == 1.0


class _Resp:
    def __init__(self, text, code=200):
        self.text, self.status_code = text, code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _fake_client(pages, asl_text):
    class C:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            if url == ds.ASL_NODES_URL:
                return _Resp(asl_text)
            page = url.rstrip("/").rsplit("/", 1)[-1]
            return _Resp(pages[page]) if page in pages else _Resp("", 404)
    return C


def _big_asl():
    lines = "".join(f"{n}=radio@1.1.1.1:4569/{n},1.1.1.1\n" for n in range(40000, 41500))
    return "[extnodes]\n" + lines + "49050=radio@1.2.3.4:4569/49050,1.2.3.4\n"


_PAGES_OK = {
    "test-numbers": "<table><tr><td>3194</td><td>Echo Test</td></tr></table>",
    "conference-bridge-list": CONF_HTML,
    "rf-links-list": RF_HTML,
    "audio-feeds-list": "<table><tr><td>90002</td><td>NEWSLINE CENTER</td></tr></table>",
}


def test_refresh_success_and_cache_roundtrip(live, monkeypatch, tmp_path):
    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _fake_client(_PAGES_OK, _big_asl()))
    st = asyncio.run(live.refresh())
    assert st["last_error"] is None
    assert st["hoip_entries"] == 1 + 2 + 5 + 1
    assert live.hoip_entries[0]["category"] == "hoip_test"
    # the cache file keeps only node numbers the directory references
    again = ds.DirectoryStatus()
    again.configure({"database": {"path": str(tmp_path / "ech.db")}})
    assert "49050" in again.asl_registered and "40000" not in again.asl_registered
    assert len(again.hoip_entries) == 9


def test_refresh_failure_keeps_previous_data(live, monkeypatch):
    import httpx
    live.hoip_entries = [{"name": "old", "destination": "15000", "category": "hoip_rf_link_us"}]
    live.asl_registered, live.asl_refreshed_at = {"49050"}, 5.0
    pages = dict(_PAGES_OK)
    del pages["audio-feeds-list"]   # one HOIP page 404s
    # ...and the AllStarLink list comes back implausibly short
    monkeypatch.setattr(httpx, "AsyncClient", _fake_client(pages, ASL_TEXT))
    st = asyncio.run(live.refresh())
    assert "audio-feeds-list" in st["last_error"] and "not trusting it" in st["last_error"]
    assert live.hoip_entries[0]["name"] == "old"
    assert live.asl_registered == {"49050"} and live.asl_refreshed_at == 5.0


def test_manual_refresh_gap(live):
    assert live.can_refresh_now() == (True, 0)
    live.last_attempt = time.time()
    ok, wait = live.can_refresh_now()
    assert not ok and 0 < wait <= ds.MANUAL_MIN_GAP_S


def test_interval_floor(live):
    live.configure({"radio_directory": {"live_refresh": {"interval_min": 1}}})
    assert live.interval_s == ds.MIN_INTERVAL_S
