"""Internet audio streams on the PBX: config merge, URL safety, dialplan text, file sync."""
from ech.core import pbx_streams as ps


def test_defaults_present_and_in_7xx():
    s = ps.streams_from_config({})
    assert len(s) == len(ps.DEFAULT_STREAMS)
    assert all(x["ext"].startswith("7") and len(x["ext"]) == 3 for x in s)


def test_config_adds_overrides_and_drops():
    cfg = {"pbx_streams": {"streams": [
        {"ext": "731", "name": "Club net", "url": "http://example.org:8000/net.mp3"},
        {"ext": "701", "name": "Renamed"},
        {"ext": "702", "enabled": False},
        {"ext": "999", "name": "bad ext", "url": "http://x/y.mp3"},
        {"ext": "751", "name": "AllStar range", "url": "http://x/y.mp3"},
        {"ext": "732", "name": "comma", "url": "http://x/a,b.mp3"},
        {"ext": "733", "name": "var", "url": "http://x/${EXTEN}.mp3"},
    ]}}
    s = {x["ext"]: x for x in ps.streams_from_config(cfg)}
    assert s["731"]["url"] == "http://example.org:8000/net.mp3"
    assert s["701"]["name"] == "Renamed" and s["701"]["url"].startswith("http://ice1.somafm.com/")
    assert "702" not in s and "999" not in s and "751" not in s and "732" not in s and "733" not in s


def test_disabled_or_no_defaults():
    assert ps.streams_from_config({"pbx_streams": {"enabled": False}}) == []
    only = ps.streams_from_config({"pbx_streams": {"defaults": False, "streams": [
        {"ext": "740", "name": "X", "url": "https://h/x.mp3"}]}})
    assert [x["ext"] for x in only] == ["740"]


def test_dialplan_lines():
    text = ps.render_dialplan([{"ext": "701", "name": "Groove; Salad", "url": "http://h/s.mp3"}])
    assert "exten => 701,1,NoOp(ECH stream: Groove Salad)" in text
    assert " same => n,MP3Player(http://h/s.mp3)" in text
    assert "[" not in text.split("\n", 2)[2]        # no context header: it is #included inside one


def test_sync_writes_once(tmp_path):
    f = tmp_path / "ech_streams.conf"
    assert ps.sync_conf({}, str(f)) is True
    assert ps.sync_conf({}, str(f)) is False
    assert "MP3Player" in f.read_text()


def test_m17_listen_dial_forces_slin():
    from ech.core.pbx_streams import render_audio_conf
    conf = render_audio_conf({"enabled": True, "allstar_node": "", "port": 9093, "webphone_ext": "111", "m17_ext": "760"})
    assert "Dial(AudioSocket/127.0.0.1:9093/${ECHU}/c(slin))" in conf
