"""Relay names learned from raw ADVERT packets (our RF log or an observer)."""

from ech.adapters.meshcore import MeshCoreAdapter

# Real advert captured from the LAN observer feed 2026-10-20 (4-hop flood).
BRUNSWICK = bytes.fromhex(
    "11841BF15CC299866B08961551F6F8927761EE2E3AEBE04194023FBB2FB3395393346947D9852123"
    "F8E3239D92946C05D86AD8E19AE0D4FD0EE0E5D481A16DC3419906363C7D60C1EA4E2E2FFD38C88A"
    "1F33F12E1149815D101577A9C911B82DF54C0AFAB6B70EFA17652F23B1B789BDFD0192D5609D02EF"
    "2AD4FB4272756E737769636B2D5250542D33")


def _adapter():
    return MeshCoreAdapter({"name": "mc-test", "transport": "serial", "port": "/dev/null"})


def test_real_advert_learned_with_name_type_position():
    a = _adapter()
    assert a.learn_advert(BRUNSWICK, "observer:MQTT Listener") == "F8927761EE2E"
    n = a._nodes["F8927761EE2E"]
    assert n.display_name == "Brunswick-RPT-3"
    assert n.meta["node_type"] == "REP"
    assert (round(n.lat, 4), round(n.lon, 4)) == (43.8684, -69.9815)
    assert a._resolve_traffic_node("f892")["name"] == "Brunswick-RPT-3"


def test_tampered_advert_rejected():
    a = _adapter()
    forged = BRUNSWICK[:-1] + b"X"          # rename -> signature no longer valid
    assert a.learn_advert(forged, "observer:x") is None
    assert not a._nodes


def test_contact_name_not_overwritten():
    a = _adapter()
    a.learn_advert(BRUNSWICK)
    n = a._nodes["F8927761EE2E"]
    n.display_name, n.name_source = "My label", "contact"
    a.learn_advert(BRUNSWICK)
    assert n.display_name == "My label"
