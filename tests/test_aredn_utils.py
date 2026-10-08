"""
tests/test_aredn_utils.py
---------------------------
Covers O101: AREDN-address classification used by aredn_ami.py and
aredn_meshchat.py to flag (non-blocking) when a configured host doesn't
look like a typical AREDN mesh address. See ech/core/aredn_utils.py's
module docstring for why this checks 10.x.x.x/.local.mesh (the real
default AREDN addressing) rather than 44.x.x.x (which turned out, per
AREDN's own current docs, to be an opt-in LAN-side-only mode, not the
general case) — and why "VLAN-awareness" was researched and ruled out as
not applicable to a Python IP/HTTP client adapter.
"""

from ech.core.aredn_utils import classify_mesh_address, is_mesh_typical


def test_local_mesh_hostname_recognized():
    c = classify_mesh_address("mynode.local.mesh")
    assert c == "aredn-hostname (.local.mesh)"
    assert is_mesh_typical(c)


def test_local_mesh_hostname_case_insensitive():
    assert classify_mesh_address("MyNode.Local.Mesh").startswith("aredn-hostname")


def test_standard_mesh_10_range_recognized():
    c = classify_mesh_address("10.42.1.5")
    assert c == "aredn-mesh (10.x.x.x, standard node range)"
    assert is_mesh_typical(c)


def test_amprnet_44_range_recognized():
    c = classify_mesh_address("44.1.2.3")
    assert c.startswith("aredn-44net")
    assert is_mesh_typical(c)

    c2 = classify_mesh_address("44.130.1.1")
    assert c2.startswith("aredn-44net")
    assert is_mesh_typical(c2)


def test_public_ip_flagged_not_mesh_typical():
    c = classify_mesh_address("8.8.8.8")
    assert c.startswith("public-ip")
    assert not is_mesh_typical(c)


def test_other_private_range_flagged_but_not_mesh_typical():
    """192.168.x.x is a real private range but NOT AREDN's standard mesh
    range — should be distinguishable from the 10.x.x.x mesh case."""
    c = classify_mesh_address("192.168.1.1")
    assert c.startswith("private-ip")
    assert not is_mesh_typical(c)


def test_loopback_recognized():
    assert classify_mesh_address("127.0.0.1") == "loopback"


def test_unparseable_hostname_flagged():
    c = classify_mesh_address("some-random-host.example.com")
    assert c == "unrecognized-hostname"
    assert not is_mesh_typical(c)


def test_empty_host_is_unset_and_mesh_typical():
    """Empty/missing host shouldn't itself trigger a 'not mesh-typical'
    warning — that's a separate validation concern (required field)."""
    c = classify_mesh_address("")
    assert c == "unset"
    assert is_mesh_typical(c)


# ── Wired into the real adapters' _health_detail() ──

def test_aredn_ami_health_detail_includes_address_class():
    from ech.adapters.aredn_ami import AREDNAMIAdapter
    a = AREDNAMIAdapter({"name": "pbx-test", "host": "10.1.2.3", "username": "u", "secret": "s"})
    assert a._health_detail()["address_class"] == "aredn-mesh (10.x.x.x, standard node range)"

    b = AREDNAMIAdapter({"name": "pbx-test2", "host": "8.8.8.8", "username": "u", "secret": "s"})
    assert b._health_detail()["address_class"].startswith("public-ip")


def test_aredn_meshchat_health_detail_includes_address_class():
    from ech.adapters.aredn_meshchat import AREDNMeshChatAdapter
    a = AREDNMeshChatAdapter({
        "name": "chat-test", "base_url": "http://mynode.local.mesh/cgi-bin/meshchat",
        "call_sign": "N0CALL",
    })
    assert a._health_detail()["address_class"] == "aredn-hostname (.local.mesh)"

    b = AREDNMeshChatAdapter({
        "name": "chat-test2", "base_url": "http://192.168.1.50/cgi-bin/meshchat",
        "call_sign": "N0CALL",
    })
    assert b._health_detail()["address_class"].startswith("private-ip")
