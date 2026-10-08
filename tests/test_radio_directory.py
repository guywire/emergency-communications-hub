"""
tests/test_radio_directory.py
--------------------------------
Covers O98: HamVOIP/AllStarLink + AREDN directory, plus the full HOIP
(Hams Over IP) network directory the live PBX adapter is actually trunked
to — test numbers, conference bridges, RF/AllStar links, and audio feeds,
sourced from HOIP's own wiki reference pages. Built-in generic AllStarLink
entries are real, confirmed-via-research test/parrot node numbers only
(see module docstring for why "group"/"music" and all AREDN entries are
intentionally NOT built in); operator-supplied entries from config.yaml
merge in on top.
"""

from ech.core.radio_directory import (
    BUILTIN_AREDN_ENTRIES, BUILTIN_HAMVOIP_ENTRIES, HOIP_ALL_ENTRIES,
    HOIP_TEST_ENTRIES, HOIP_CONFERENCE_US_ENTRIES, HOIP_CONFERENCE_EU_ENTRIES,
    HOIP_CONFERENCE_AP_ENTRIES, HOIP_RF_LINK_US_ENTRIES, HOIP_RF_LINK_EU_ENTRIES,
    HOIP_RF_LINK_AP_ENTRIES, HOIP_AUDIO_FEED_ENTRIES, get_directory,
)


def test_builtin_hamvoip_entries_are_all_test_category():
    """No built-in 'group' or 'music' entries — see module docstring for why."""
    assert len(BUILTIN_HAMVOIP_ENTRIES) > 0
    assert all(e["category"] == "test" for e in BUILTIN_HAMVOIP_ENTRIES)


def test_builtin_hamvoip_entries_have_required_fields():
    for e in BUILTIN_HAMVOIP_ENTRIES:
        assert e["name"] and e["destination"] and e["description"] and e["source"]


def test_no_builtin_aredn_entries():
    """AREDN addresses are private to each physical mesh — no universal list exists."""
    assert BUILTIN_AREDN_ENTRIES == []


# ── HOIP directory (test numbers / conference bridges / RF links / audio feeds) ──

def test_hoip_sub_lists_all_have_required_fields_and_distinct_categories():
    sub_lists = [
        HOIP_TEST_ENTRIES, HOIP_CONFERENCE_US_ENTRIES, HOIP_CONFERENCE_EU_ENTRIES,
        HOIP_CONFERENCE_AP_ENTRIES, HOIP_RF_LINK_US_ENTRIES, HOIP_RF_LINK_EU_ENTRIES,
        HOIP_RF_LINK_AP_ENTRIES, HOIP_AUDIO_FEED_ENTRIES,
    ]
    categories = set()
    for sub in sub_lists:
        assert len(sub) > 0
        for e in sub:
            assert e["name"] and e["destination"] and e["description"] and e["source"]
            assert e["destination"].isdigit()
            categories.add(e["category"])
    # Each sub-list is its own category — this is what keeps ~150 HOIP
    # entries from rendering as one unmanageable flat group in the UI.
    assert len(categories) == len(sub_lists)


def test_hoip_all_entries_is_concatenation_of_sub_lists():
    assert len(HOIP_ALL_ENTRIES) == (
        len(HOIP_TEST_ENTRIES) + len(HOIP_CONFERENCE_US_ENTRIES) + len(HOIP_CONFERENCE_EU_ENTRIES)
        + len(HOIP_CONFERENCE_AP_ENTRIES) + len(HOIP_RF_LINK_US_ENTRIES) + len(HOIP_RF_LINK_EU_ENTRIES)
        + len(HOIP_RF_LINK_AP_ENTRIES) + len(HOIP_AUDIO_FEED_ENTRIES)
    )


def test_hoip_destinations_are_unique_within_each_sub_list():
    for sub in (HOIP_TEST_ENTRIES, HOIP_CONFERENCE_US_ENTRIES, HOIP_CONFERENCE_EU_ENTRIES,
                HOIP_CONFERENCE_AP_ENTRIES, HOIP_RF_LINK_US_ENTRIES, HOIP_RF_LINK_EU_ENTRIES,
                HOIP_RF_LINK_AP_ENTRIES, HOIP_AUDIO_FEED_ENTRIES):
        dests = [e["destination"] for e in sub]
        assert len(dests) == len(set(dests)), f"duplicate destination in {sub[0]['category']}"


def test_hoip_audio_feed_entries_include_known_feeds():
    """Spot-check specific real entries researched this session — if these
    disappear, it's a sign the list was accidentally emptied/overwritten,
    not that HOIP removed them."""
    names = {e["name"] for e in HOIP_AUDIO_FEED_ENTRIES}
    assert "TOP 80's" in names
    dests = {e["name"]: e["destination"] for e in HOIP_AUDIO_FEED_ENTRIES}
    assert dests["TOLEDO, OH POLICE DISPATCH"] == "90025"


def test_hoip_test_entries_include_echo_test():
    dests = {e["name"]: e["destination"] for e in HOIP_TEST_ENTRIES}
    assert dests["Echo Test"] == "3194"


# ── get_directory() merge behavior ──

def test_get_directory_without_config_returns_builtins_only():
    d = get_directory({})
    assert d["hamvoip"] == BUILTIN_HAMVOIP_ENTRIES + HOIP_ALL_ENTRIES
    assert d["aredn"] == []


def test_get_directory_includes_hoip_entries():
    d = get_directory({})
    categories = {e["category"] for e in d["hamvoip"]}
    assert "hoip_test" in categories
    assert "hoip_conference_us" in categories
    assert "hoip_rf_link_eu" in categories
    assert "hoip_audio_feed" in categories


def test_get_directory_merges_operator_entries():
    cfg = {
        "radio_directory": {
            "hamvoip_entries": [
                {"name": "Local ARES hub", "destination": "12345", "category": "group",
                 "description": "county net hub"},
            ],
            "aredn_entries": [
                {"name": "Regional gateway", "destination": "n0call-gw.local.mesh",
                 "description": "main AREDN tunnel"},
            ],
        }
    }
    d = get_directory(cfg)
    assert len(d["hamvoip"]) == len(BUILTIN_HAMVOIP_ENTRIES) + len(HOIP_ALL_ENTRIES) + 1
    assert d["hamvoip"][-1]["name"] == "Local ARES hub"
    assert len(d["aredn"]) == 1
    assert d["aredn"][0]["destination"] == "n0call-gw.local.mesh"


def test_get_directory_handles_missing_sections_gracefully():
    expected_hamvoip = BUILTIN_HAMVOIP_ENTRIES + HOIP_ALL_ENTRIES
    assert get_directory({"radio_directory": {}}) == {"hamvoip": expected_hamvoip, "aredn": []}
    assert get_directory({"radio_directory": None}) == {"hamvoip": expected_hamvoip, "aredn": []}
