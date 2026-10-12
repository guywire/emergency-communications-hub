from ech.adapters.meshtastic_adapter import _packet_node_id


def test_from_id_used_when_present():
    assert _packet_node_id({"fromId": "!a1b2c3d4", "from": 1}, "fromId", "from") == "!a1b2c3d4"


def test_null_from_id_falls_back_to_numeric_from():
    # meshtastic lib leaves fromId=None for nodes not yet in its node table
    assert _packet_node_id({"fromId": None, "from": 0xA1B2C3D4}, "fromId", "from") == "!a1b2c3d4"


def test_missing_and_empty_from_id_fall_back():
    assert _packet_node_id({"from": 255}, "fromId", "from") == "!000000ff"
    assert _packet_node_id({"fromId": "  ", "from": 255}, "fromId", "from") == "!000000ff"


def test_nothing_known_returns_none():
    assert _packet_node_id({"fromId": None}, "fromId", "from") is None


def test_broadcast_destination():
    assert _packet_node_id({"toId": None, "to": 0xFFFFFFFF}, "toId", "to") == "^all"
