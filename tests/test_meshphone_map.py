"""MeshPhone map: directory as the guide, facts polled from the mesh (all data made up)."""
import asyncio
import struct

from ech.core import meshphone_map as mm
from ech.core.meshphone_directory import parse_offices

PEERS = """Name/Username    Host                                           Mask                                      Port           Status      Description
lab-pi/lab-pi    (null)                                   (D)  (null)                                    (null)(T)      UNKNOWN
hoip-iax/100000  192.0.2.10                               (S)  255.255.255.255                           4569           OK (74 ms)
peerb/HOMETOWN   10.20.0.2                                (S)  255.255.255.255                           4569  (T)      OK (145 ms)
w1xyz/n0call     10.30.0.44                               (S)  255.255.255.255                           4569  (T)      OK (194 ms)
STORMTOWN/HOMET  10.40.0.5                                (S)  255.255.255.255                           4569  (T)      UNREACHABLE
5 iax2 peers [4 online, 1 offline, 0 unmonitored]
"""

# the AREDN host list: each node, then the hosts on its LAN
HOSTS = [
    {"name": "K9DECOY-NODE", "ip": "10.30.0.210"},          # another node's mesh IP in the same /24
    {"name": "N0CALL-HOME-NODE", "ip": "10.1.0.1"}, {"name": "N0CALL-pbx-ech", "ip": "10.9.9.44"},
    {"name": "W1XYZ-HARBOR-NODE", "ip": "10.2.0.1"}, {"name": "w1xyz-pbx", "ip": "10.30.0.57"},
    {"name": "w1xyz-mailhub", "ip": "10.30.0.43"},
    {"name": "K1AAA-MILL-NODE", "ip": "10.3.0.1"}, {"name": "k1aaa-pbx", "ip": "10.50.0.7"},
    {"name": "K1AAA-yealink-t46s-pbx", "ip": "10.50.0.8"},
    {"name": "KX6AAA-NODE", "ip": "10.4.0.1"}, {"name": "kx6aaa-raspbx", "ip": "10.20.0.2"},
    {"name": "stormbox", "ip": "10.40.0.5"},
    {"name": "Q1ASL-NODE", "ip": "10.6.0.1"}, {"name": "allstar-node61999", "ip": "10.80.0.2"},
    {"name": "N9QQQ-NODE", "ip": "10.5.0.1"}, {"name": "n9qqq-asterisk", "ip": "10.60.0.9"},
    {"name": "N9QQQ-shack", "ip": "10.60.0.10"},
]
SERVICES = [{"name": "MeshPhone 937-1000 [phone]", "ip": "10.5.0.1", "link": "", "protocol": "tcp"},
            {"name": "N9 PBX (MeshPhone 937-10xx)", "ip": "10.5.0.1", "link": "http://N9QQQ-shack:80/", "protocol": "tcp"}]
NODES = {"10.1.0.1": {"name": "N0CALL-HOME-NODE", "lan": "10.9.9.33", "lat": 45.0, "lon": -70.0, "grid": "FN55aa", "place": "Hometown, ME"},
         "10.2.0.1": {"name": "W1XYZ-HARBOR-NODE", "lan": "10.30.0.33", "lat": 41.6, "lon": -70.9, "grid": "FN41", "place": "Harbor, MA"},
         "10.3.0.1": {"name": "K1AAA-MILL-NODE", "lat": 42.0, "lon": -71.6, "grid": "FN42", "place": "Milltown, RI"},
         "10.5.0.1": {"name": "N9QQQ-NODE", "lat": 39.7, "lon": -84.2, "grid": "EM79", "place": "Riverton, OH"}}


def _off(name, call, ip="", trunks=(), lat=None, lon=None, location=""):
    return {"name": name, "admin_callsign": call, "ip": ip, "hostname": "", "trunks": list(trunks),
            "lat": lat, "lon": lon, "location": location or name.title(), "office_codes": [], "dialplans": []}


OFFICES = [_off("HOMETOWN", "N0CALL"),
           _off("HARBOR", "W1XYZ", ip="10.30.0.201", trunks=["MILLTOWN"], lat=41.6, lon=-70.9),
           _off("MILLTOWN", "K1AAA", ip="10.50.0.7", trunks=["HARBOR", "HUBTOWN"], lat=35.0, lon=-80.0,
                location="Wrongville, NC"),
           _off("HUBTOWN", "N2AAA", ip="10.70.0.9", trunks=["MILLTOWN", "SIDETOWN"], lat=40.7, lon=-74.2),
           _off("SIDETOWN", "N2AAA", lat=40.0, lon=-75.0),
           _off("BAYTOWN", "KX6AAA", ip="10.20.0.2"),
           _off("STORMTOWN", "KX6AAA", ip="10.40.0.5")]


def _build(answers=None, reports=None, people=None):
    cands = {o["name"]: mm.office_candidates(o, HOSTS) for o in OFFICES}
    mesh = mm.mesh_candidates(HOSTS, SERVICES)
    if answers is None:
        answers = {"10.30.0.57": 120.0, "10.50.0.7": 117.0, "10.60.0.9": 88.0, "10.80.0.2": 99.0}
    return mm.build_map(OFFICES, HOSTS, SERVICES, cands, mesh, answers, NODES, {"10.9.9.44"},
                        "HOMETOWN", mm.parse_iax_peers(PEERS), people or [], reports or [], generated_at=1.0)


def test_parse_iax_peers():
    p = {x["name"]: x for x in mm.parse_iax_peers(PEERS)}
    assert set(p) == {"lab-pi", "hoip-iax", "peerb", "w1xyz", "STORMTOWN"}
    assert p["w1xyz"] == {"name": "w1xyz", "host": "10.30.0.44", "ok": True, "ms": 194, "status": "OK"}
    assert p["STORMTOWN"]["ok"] is False and p["lab-pi"]["host"] == ""


def test_node_of_follows_the_host_list_grouping():
    idx = {h["name"]: i for i, h in enumerate(HOSTS)}
    assert mm.node_of(HOSTS, idx["w1xyz-mailhub"])["name"] == "W1XYZ-HARBOR-NODE"
    assert mm.node_of(HOSTS, idx["k1aaa-pbx"])["name"] == "K1AAA-MILL-NODE"
    # an address not in the list sits with its LAN neighbours
    assert mm.node_of(HOSTS, mm.host_index(HOSTS, "10.30.0.44"))["name"] == "W1XYZ-HARBOR-NODE"


def test_candidates():
    assert mm.office_candidates(OFFICES[1], HOSTS) == ["10.30.0.201", "10.30.0.57"]
    mesh = mm.mesh_candidates(HOSTS, SERVICES)
    assert "10.50.0.8" not in mesh                       # a handset named "...-pbx"
    assert mesh["10.60.0.9"] == ["n9qqq-asterisk"]
    assert mesh["10.60.0.10"] == ["N9QQQ-shack"]         # linked from a PBX service
    assert mm.numbers_in("N9 PBX (MeshPhone 937-10xx) and 937-1000") == ["937-10xx", "937-1000"]


def test_offices_located_by_their_node_not_the_directory():
    m = _build()
    o = {x["name"]: x for x in m["offices"]}
    assert o["HOMETOWN"]["state"] == "self" and o["HOMETOWN"]["loc_source"] == "node"
    h = o["HARBOR"]
    assert h["state"] == "up" and h["ip_moved"] and h["answering_ip"] == "10.30.0.57"
    assert (h["lat"], h["place"], h["loc_off_km"]) == (41.6, "Harbor, MA", None)
    mt = o["MILLTOWN"]
    assert mt["loc_source"] == "node" and mt["lat"] == 42.0 and mt["loc_off_km"] > 500   # directory was wrong
    # listed, not answering and not on the mesh at all: not shown
    assert "HUBTOWN" not in o and "SIDETOWN" not in o
    assert m["summary"]["dropped"] == ["HUBTOWN", "SIDETOWN"]
    # a trunk Asterisk qualifies proves its PBX is up, at the trunk address
    assert o["BAYTOWN"]["state"] == "up" and o["BAYTOWN"]["rtt_ms"] == 145


def test_pbx_found_only_on_the_mesh_is_added_and_named_from_white_pages():
    people = [{"callsign": "N9QQQ", "name": "Pat", "number": "9371000", "pbx": "RIVERTON"},
              {"callsign": "N0ZZZ", "name": "Lee", "number": "9371001", "pbx": "RIVERTON"}]
    m = _build(people=people)
    r = next(x for x in m["offices"] if x["name"] == "RIVERTON")
    assert not r["in_directory"] and r["state"] == "up" and r["rtt_ms"] == 88.0
    assert r["admin_callsign"] == "N9QQQ" and r["place"] == "Riverton, OH" and r["grid"] == "EM79"
    assert "937-1000" in r["numbers"] and "937-10xx" in r["numbers"]
    assert m["summary"]["mesh_only"] == 1
    assert r["people_count"] == 2 and r["number_range"] == "937-10xx"
    assert r["people_at"][0] == {"callsign": "N9QQQ", "name": "Pat", "number": "9371000"}
    # people get their office's node-derived location
    assert {p["callsign"]: p["location"] for p in m["people"]} == {"N9QQQ": "Riverton, OH", "N0ZZZ": "Riverton, OH"}


def test_links_reported_verified_and_inferred():
    reports = [{"by": "MILLTOWN", "to": "HARBOR", "ms": 40, "down": False},
               {"by": "HARBOR", "to": "MILLTOWN", "ms": 60, "down": False},
               {"by": "MILLTOWN", "to": "HUBTOWN", "ms": None, "down": True},
               {"by": "HUBTOWN", "to": "MILLTOWN", "ms": 90, "down": False}]
    m = _build(reports=reports)
    links = {frozenset((l["a"], l["b"])): l for l in m["links"]}
    hm = links[frozenset(("HARBOR", "MILLTOWN"))]
    assert hm["state"] == "reported_up" and hm["rtt_ms"] == 50 and len(hm["reports"]) == 2
    assert frozenset(("HUBTOWN", "MILLTOWN")) not in links          # HUBTOWN doesn't exist
    assert frozenset(("HUBTOWN", "SIDETOWN")) not in links
    v = links[frozenset(("HARBOR", "HOMETOWN"))]
    assert v["state"] == "verified" and v["rtt_ms"] == 194 and v["peer"] == "w1xyz"
    assert links[frozenset(("HOMETOWN", "STORMTOWN"))]["state"] == "down"
    assert not any("192.0.2.10" in str(l) for l in m["links"])     # the internet trunk isn't a mesh link


def test_parse_trunk_reports():
    html = ('<div style="height: 100.0%;">HARBOR - 97&nbsp;ms - HOMETOWN</div>'
            '<div style="height: 100.0%;">MILLTOWN - DOWN - HARBOR</div><div>A - UNKNOWN - B</div>')
    assert mm.parse_trunk_reports(html) == [{"by": "HARBOR", "to": "HOMETOWN", "ms": 97, "down": False},
                                            {"by": "MILLTOWN", "to": "HARBOR", "ms": None, "down": True}]


def test_poke_frame_layout():
    scall, dcall, ts, oseq, iseq, ftype, sub = struct.unpack(">HHIBBBB", mm.poke_frame(0x1234))
    assert scall == 0x9234 and dcall == 0 and ftype == 6 and sub == 0x1E


def test_iax_poke_against_a_local_pong():
    async def run():
        loop = asyncio.get_event_loop()

        class Pong(asyncio.DatagramProtocol):
            def connection_made(self, t):
                self.t = t

            def datagram_received(self, data, addr):
                self.t.sendto(data[:10] + bytes([6, 3]), addr)

        t, _ = await loop.create_datagram_endpoint(Pong, local_addr=("127.0.0.1", 0))
        old = mm.IAX_PORT
        mm.IAX_PORT = t.get_extra_info("sockname")[1]
        try:
            ms = await mm.iax_poke("127.0.0.1", timeout=1)
            mm.IAX_PORT = 1
            none = await mm.iax_poke("127.0.0.1", timeout=0.3, tries=1)
        finally:
            mm.IAX_PORT = old
            t.close()
        return ms, none
    ms, none = asyncio.run(run())
    assert ms is not None and none is None


def test_km_and_local_ip():
    assert 300 < mm.km(42.36, -71.06, 40.71, -74.01) < 320
    assert mm.is_local_ip("127.0.0.1") and not mm.is_local_ip("192.0.2.99")


def test_parse_offices_keeps_map_fields():
    o = parse_offices([{"name": "MILLTOWN", "latitude": "40.5", "longitude": "-75.5", "ip_address": "10.50.0.7",
                        "hostname": None, "pbx_type": "FreePBX", "network": "us", "admin_email": "x@example.org",
                        "admin_name": "Someone", "trunks": [], "office_codes": []}])[0]
    assert o["lat"] == 40.5 and o["ip"] == "10.50.0.7" and o["pbx_type"] == "FreePBX"
    assert o["admin_email"] == "x@example.org" and o["admin_name"] == "Someone"
    bad = parse_offices([{"name": "Q", "admin_email": "<script>@x", "trunks": [], "office_codes": []}])[0]
    assert bad["admin_email"] == ""


def test_node_location_needs_the_nodes_lan_to_hold_the_pbx():
    nodes = {**NODES, "10.3.0.1": {**NODES["10.3.0.1"], "lan": "10.99.0.1"}}   # grouping was wrong
    cands = {o["name"]: mm.office_candidates(o, HOSTS) for o in OFFICES}
    m = mm.build_map(OFFICES, HOSTS, SERVICES, cands, {}, {"10.50.0.7": 117.0}, nodes, set(), "HOMETOWN")
    mt = next(o for o in m["offices"] if o["name"] == "MILLTOWN")
    assert mt["loc_source"] == "directory" and mt["node"] == ""


def test_generic_directory_hostname_is_not_matched():
    o = {**OFFICES[3], "hostname": "freepbx"}
    hosts = HOSTS + [{"name": "freepbx", "ip": "10.90.0.5"}]
    assert "10.90.0.5" not in mm.office_candidates(o, hosts)
    o2 = {**OFFICES[3], "hostname": "n2aaa-box"}
    assert "10.91.0.5" in mm.office_candidates(o2, hosts + [{"name": "n2aaa-box", "ip": "10.91.0.5"}])


def test_allstar_only_node_is_not_added_as_a_meshphone_pbx():
    names = [o["name"] for o in _build()["offices"]]
    assert not any("allstar" in n.lower() for n in names)


def test_offices_sharing_one_pbx_are_hosted():
    offices = OFFICES + [_off("FARVILLE", "K1AAA", ip="10.50.0.7", lat=30.0, lon=-90.0, location="Farville, LA")]
    cands = {o["name"]: mm.office_candidates(o, HOSTS) for o in offices}
    m = mm.build_map(offices, HOSTS, SERVICES, cands, {}, {"10.50.0.7": 117.0}, NODES, set(), "HOMETOWN")
    f = next(o for o in m["offices"] if o["name"] == "FARVILLE")
    assert f["shares_pbx_with"] == ["MILLTOWN"] and f["hosted"] and f["place"] == "Milltown, RI"


TRACE = """traceroute to 10.30.0.44 (10.30.0.44), 20 hops max, 60 byte packets
 1  10.144.0.82  0.551 ms
 2  10.160.0.198  3.675 ms
 3  *
 4  10.2.0.1  223.283 ms
 5  10.30.0.44  223.766 ms
"""


def test_traceroute_last_hop_is_the_node():
    assert mm.parse_traceroute(TRACE, "10.30.0.44") == "10.2.0.1"
    assert mm.parse_traceroute(TRACE.rsplit("\n 5", 1)[0], "10.30.0.44") == ""     # never reached


def test_node_found_by_traceroute_when_the_host_list_is_wrong():
    nodes = {**NODES, "10.3.0.1": {**NODES["10.3.0.1"], "lan": "10.99.0.1"},          # grouping wrong
             "10.7.0.1": {"name": "K1AAA-REAL-NODE", "lan": "10.50.0.1", "lat": 41.9, "lon": -71.7,
                          "grid": "FN41", "place": "Realtown, RI"}}
    cands = {o["name"]: mm.office_candidates(o, HOSTS) for o in OFFICES}
    m = mm.build_map(OFFICES, HOSTS, SERVICES, cands, {}, {"10.50.0.7": 117.0}, nodes, set(), "HOMETOWN",
                     node_for={"10.50.0.7": "10.7.0.1"})
    mt = next(o for o in m["offices"] if o["name"] == "MILLTOWN")
    assert (mt["node"], mt["place"], mt["loc_source"]) == ("K1AAA-REAL-NODE", "Realtown, RI", "node")


def test_office_that_exists_on_the_mesh_but_is_silent_is_kept():
    hosts = HOSTS + [{"name": "N2AAA-NODE", "ip": "10.8.0.1"}, {"name": "n2aaa-pbx", "ip": "10.70.0.9"}]
    cands = {o["name"]: mm.office_candidates(o, hosts) for o in OFFICES}
    m = mm.build_map(OFFICES, hosts, SERVICES, cands, {}, {}, NODES, set(), "HOMETOWN")
    o = {x["name"]: x for x in m["offices"]}
    assert o["HUBTOWN"]["state"] == "no_answer"
    assert o["SIDETOWN"]["state"] == "no_answer"     # same admin: that PBX host is its too


def test_number_range():
    assert mm.number_range(["9371000", "9371012", "5551234"]) == "937-10xx"
    assert mm.number_range(["9371000"]) == ""          # one listing isn't a block
    assert mm.number_range(["bad", ""]) == ""


def test_people_get_admins_and_online_status():
    offices = [dict(OFFICES[0], admin_phone="5554101", admin_name="Op Erator", office_codes=["55541"]),
               dict(OFFICES[1], admin_phone="5089901", admin_name="Har Bor")]
    services = SERVICES + [{"name": "W1XYZ 508-9901 [phone]", "ip": "10.2.0.1", "link": "", "protocol": "tcp"}]
    cands = {o["name"]: mm.office_candidates(o, HOSTS) for o in offices}
    people = [{"callsign": "N0BBB", "name": "Bee", "number": "5554102", "pbx": "HOMETOWN"}]
    reg = mm.own_numbers([{"extension": "101", "online": True}, {"extension": "102", "online": False}], "55541")
    assert reg == {"5554101": True, "5554102": False}
    m = mm.build_map(offices, HOSTS, services, cands, {}, {"10.30.0.57": 1.0}, NODES, {"10.9.9.44"},
                     "HOMETOWN", people=people, own_registered=reg)
    p = {x["callsign"]: x for x in m["people"]}
    assert p["N0CALL"]["name"] == "Op Erator" and p["N0CALL"]["source"].startswith("directory")
    assert p["N0CALL"]["status"] == "online" and p["N0BBB"]["status"] == "offline"
    assert p["W1XYZ"]["status"] == "on_mesh"            # its phone is advertised on the mesh
    assert m["summary"]["people_online"] == 1 and m["summary"]["people_on_mesh"] == 1


def test_handsets_and_advertised_phone_addresses():
    hosts = HOSTS + [{"name": "N0BBB-phone", "ip": "10.9.9.50"}, {"name": "N0BBB-GXP2160", "ip": "10.9.9.51"},
                     {"name": "K1CCC-IPHONE", "ip": "10.9.9.52"}, {"name": "wt0f-meshphone", "ip": "10.9.9.53"},
                     {"name": "N9QQQ-SIP-T26P", "ip": "10.60.0.11"}]
    h = mm.handset_hosts(hosts)
    assert [p["ip"] for p in h["N0BBB"]] == ["10.9.9.50", "10.9.9.51"]
    assert h["K1AAA"][0]["name"] == "K1AAA-yealink-t46s-pbx"        # a handset, not the PBX
    assert "K1CCC" not in h and "WT0F" not in h and h["N9QQQ"][0]["ip"] == "10.60.0.11"
    svcs = [{"name": "N9QQQ 937-1001 [phone]", "link": "http://n9qqq-sip-t26p.local.mesh/"},
            {"name": "MeshPhone 937-1000 [phone]", "ip": "10.5.0.1", "link": ""}]
    assert mm.advertised_phone_ips(hosts, svcs) == {"9371001": "10.60.0.11"}


def test_dialplan_patterns_find_the_office():
    offs = [dict(_off("A", "N0A"), office_codes=["55541"]),
            dict(_off("B", "N0B"), dialplans=["_937[01]XXX"]), dict(_off("C", "N0C"), dialplans=["_NXX12XX"])]
    assert mm.office_for_number("5554199", offs)["name"] == "A"
    assert mm.office_for_number("9371042", offs)["name"] == "B"
    assert mm.office_for_number("9372042", offs) is None
    assert mm.office_for_number("2341299", offs)["name"] == "C"
    assert mm._dial_re("_9[a-z]X") is None and mm._dial_re("1234").fullmatch("1234")


def test_people_status_from_phones_and_their_pbx():
    offices = [dict(OFFICES[0], office_codes=["55541"]), dict(OFFICES[1], office_codes=["50899"]),
               dict(OFFICES[2], office_codes=["40144"])]
    hosts = HOSTS + [{"name": "W1QQQ-phone", "ip": "10.30.0.60"}, {"name": "W1RRR-phone", "ip": "10.30.0.61"}]
    cands = {o["name"]: mm.office_candidates(o, hosts) for o in offices}
    people = [{"callsign": "W1QQQ", "name": "Q", "number": "5089902", "pbx": "HARBOR"},
              {"callsign": "W1RRR", "name": "R", "number": "5089903", "pbx": "HARBOR"},
              {"callsign": "W1SSS", "name": "S", "number": "5089904", "pbx": "OLDHARBOR"},   # renamed office
              {"callsign": "K1TTT", "name": "T", "number": "4014405", "pbx": "MILLTOWN"},
              {"callsign": "K9ZZZ", "name": "Z", "number": "7770000", "pbx": "GONE"}]
    m = mm.build_map(offices, hosts, SERVICES, cands, {}, {"10.30.0.57": 1.0}, NODES, {"10.9.9.44"},
                     "HOMETOWN", people=people, sip_answers={"10.30.0.60": 42.0})
    p = {x["callsign"]: x for x in m["people"]}
    assert p["W1QQQ"]["status"] == "phone_up" and "42 ms" in p["W1QQQ"]["status_note"]
    assert p["W1RRR"]["status"] == "pbx_up" and "didn't answer a SIP ping" in p["W1RRR"]["status_note"]
    assert p["W1SSS"]["status"] == "pbx_up" and p["W1SSS"]["office"] == "HARBOR" and p["W1SSS"]["location"]
    assert p["K1TTT"]["status"] == "pbx_down"          # MILLTOWN doesn't answer
    assert p["K9ZZZ"]["status"] == ""
    assert m["summary"]["people_status"]["pbx_up"] == 2


def test_sip_ping_against_a_local_responder():
    async def run():
        loop = asyncio.get_event_loop()
        got = []

        class Uas(asyncio.DatagramProtocol):
            def connection_made(self, t):
                self.t = t

            def datagram_received(self, data, addr):
                got.append(data)
                self.t.sendto(b"SIP/2.0 200 OK\r\nContent-Length: 0\r\n\r\n", addr)

        t, _ = await loop.create_datagram_endpoint(Uas, local_addr=("127.0.0.1", 0))
        old = mm.SIP_PORT
        mm.SIP_PORT = t.get_extra_info("sockname")[1]
        try:
            ms = await mm.sip_ping("127.0.0.1", timeout=1)
        finally:
            mm.SIP_PORT = old
            t.close()
        return ms, got
    ms, got = asyncio.run(run())
    assert ms is not None and got[0].startswith(b"OPTIONS sip:127.0.0.1 SIP/2.0\r\nVia: SIP/2.0/UDP 127.0.0.1:")



TRUNK_TABLE = """<html><body><pre>Trunk report for MeshPhone Office 55501
Office CODEC County City Sysop Host Status Rating Ping
-----------------------------------------------------
 55502 g711 Bay Harbor Ann W1XYZ w1xyz-pbx OK Excellent 33 ms
 55503 g711 Kent Bay Bob KX6AAA kx6aaa-raspbx UNREACHABLE Down
</pre></body></html>"""


def test_parse_published_trunks_in_all_three_forms():
    j = mm.parse_published_trunks('{"ok": true, "data": [{"trunk_name": "w1xyz", "host": "10.30.0.57", "status": "OK", '
                                  '"latency_ms": 23}, {"trunk_name": "hoip", "host": "192.0.2.10", "status": "UNREACHABLE", '
                                  '"latency_ms": null}, {"trunk_name": "x", "host": "", "status": "UNKNOWN"}]}')
    assert [(p["host"], p["ok"], p["ms"]) for p in j["peers"]] == [("10.30.0.57", True, 23), ("192.0.2.10", False, None)]
    a = mm.parse_published_trunks("<pre>" + PEERS + "</pre>")
    assert ("10.20.0.2", True, 145) in [(p["host"], p["ok"], p["ms"]) for p in a["peers"]]
    assert not any(p["name"] == "lab-pi" for p in a["peers"])             # UNKNOWN: not monitored
    t = mm.parse_published_trunks(TRUNK_TABLE)
    assert t["code"] == "55501"
    assert [(p["code"], p["host"], p["ok"], p["ms"]) for p in t["peers"]] == [
        ("55502", "w1xyz-pbx", True, 33), ("55503", "kx6aaa-raspbx", False, None)]


def test_trunk_pages_found_by_name_or_path():
    svcs = [{"name": "PBX Trunk Status", "link": "http://k1aaa-pbx:80/iax_peers.txt"},
            {"name": "APISON PBX Trunk Status", "link": "http://k1aaa-pbx:80/iax/iax2_status_ami.php"},
            {"name": "PBX Line Report", "link": "http://k1aaa-pbx:80/cgi-bin/sip_peers"},      # handsets, not trunks
            {"name": "Trunk Report", "link": "http://192.0.2.5/x"},                            # not on the mesh
            {"name": "Club page", "link": "http://k1aaa-pbx:80/"}]
    pages = mm.trunk_pages(svcs, HOSTS)
    assert [p["url"] for p in pages] == ["http://10.50.0.7/iax_peers.txt", "http://10.50.0.7/iax/iax2_status_ami.php?json=1"]


def test_published_trunks_on_the_map():
    pg = {"ip": "10.50.0.7", "host": "k1aaa-pbx", "code": "", "at": "2026-11-25T12:00:00Z",
          "peers": [{"name": "", "host": "10.30.0.57", "code": "", "ok": True, "ms": 33},     # MILLTOWN -> HARBOR
                    {"name": "", "host": "kx6aaa-raspbx", "code": "", "ok": False, "ms": None},  # -> BAYTOWN
                    {"name": "hoip", "host": "192.0.2.10", "code": "", "ok": True, "ms": 70}]}
    reports = [{"by": "MILLTOWN", "to": "HARBOR", "ms": 400, "down": False}]                   # older directory report
    cands = {o["name"]: mm.office_candidates(o, HOSTS) for o in OFFICES}
    m = mm.build_map(OFFICES, HOSTS, SERVICES, cands, mm.mesh_candidates(HOSTS, SERVICES),
                     {"10.30.0.57": 120.0, "10.50.0.7": 117.0}, NODES, {"10.9.9.44"}, "HOMETOWN",
                     [], [], reports, generated_at=1.0, published=[pg])
    links = {frozenset((l["a"], l["b"])): l for l in m["links"]}
    hm = links[frozenset(("HARBOR", "MILLTOWN"))]
    assert hm["state"] == "reported_up" and hm["rtt_ms"] == 33                 # the live page wins
    assert hm["reports"][0]["source"] == "published" and len(hm["reports"]) == 1
    assert links[frozenset(("BAYTOWN", "MILLTOWN"))]["state"] == "reported_down"   # a trunk the directory lacks
    assert m["summary"]["trunk_pages"] == 1 and m["summary"]["published_reports"] == 2



def test_directory_follows_the_map_refresh():
    from ech.core.meshphone_directory import directory_refresh_s
    assert directory_refresh_s({}) == 23 * 3600
    assert directory_refresh_s({"radio_directory": {"meshphone": {"map_refresh_hours": 12}}}) == 11 * 3600
    assert directory_refresh_s({"radio_directory": {"meshphone": {"map_refresh_hours": 0.5}}}) == 3600
    assert directory_refresh_s({"radio_directory": {"meshphone": {"map_refresh_hours": "x"}}}) == 23 * 3600



def test_own_trunk_status_page_reads_back(monkeypatch):
    s = mm.MeshPhoneMap() if hasattr(mm, "MeshPhoneMap") else type(mm.service)()

    async def peers():
        return mm.parse_iax_peers(PEERS)
    monkeypatch.setattr(s, "_own_peers", peers)
    monkeypatch.setattr(s, "_cfg", lambda: {"own_office": "HOMETOWN"})
    d = asyncio.run(s.trunk_status())
    assert d["office"] == "HOMETOWN"
    assert [t["host"] for t in d["trunks"]] == ["10.20.0.2", "10.30.0.44", "10.40.0.5"]   # no internet trunk
    import json
    got = mm.parse_published_trunks(json.dumps(d))                   # what another copy reads
    assert [(p["host"], p["ok"], p["ms"]) for p in got["peers"]] == [
        ("10.20.0.2", True, 145), ("10.30.0.44", True, 194), ("10.40.0.5", False, None)]



def test_a_published_page_finds_an_office_whose_listed_address_is_stale():
    offices = [dict(_off("EASTLANE", "N7AAA", ip="10.99.0.1", trunks=["HARBOR"]), office_codes=["55501"],
                    hostname="lane-pbx.local.mesh"),
               dict(_off("JACKSON", "N7BBB", ip="10.99.0.2"), office_codes=["55503"], hostname="jackson-pbx.local.mesh"),
               dict(_off("HARBOR", "W1XYZ", ip="10.30.0.57"), office_codes=["55502"])]
    hosts = HOSTS + [{"name": "lane-pbx-new", "ip": "10.77.0.5"}, {"name": "jackson-pbx", "ip": "10.77.0.6"}]
    pages = [{"ip": "10.77.0.5", "host": "lane-pbx-new", "code": "55501",
              "peers": [{"host": "jackson-pbx", "code": "55503", "ok": True, "ms": 40},
                        {"host": "10.30.0.57", "code": "55502", "ok": True, "ms": 30},
                        {"host": "freepbx", "code": "55503", "ok": True, "ms": 1}]}]    # not its host name: ignored
    extra = mm.published_candidates(offices, pages, hosts)
    assert extra == {"EASTLANE": ["10.77.0.5"], "JACKSON": ["10.77.0.6"], "HARBOR": ["10.30.0.57"]}



def test_checkin_reports_listed_trunks_like_the_directory_script():
    peers = mm.parse_iax_peers(PEERS)
    names = {"10.20.0.2": "BAYTOWN", "10.30.0.44": "HARBOR", "10.40.0.5": "STORMTOWN", "192.0.2.10": "HOIP"}
    t = mm.checkin_trunks(peers, ["HARBOR", "STORMTOWN", "HOIP"], lambda p: names.get(p["host"]))
    # BAYTOWN isn't listed as our trunk; HOIP is over the internet; lab-pi is unmonitored
    assert t == [{"pbx_name": "HARBOR", "latency": "194"}, {"pbx_name": "STORMTOWN", "latency": "Unreachable"}]
    f = mm.checkin_form({"pbx_name": "HOMETOWN", "hostname": "n0call-pbx", "ip_address": "10.9.9.44",
                         "pbx_type": "Allstar", "gen_script_name": "SignalMatrix", "gen_script_version": "1.2.3",
                         "network": None, "trunks": t})
    assert f == {"pbx_name": "HOMETOWN", "hostname": "n0call-pbx", "ip_address": "10.9.9.44", "pbx_type": "Allstar",
                 "gen_script_name": "SignalMatrix", "gen_script_version": "1.2.3",
                 "trunks[0][pbx_name]": "HARBOR", "trunks[0][latency]": "194",
                 "trunks[1][pbx_name]": "STORMTOWN", "trunks[1][latency]": "Unreachable"}


def test_checkin_data_and_post(monkeypatch):
    import httpx
    from ech.core.directory_status import status as live
    s = type(mm.service)()
    s._config = {"radio_directory": {"meshphone": {"own_office": "HOMETOWN", "checkin": True,
                                                   "checkin_ip": "10.9.9.44", "dns_server": "none"}}}
    monkeypatch.setattr(live, "mp_offices", [dict(_off("HOMETOWN", "N0CALL", trunks=["HARBOR"]), hostname="n0call-pbx.local.mesh"),
                                             _off("HARBOR", "W1XYZ", ip="10.30.0.44")])

    async def peers():
        return mm.parse_iax_peers(PEERS)
    monkeypatch.setattr(s, "_own_peers", peers)
    sent = {}

    class Resp:
        status_code = 200

    class Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, data=None, headers=None, timeout=None):
            sent.update(url=url, data=data)
            return Resp()
    monkeypatch.setattr(httpx, "AsyncClient", Client)
    from ech.core import meshphone_directory as mpd
    monkeypatch.setattr(mpd, "resolve_url", lambda url, dns: (url, {}))
    r = asyncio.run(s.checkin())
    assert r["ok"] and r["trunks"] == ["HARBOR"]
    assert sent["url"].endswith("/api/checkin")
    d = sent["data"]
    assert (d["pbx_name"], d["hostname"], d["ip_address"], d["gen_script_name"]) == \
        ("HOMETOWN", "n0call-pbx", "10.9.9.44", "SignalMatrix")
    assert d["trunks[0][pbx_name]"] == "HARBOR" and d["trunks[0][latency]"] == "194"
    assert not any("secret" in k.lower() or "pass" in k.lower() for k in d)
