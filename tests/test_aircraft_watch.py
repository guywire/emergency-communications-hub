"""Military / rescue aircraft classes and NGR&S spot parsing."""
from ech.core import aircraft_watch as aw


def test_parse_ngrs_spots():
    s = aw.parse_spot("[HELO: LIFE] N901XM Alt:1275ft Sq:0377 Hex:AC74FC @12:12")
    assert s == {"kind": "HELO", "type": "LIFE", "call": "N901XM", "alt_ft": 1275, "squawk": "0377",
                 "hex": "ac74fc", "hhmm": "12:12"}
    assert aw.tag_class(s) == "rescue"
    m = aw.parse_spot("[MIL: ACFT] N/A Alt:N/Aft Sq:N/A Hex:AE0377 @09:32")
    assert m["call"] is None and m["alt_ft"] is None and aw.tag_class(m) == "military"
    assert aw.tag_class(aw.parse_spot("[MIL-HELO: P8] C6050 Alt:2100ft Sq:1200 Hex:AE4E02 @14:41")) == "military"
    assert aw.tag_class(aw.parse_spot("[HELO: R44] N417PK Alt:1300ft Sq:0353 Hex:A4EF70 @07:24")) == "helicopter"
    assert aw.parse_spot("--- Gloucester Hill R&S Node (12:02 PM) ---") is None


def test_classify_our_aircraft():
    assert aw.classify("a1b2c3", "UAL12", "7700")[0] == "emergency"
    assert aw.classify("ae07b9", "RCH144")[0] == "military"            # US mil block
    assert aw.classify("a1b2c3", "RCH144")[0] == "military"            # callsign
    assert aw.classify("a1b2c3", "C6050")[0] == "rescue"               # Coast Guard
    assert aw.classify("a1b2c3", "LIFE3")[0] == "rescue"
    assert aw.classify("a1b2c3", "N123AB", category="A7")[0] == "helicopter"
    learned = {"ac74fc": {"class": "rescue", "label": "HELO: LIFE"}}
    assert aw.classify("ac74fc", "N901XM", learned=learned) == ("rescue", "NGR&S: HELO: LIFE")
    assert aw.classify("a1b2c3", "DAL455", "1200", "A3") is None       # an airliner


def test_adsblol_ingest_and_military_flag():
    import json
    from ech.adapters.adsblol_adapter import ADSBLolAdapter
    a = ADSBLolAdapter({"name": "adsb-lol", "lat": 44.8, "lon": -68.8})
    n = a.ingest({"ac": [
        {"hex": "ae07b9", "flight": "RCH147  ", "lat": 44.5, "lon": -69.0, "alt_baro": 31000, "gs": 420,
         "track": 210, "t": "K35R", "r": "57-1432", "dbFlags": 1, "category": "A5", "seen_pos": 1},
        {"hex": "a1b2c3", "flight": "N12AB", "lat": 44.6, "lon": -69.1, "alt_baro": 900, "t": "R44", "seen_pos": 2},
        {"hex": "a4a4a4", "lat": None, "lon": None}]})
    assert n == 2
    m = a._nodes["icao:ae07b9"].meta
    assert m["military"] and m["aircraft_type"] == "K35R" and m["flight"] == "RCH147"
    assert aw.classify("a1b2c3", "N12AB", aircraft_type="R44")[0] == "helicopter"
    assert aw.classify("123456", "N43128", military=True, aircraft_type="P28A")[0] == "military"
