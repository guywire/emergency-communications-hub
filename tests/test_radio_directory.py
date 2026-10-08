"""
tests/test_radio_directory.py
--------------------------------
Covers O98: HamVOIP/AllStarLink + AREDN directory. Built-in entries are
real, confirmed-via-research test/parrot node numbers only (see module
docstring for why "group"/"music" and all AREDN entries are intentionally
NOT built in); operator-supplied entries from config.yaml merge in on top.
"""

from ech.core.radio_directory import BUILTIN_AREDN_ENTRIES, BUILTIN_HAMVOIP_ENTRIES, get_directory


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


def test_get_directory_without_config_returns_builtins_only():
    d = get_directory({})
    assert d["hamvoip"] == BUILTIN_HAMVOIP_ENTRIES
    assert d["aredn"] == []


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
    assert len(d["hamvoip"]) == len(BUILTIN_HAMVOIP_ENTRIES) + 1
    assert d["hamvoip"][-1]["name"] == "Local ARES hub"
    assert len(d["aredn"]) == 1
    assert d["aredn"][0]["destination"] == "n0call-gw.local.mesh"


def test_get_directory_handles_missing_sections_gracefully():
    assert get_directory({"radio_directory": {}}) == {"hamvoip": BUILTIN_HAMVOIP_ENTRIES, "aredn": []}
    assert get_directory({"radio_directory": None}) == {"hamvoip": BUILTIN_HAMVOIP_ENTRIES, "aredn": []}
