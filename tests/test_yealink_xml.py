"""Yealink XML Browser pages render as well-formed XML the phone can parse."""
import xml.etree.ElementTree as ET

from ech.core import yealink_xml as yx

BASE = "http://10.0.0.44:8766/yealink/KEY"


def _parse(doc):
    return ET.fromstring(doc.encode())


def test_main_menu_links_every_section():
    root = _parse(yx.main_menu(BASE))
    assert root.tag == "YealinkIPPhoneTextMenu"
    uris = [m.find("URI").text for m in root.findall("MenuItem")]
    assert f"{BASE}/winlink.xml" in uris and f"{BASE}/mesh.xml" in uris


def test_winlink_menu_marks_unread_and_escapes():
    inbox = {"callsign": "N0CALL", "count": 2, "unread": 1, "messages": [
        {"mid": "ABC123", "subject": "Net <tonight> & more", "from": "W1AW", "date": "2026-11-15T14:14:00-05:00", "unread": "True"},
        {"mid": "DEF456", "subject": "Old", "from": "K1ABC", "date": "2026-11-01T10:00:00-05:00", "unread": "False"}]}
    root = _parse(yx.winlink_menu(BASE, inbox))
    prompts = [m.find("Prompt").text for m in root.findall("MenuItem")]
    assert prompts[0].startswith("* ") and "Net <tonight> & more" in prompts[0]
    assert not prompts[1].startswith("* ")
    assert root.findall("MenuItem")[0].find("URI").text == f"{BASE}/winlink/ABC123.xml"


def test_message_screen_truncates_long_bodies():
    root = _parse(yx.winlink_message({"subject": "S", "from": "W1AW", "to": ["A"] * 10, "body": "x" * 100}))
    text = root.find("Text").text
    assert "+4 more" in text


def test_mesh_screen_skips_aprs_and_irc():
    msgs = [{"msg_type": "text", "source_adapter": "aprs-internet", "body": "beacon"},
            {"msg_type": "text", "source_adapter": "irc-libera", "body": "chat"},
            {"msg_type": "text", "source_adapter": "meshcore-usb", "source_channel": "ch2:#bot", "from_display": "Node1",
             "body": "hello mesh", "timestamp": "2026-11-17T20:00:00+00:00"}]
    text = _parse(yx.mesh_screen(msgs)).find("Text").text
    assert "hello mesh" in text and "beacon" not in text and "chat" not in text


def test_empty_states():
    assert "No active NWS alerts" in _parse(yx.alerts_screen([])).find("Text").text
    assert _parse(yx.winlink_menu(BASE, {"messages": []})).tag == "YealinkIPPhoneTextScreen"


def test_long_message_becomes_menu_of_small_pages():
    msg = {"subject": "Weekly … net", "from": "W1AW", "to": ["A"], "body": ("word " * 300 + "\n\n") * 3}
    root = _parse(yx.winlink_message(msg, BASE + "/winlink/ABC.xml"))
    assert root.tag == "YealinkIPPhoneTextMenu"
    items = root.findall("MenuItem")
    assert len(items) >= 3 and items[0].find("URI").text.endswith("?page=1")
    assert items[0].find("Prompt").text.startswith("Part 1 of")
    for i in range(1, len(items) + 1):
        page = _parse(yx.winlink_message(msg, BASE + "/winlink/ABC.xml", i))
        text = page.find("Text").text
        assert len(text) <= yx.PAGE_CHARS and text.isascii()
    assert "From: W1AW" in _parse(yx.winlink_message(msg, BASE + "/winlink/ABC.xml", 1)).find("Text").text


def test_short_message_is_a_single_screen():
    root = _parse(yx.winlink_message({"subject": "Hi", "from": "W1AW", "body": "short"}, BASE + "/winlink/A.xml"))
    assert root.tag == "YealinkIPPhoneTextScreen"


def test_main_menu_has_bot_pages_directory_and_aprs():
    uris = [m.find("URI").text for m in _parse(yx.main_menu(BASE)).findall("MenuItem")]
    for k in yx.BOT_PAGES:
        assert f"{BASE}/bot/{k}.xml" in uris
    assert f"{BASE}/dir.xml" in uris and f"{BASE}/aprs.xml" in uris


def test_ascii_drops_emoji_and_accents():
    assert yx._ascii("45°F ☀️ café → N") == "45F cafe -> N"


def test_paged_screen_short_and_long():
    assert _parse(yx.paged_screen("Wx", "Sunny", f"{BASE}/bot/weather.xml")).tag == "YealinkIPPhoneTextScreen"
    long = ("Tonight: clear skies with light winds. " * 60).strip()
    menu = _parse(yx.paged_screen("Wx", long, f"{BASE}/bot/aprs.xml?call=W1AW"))
    assert menu.tag == "YealinkIPPhoneTextMenu"
    assert menu.find("MenuItem/URI").text.endswith("?call=W1AW&page=1")


def test_directory_pages_are_text_menus_that_dial():
    entries = [(f"Person {i}", str(100 + i)) for i in range(45)]
    url, dial = f"{BASE}/dir/ech.xml", f"{BASE}/dial.xml"
    p1 = _parse(yx.directory_page("ECH", entries, url, dial, 1))
    assert p1.tag == "YealinkIPPhoneTextMenu"
    uris = [m.find("URI").text for m in p1.findall("MenuItem")]
    assert len(uris) == yx.DIR_PAGE + 1 and uris[0] == f"{dial}?n=100" and uris[-1] == f"{url}?page=2"
    p3 = [m.find("URI").text for m in _parse(yx.directory_page("ECH", entries, url, dial, 3)).findall("MenuItem")]
    assert p3[0] == f"{url}?page=2" and len(p3) == 6


def test_directory_letters_and_filter():
    entries = [("Alice", "1"), ("[OFFICE] Bob", "2"), ("alan", "3"), ("0-WWV", "4")]
    url, dial = f"{BASE}/dir/mesh.xml", f"{BASE}/dial.xml"
    prompts = [m.find("Prompt").text for m in _parse(yx.directory_letters("Mesh", entries, url)).findall("MenuItem")]
    assert prompts == ["# (1)", "A (2)", "O (1)"]
    a = [m.find("URI").text for m in _parse(yx.directory_page("Mesh", entries, url, dial, 1, "A")).findall("MenuItem")]
    assert a == [f"{dial}?n=1", f"{dial}?n=3"]


def test_dial_execute():
    r = _parse(yx.dial_execute("101"))
    assert r.tag == "YealinkIPPhoneExecute" and r.find("ExecuteItem").get("URI") == "Dial:101"


def test_aprs_menu_dedupes_newest_first():
    pk = [{"ts": 1, "from": "W1AW-9", "summary": "pos"}, {"ts": 2, "from": "KN0O", "summary": "wx"},
          {"ts": 3, "from": "W1AW-9", "summary": "pos2"}, {"ts": 4, "from": "bad<call>", "summary": ""}]
    uris = [m.find("URI").text for m in _parse(yx.aprs_menu(BASE, pk)).findall("MenuItem")]
    assert uris == [f"{BASE}/bot/aprs.xml?call=W1AW-9", f"{BASE}/bot/aprs.xml?call=KN0O"]


def test_forecast_menu_and_period():
    periods = [{"name": "Tonight", "temperature": 38, "temperatureUnit": "F", "shortForecast": "Partly Cloudy",
                "windSpeed": "5 mph", "windDirection": "SW", "detailedForecast": "Partly cloudy, with a low around 38."}]
    menu = _parse(yx.forecast_menu(BASE, periods))
    assert menu.find("MenuItem/Prompt").text == "Tonight: 38F Partly Cloudy"
    assert menu.find("MenuItem/URI").text == f"{BASE}/forecast.xml?period=1"
    scr = _parse(yx.forecast_period(periods[0]))
    assert "low around 38" in scr.findtext("Text") and "Wind SW 5 mph" in scr.findtext("Text")


def test_allstar_and_m17_panels():
    presets = [{"node": "620860", "name": "W1PBR hub"}, {"node": "620861", "name": "W1PBR 147.060"}]
    m = _parse(yx.allstar_menu(BASE, "63018", ["R620860", "T521771"], presets))
    prompts = [i.findtext("Prompt") for i in m.findall("MenuItem")]
    uris = [i.findtext("URI") for i in m.findall("MenuItem")]
    assert prompts[0] == "Linked to: 620860, 521771"
    assert f"{BASE}/dial.xml?n=751" in uris and f"{BASE}/allstar/link.xml?node=620861" in uris
    assert uris[-1] == f"{BASE}/allstar/unlink.xml"
    inp = _parse(yx.input_screen("Link node", f"{BASE}/allstar/link.xml", "node", "Node:"))
    assert inp.tag == "YealinkIPPhoneInputScreen" and inp.findtext("InputField/Parameter") == "node"
    m17 = _parse(yx.m17_menu(BASE, [{"name": "m17-ref", "host": "ref.example", "module": "C", "connected": True}]))
    assert f"{BASE}/dial.xml?n=760" not in [i.findtext("URI") for i in m17.findall("MenuItem")]
    mods = _parse(yx.m17_modules(BASE, "m17-ref", "C"))
    assert len(mods.findall("MenuItem")) == 26 and "(current)" in mods.findall("MenuItem")[2].findtext("Prompt")
    main = [i.findtext("URI") for i in _parse(yx.main_menu(BASE)).findall("MenuItem")]
    assert f"{BASE}/allstar.xml" in main and f"{BASE}/m17.xml" in main
