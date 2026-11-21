"""Desk-phone push: what ECH decides to change on a phone, from the phone's own config."""
from ech.core.phone_provision import MAX_LOCAL_CONTACTS, parse_cfg, plan_settings, render_cfg, render_contacts_xml, rsa_encrypt

ECH = {"10.0.0.44", "192.168.1.200"}

EXPORT = """#!version:1.0.0.1
### For security, the following parameters with password haven't been display in this file.
###account.1.password =
account.1.enable = 1
account.1.label = Other PBX
account.1.sip_server.1.address = 10.9.9.9
account.3.enable = 1
account.3.label = ECH101
account.3.sip_server.1.address = 10.0.0.44
account.4.enable = 0
account.4.sip_server.1.address = 192.168.1.200
voice_mail.number.3 = *86
app.hide.icon = Browser/Calendar/Email
"""

OPTS = {"http_port": 8766, "flash_time": 3600, "voicemail_code": "*98", "show_browser": True}


def _books(*filled):
    books = [{"id": str(i), "name": "", "url": ""} for i in range(1, 6)]
    for i, name, url in filled:
        books[i - 1] = {"id": str(i), "name": name, "url": url}
    return books


def test_parse_skips_comments_and_redacted_passwords():
    cfg = parse_cfg(EXPORT)
    assert cfg["account.3.label"] == "ECH101"
    assert not any("password" in k for k in cfg)


def test_plan_targets_only_the_enabled_ech_line():
    want, notes = plan_settings(parse_cfg(EXPORT), _books(), ECH, OPTS)
    assert want["voice_mail.number.3"] == "*98"
    assert "voice_mail.number.1" not in want          # other PBX's line untouched
    assert "voice_mail.number.4" not in want          # disabled line untouched
    assert want["remote_phonebook.data.1.url"] == "http://10.0.0.44:8766/phonebook.xml"
    assert want["remote_phonebook.data.2.url"] == "http://10.0.0.44:8766/phonebook-meshphone.xml"
    assert want["remote_phonebook.data.3.url"] == "http://10.0.0.44:8766/phonebook-streams.xml"
    assert want["remote_phonebook.data.3.name"] == "Audio Streams"
    assert want["features.remote_phonebook.flash_time"] == "3600"
    assert want["app.hide.icon"] == "Calendar/Email"
    assert any("account 3" in n for n in notes)


def test_plan_never_overwrites_someone_elses_phonebook():
    books = _books((1, "Corporate", "https://example.com/dir.xml"),
                   (3, "ECH", "http://10.0.0.44:8766/phonebook.xml"))
    want, _ = plan_settings(parse_cfg(EXPORT), books, ECH, OPTS)
    assert "remote_phonebook.data.1.url" not in want
    assert want["remote_phonebook.data.3.url"].endswith("/phonebook.xml")       # existing ECH slot reused
    assert want["remote_phonebook.data.2.url"].endswith("/phonebook-meshphone.xml")


def test_no_ech_line_means_no_changes():
    cfg = parse_cfg("account.1.enable = 1\naccount.1.sip_server.1.address = 10.9.9.9\n")
    want, notes = plan_settings(cfg, _books(), ECH, OPTS)
    assert want == {} and "no enabled line" in notes[0]


def test_browser_left_alone_when_not_hidden_or_disabled():
    cfg = parse_cfg(EXPORT.replace("app.hide.icon = Browser/Calendar/Email", "app.hide.icon = Email"))
    want, _ = plan_settings(cfg, _books(), ECH, OPTS)
    assert "app.hide.icon" not in want
    want, _ = plan_settings(parse_cfg(EXPORT), _books(), ECH, {**OPTS, "show_browser": False})
    assert "app.hide.icon" not in want


def test_render_cfg_is_importable_format():
    text = render_cfg({"b.key": "2", "a.key": "1"})
    lines = text.splitlines()
    assert lines[0] == "#!version:1.0.0.1"
    assert lines[2:] == ["a.key = 1", "b.key = 2"]


def test_rsa_encrypt_pkcs1_v15_roundtrip():
    # small test key built from known primes
    primes = (1000000007, 998244353, 2147483647, 4294967291)
    n, phi = 1, 1
    for pr in primes:
        n *= pr
        phi *= pr - 1
    e = 65537
    d = pow(e, -1, phi)
    enc = rsa_encrypt("pw", format(n, "x"), format(e, "x"))
    assert enc.startswith("__WUI_ENC__:")
    m = pow(int(enc.split(":", 1)[1], 16), d, n)
    em = m.to_bytes((n.bit_length() + 7) // 8, "big")
    assert em[:2] == b"\x00\x02"
    assert em[em.index(b"\x00", 2) + 1:] == b"pw"


def test_menu_key_takes_first_free_line_key():
    cfg = parse_cfg(EXPORT + "linekey.1.type = 15\nlinekey.2.type = 15\nlinekey.3.type = 81\n")
    want, notes = plan_settings(cfg, _books(), ECH, {**OPTS, "xml_key": "k123"})
    assert want["linekey.4.type"] == "27"
    assert want["linekey.4.value"] == "http://10.0.0.44:8766/yealink/k123/menu.xml"
    assert want["linekey.4.label"] == "ECH"
    assert any("line key 4" in n for n in notes)


def test_menu_key_reuses_its_own_key():
    cfg = parse_cfg(EXPORT + "linekey.1.type = 15\nlinekey.6.type = 27\n"
                    "linekey.6.value = http://10.0.0.44:8766/yealink/old/menu.xml\n")
    want, _ = plan_settings(cfg, _books(), ECH, {**OPTS, "xml_key": "new"})
    assert want["linekey.6.value"].endswith("/yealink/new/menu.xml")
    assert want["linekey.2.value"].endswith("/allstar.xml") and want["linekey.2.label"] == "AllStar"
    assert want["linekey.3.value"].endswith("/m17.xml") and want["linekey.3.label"] == "M17"


def test_panel_keys_keep_their_slots_and_can_be_turned_off():
    cfg = parse_cfg(EXPORT + "\n".join([
        "linekey.1.type = 15",
        "linekey.3.type = 27", "linekey.3.value = http://10.0.0.44:8766/yealink/k/menu.xml",
        "linekey.5.type = 27", "linekey.5.value = http://10.0.0.44:8766/yealink/k/m17.xml",
        "linekey.8.type = 27", "linekey.8.value = http://10.0.0.44:8766/yealink/k/allstar.xml", ""]))
    want, notes = plan_settings(cfg, _books(), ECH, {**OPTS, "xml_key": "k"})
    assert want["linekey.3.label"] == "ECH" and want["linekey.8.label"] == "AllStar" and want["linekey.5.label"] == "M17"
    assert any("AllStar panel on line key 8" in n for n in notes)
    want, _ = plan_settings(cfg, _books(), ECH, {**OPTS, "xml_key": "k", "allstar_key": False, "m17_key": False})
    assert "linekey.8.label" not in want and "linekey.5.label" not in want


def test_no_menu_key_without_xml_key():
    want, _ = plan_settings(parse_cfg(EXPORT), _books(), ECH, OPTS)
    assert not any(k.startswith("linekey.") for k in want)


def test_contacts_xml_groups_dedupes_and_escapes():
    import xml.etree.ElementTree as ET
    xml, n = render_contacts_xml([("ECH", [("KN0O Ext 1", "101"), ("dup", "101"), ("A & B <x>", "201")]),
                                  ("MeshPhone", [("N2MH", "781442010"), ("no number", "")]),
                                  ("Empty", [])])
    assert n == 3
    root = ET.fromstring(f"<r>{xml.split('?>', 1)[1]}</r>")
    assert [g.get("display_name") for g in root.iter("group")] == ["ECH", "MeshPhone"]
    cs = [(c.get("display_name"), c.get("office_number"), c.get("group_id_name")) for c in root.iter("contact")]
    assert cs == [("KN0O Ext 1", "101", "ECH"), ("A & B <x>", "201", "ECH"), ("N2MH", "781442010", "MeshPhone")]


def test_contacts_xml_caps_at_phone_limit():
    _, n = render_contacts_xml([("Big", [(f"p{i}", str(i)) for i in range(MAX_LOCAL_CONTACTS + 50)])])
    assert n == MAX_LOCAL_CONTACTS
