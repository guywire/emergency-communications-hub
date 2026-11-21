"""M17 reflector directory: host-file parsing, merge order, saved-list sources, phone picker pages."""
import xml.etree.ElementTree as ET

from ech.core import m17_directory as md
from ech.core import yealink_xml as yx

HOSTS = """# M17 hosts
M17-177   k2hzem17.duckdns.org   17000
M17-QQQ\tm17.openquad.net
M17-BAD   bad..host 17000
M17-XYZ   ref.example.org notaport
M17-SFO   sfo.example.net 17001
"""


def test_parse_hosts_skips_bad_lines_and_defaults_port():
    rows = md.parse_hosts(HOSTS)
    assert [r["name"] for r in rows] == ["M17-177", "M17-QQQ", "M17-SFO"]
    assert rows[1]["port"] == 17000 and rows[2]["port"] == 17001


def test_merge_puts_saved_first_and_dedupes(tmp_path):
    d = md.M17Directory(tmp_path / "c.json")
    d.entries = md.parse_hosts(HOSTS)
    saved = md.saved_from_config({
        "m17_reflectors": [{"name": "Home", "host": "M17.OpenQuad.net", "port": 17000}],
        "adapters": [{"type": "m17_reflector", "name": "m17-177", "reflector_host": "k2hzem17.duckdns.org"},
                     {"type": "irc", "name": "x"}]})
    rows = d.merged(saved)
    assert [(r["host"], r["source"]) for r in rows] == [
        ("m17.openquad.net", "saved"), ("k2hzem17.duckdns.org", "saved"), ("sfo.example.net", "directory")]


def test_cache_survives_restart(tmp_path):
    p = tmp_path / "c.json"
    p.write_text('{"fetched_at": 123, "entries": [{"name": "M17-A", "host": "a.example", "port": 17000}]}')
    assert md.M17Directory(p).entries[0]["host"] == "a.example"


def test_valid_host():
    assert md.valid_host("m17.openquad.net") and md.valid_host("10.1.2.3")
    assert not md.valid_host("") and not md.valid_host("a..b") and not md.valid_host("x/../y") and not md.valid_host("h&m=1")


def test_phone_reflector_picker_pages_and_module_step():
    base = "http://10.0.0.44:8766/yealink/K"
    rows = [{"name": f"M17-{i:03d}", "host": f"r{i}.example", "port": 17000, "source": "directory"} for i in range(45)]
    p1 = ET.fromstring(yx.m17_reflectors(base, "m17", rows, "r3.example", 1))
    items = [(i.findtext("Prompt"), i.findtext("URI")) for i in p1.findall("MenuItem")]
    assert len(items) == yx.DIR_PAGE + 1 and items[-1][1].endswith("page=2")
    assert items[3] == ("M17-003 (current)", f"{base}/m17/module.xml?a=m17&h=r3.example&p=17000")
    mods = ET.fromstring(yx.m17_modules(base, "m17", "", "r9.example", 17001))
    assert mods.find("MenuItem/URI").text == f"{base}/m17/server.xml?a=m17&h=r9.example&p=17001&m=A"
