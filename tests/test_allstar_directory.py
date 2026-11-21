"""AllStar node directory for the phone panel (2026-11-21). Lines are real astdb entries."""
from ech.core import yealink_xml as yx
from ech.core.allstar_directory import AllStarDirectory, parse_astdb, state_of

SAMPLE = """2000|WB6NIL|ASL Public Hub|aws-east2ls-ast0
2002|WB6NIL|AllStarLink Parrot|AWS US-EAST-1
63018|KN0O||Rockport Maine
521771|W1RKD|147.060 +|Rockland,Me
620860|W1PBR|Club Cloud Hub|Hope, Maine
620861|W1PBR|147.060|Hope, Maine
41234|N1XYZ|Repeater|Charleston, West Virginia
41235|K8ABC|Hub|Richmond, Virginia
41236|W1ME|Home|Paris, France
garbage line
"""


def test_parse_skips_junk():
    rows = parse_astdb(SAMPLE)
    assert len(rows) == 9 and rows[2] == {"node": "63018", "call": "KN0O", "desc": "", "loc": "Rockport Maine"}


def test_state_of_free_text_locations():
    assert state_of("Rockport Maine") == "maine"
    assert state_of("Hope, Maine") == "maine"
    assert state_of("Rockland,Me") == "maine"
    assert state_of("Bangor ME 04401") == "maine"
    assert state_of("Charleston, West Virginia") == "west virginia"
    assert state_of("Richmond, Virginia") == "virginia"
    assert state_of("Paris, France") == "" and state_of("aws-east2ls-ast0") == ""


def test_state_list_search_and_lookup(tmp_path):
    cache = tmp_path / "allstar_nodes.txt"
    cache.write_text(SAMPLE)
    d = AllStarDirectory(cache)
    assert [r["node"] for r in d.in_state("maine")] == ["63018", "620860", "620861", "521771"]
    assert [r["node"] for r in d.search_call("w1p")] == ["620860", "620861"]
    assert d.search_call("x") == []
    assert d.lookup("620860")["desc"] == "Club Cloud Hub" and d.lookup("1") is None


def test_phone_screens(tmp_path):
    rows = parse_astdb(SAMPLE)
    menu = yx.allstar_dir_menu("B", "maine", 4, len(rows))
    assert "Nodes in Maine (4)" in menu and "B/allstar/dir/callinput.xml" in menu
    page = yx.allstar_node_list("B", "AllStar Maine", rows * 5, "B/allstar/dir/state.xml", 1)
    assert "Next page" in page and "B/allstar/node.xml?node=2000" in page
    assert "B/allstar/dir/state.xml?page=2" in page.replace("&amp;", "&")
    node = yx.allstar_node_menu("B", rows[4], linked=False).replace("&amp;", "&")
    assert "link.xml?node=620860&dial=1" in node and "confirm=tx" in node
    assert "unlink.xml?node=620860" in yx.allstar_node_menu("B", rows[4], linked=True)
    assert "mode=tx" in yx.allstar_confirm_tx("B", "63018", rows[4])
    assert 'type="string"' in yx.input_screen("t", "u", "q", "Callsign:", kind="string")
