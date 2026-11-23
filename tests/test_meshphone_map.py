"""MeshPhone map: measured PBX reachability + link classification."""
import asyncio
import struct

from ech.core import meshphone_map as mm
from ech.core.meshphone_directory import parse_offices

PEERS = """Name/Username    Host                                           Mask                                      Port           Status      Description
lab-pi/lab-pi    (null)                                   (D)  (null)                                    (null)(T)      UNKNOWN
hoip-iax/100000  192.0.2.10                               (S)  255.255.255.255                           4569           OK (74 ms)
peerb/HOMETOWN   10.20.0.2                                (S)  255.255.255.255                           4569  (T)      OK (145 ms)
w1xyz/n0call     10.30.0.4                                (S)  255.255.255.255                           4569  (T)      OK (194 ms)
STORMTOWN/HOMET  10.40.0.5                                (S)  255.255.255.255                           4569  (T)      UNREACHABLE
5 iax2 peers [4 online, 1 offline, 0 unmonitored]
"""


def _off(name, call, ip="", trunks=(), lat=40.0, lon=-75.0):
    return {"name": name, "admin_callsign": call, "ip": ip, "hostname": "", "trunks": list(trunks),
            "lat": lat, "lon": lon, "location": name.title(), "office_codes": [], "dialplans": []}


def test_parse_iax_peers():
    p = {x["name"]: x for x in mm.parse_iax_peers(PEERS)}
    assert set(p) == {"lab-pi", "hoip-iax", "peerb", "w1xyz", "STORMTOWN"}
    assert p["w1xyz"] == {"name": "w1xyz", "host": "10.30.0.4", "ok": True, "ms": 194, "status": "OK"}
    assert p["STORMTOWN"]["ok"] is False and p["STORMTOWN"]["status"] == "UNREACHABLE"
    assert p["lab-pi"]["host"] == ""


def test_candidates_include_moved_pbx_but_not_handsets():
    o = _off("HARBOR", "W1XYZ", ip="10.30.0.201")
    hosts = [{"name": "w1xyz-pbx", "ip": "10.30.0.57"}, {"name": "W1XYZ-yealink-T46S-pbx", "ip": "10.1.1.1"},
             {"name": "w1xyz-hap", "ip": "10.1.1.2"}, {"name": "xw1xyz-pbx", "ip": "10.1.1.3"}]
    assert mm.candidate_ips(o, hosts) == ["10.30.0.201", "10.30.0.57"]


def test_match_peer_by_ip_name_and_callsign():
    offices = [_off("BAYTOWN", "KX6AAA", ip="10.20.0.2"), _off("STORMTOWN", "KX6AAA", ip="10.40.0.5"),
               _off("HARBOR", "W1XYZ", ip="10.30.0.201")]
    cands = {o["name"]: [o["ip"]] for o in offices}
    peers = {x["name"]: x for x in mm.parse_iax_peers(PEERS)}
    assert mm.match_peer(peers["peerb"], offices, cands) == "BAYTOWN"       # by address
    assert mm.match_peer(peers["STORMTOWN"], offices, cands) == "STORMTOWN"  # by name
    assert mm.match_peer(peers["w1xyz"], offices, cands) == "HARBOR"      # by admin callsign
    assert mm.match_peer(peers["hoip-iax"], offices, cands) is None


def test_build_map_states_and_links():
    offices = [_off("HOMETOWN", "N0CALL", trunks=[]),
               _off("HARBOR", "W1XYZ", ip="10.30.0.201", trunks=["MILLTOWN"]),
               _off("MILLTOWN", "K1AAA", ip="10.50.0.7", trunks=["HARBOR", "HUBTOWN"]),
               _off("HUBTOWN", "N2AAA", ip="10.60.0.9", trunks=["MILLTOWN", "SIDETOWN"]),
               _off("SIDETOWN", "N2AAA", trunks=[]),
               _off("STORMTOWN", "KX6AAA", ip="10.40.0.5")]
    cands = {"HARBOR": ["10.30.0.201", "10.30.0.57"], "MILLTOWN": ["10.50.0.7"],
             "HUBTOWN": ["10.60.0.9"], "SIDETOWN": [], "STORMTOWN": ["10.40.0.5"], "HOMETOWN": []}
    answers = {"HARBOR": {"ip": "10.30.0.57", "ms": 120.0}, "MILLTOWN": {"ip": "10.50.0.7", "ms": 117.0},
               "STORMTOWN": {"ip": "10.40.0.5", "ms": 93.0}}
    m = mm.build_map(offices, cands, answers, "HOMETOWN", mm.parse_iax_peers(PEERS), generated_at=1.0)
    o = {x["name"]: x for x in m["offices"]}
    assert o["HOMETOWN"]["state"] == "self"
    assert o["HARBOR"]["state"] == "up" and o["HARBOR"]["ip_moved"] is True
    assert o["HARBOR"]["answering_ip"] == "10.30.0.57"     # POKE answer wins over the trunk address
    assert o["HUBTOWN"]["state"] == "no_answer"
    assert o["SIDETOWN"]["state"] == "no_address"
    links = {frozenset((l["a"], l["b"])): l for l in m["links"]}
    assert links[frozenset(("MILLTOWN", "HARBOR"))]["state"] == "both_up" and links[frozenset(("MILLTOWN", "HARBOR"))]["mutual"]
    assert links[frozenset(("MILLTOWN", "HUBTOWN"))]["state"] == "end_down"
    assert links[frozenset(("SIDETOWN", "HUBTOWN"))]["state"] == "end_down"      # HUBTOWN doesn't answer
    assert not links[frozenset(("SIDETOWN", "HUBTOWN"))]["mutual"]
    # our own trunks come from Asterisk: verified, or down when Asterisk says so
    assert links[frozenset(("HARBOR", "HOMETOWN"))]["state"] == "verified"
    assert links[frozenset(("HARBOR", "HOMETOWN"))]["rtt_ms"] == 194 and links[frozenset(("HARBOR", "HOMETOWN"))]["peer"] == "w1xyz"
    assert links[frozenset(("STORMTOWN", "HOMETOWN"))]["state"] == "down"
    assert m["summary"]["moved"] == 1


def test_trunk_to_unlisted_office_is_flagged():
    m = mm.build_map([_off("A", "K1A", trunks=["GHOST"])], {"A": []}, {}, "")
    assert m["links"][0]["state"] == "unlisted_end"
    assert m["summary"]["unlisted_trunk_names"] == ["GHOST"]


def test_poke_frame_layout():
    f = mm.poke_frame(0x1234)
    scall, dcall, ts, oseq, iseq, ftype, sub = struct.unpack(">HHIBBBB", f)
    assert scall == 0x9234 and dcall == 0 and ftype == 6 and sub == 0x1E


def test_iax_poke_against_a_local_pong():
    async def run():
        loop = asyncio.get_event_loop()

        class Pong(asyncio.DatagramProtocol):
            def connection_made(self, t):
                self.t = t

            def datagram_received(self, data, addr):
                assert data[10] == 6 and data[11] == 0x1E
                self.t.sendto(data[:10] + bytes([6, 3]), addr)

        t, _ = await loop.create_datagram_endpoint(Pong, local_addr=("127.0.0.1", 0))
        port = t.get_extra_info("sockname")[1]
        old = mm.IAX_PORT
        mm.IAX_PORT = port
        try:
            ms = await mm.iax_poke("127.0.0.1", timeout=1)
            mm.IAX_PORT = 1      # nothing listening
            none = await mm.iax_poke("127.0.0.1", timeout=0.3, tries=1)
        finally:
            mm.IAX_PORT = old
            t.close()
        return ms, none
    ms, none = asyncio.run(run())
    assert ms is not None and ms >= 0 and none is None


def test_parse_offices_keeps_map_fields():
    o = parse_offices([{"name": "MILLTOWN", "latitude": "40.5", "longitude": "-75.5", "ip_address": "10.50.0.7",
                        "hostname": None, "pbx_type": "FreePBX", "network": "us", "admin_email": "x@y",
                        "admin_name": "Someone", "trunks": [], "office_codes": []}])[0]
    assert o["lat"] == 40.5 and o["lon"] == -75.5 and o["ip"] == "10.50.0.7" and o["pbx_type"] == "FreePBX"
    assert o["admin_email"] == "x@y" or o["admin_email"] == ""
    assert o["admin_name"] == "Someone"
    bad = parse_offices([{"name": "Q", "admin_email": "<script>@x", "trunks": [], "office_codes": []}])[0]
    assert bad["admin_email"] == ""


def test_own_verified_trunk_proves_the_office_is_up():
    offices = [_off("HOMETOWN", "N0CALL"), _off("HARBOR", "W1XYZ", ip="10.30.0.201")]
    m = mm.build_map(offices, {"HARBOR": ["10.30.0.201"], "HOMETOWN": []}, {}, "HOMETOWN",
                     mm.parse_iax_peers(PEERS))
    d = next(o for o in m["offices"] if o["name"] == "HARBOR")
    assert d["state"] == "up" and d["answering_ip"] == "10.30.0.4" and d["rtt_ms"] == 194 and d["ip_moved"]
