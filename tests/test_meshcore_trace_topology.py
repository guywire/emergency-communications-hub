"""
tests/test_meshcore_trace_topology.py
-------------------------------------
Live report (2026-10-11): many traces, one reply. Topology "heard direct"
links and adjacencies were kept forever (persisted, never expired) and
other observers' paths would have been marked direct-to-us. Now: links
age out, observer paths add adjacencies only, observer "," paths parse,
and an unanswered trace posts a "no reply" result instead of vanishing.
"""

import asyncio
import collections
import json
import time

import pytest

from ech.adapters.meshcore import MeshCoreAdapter


def _adapter(**cfg):
    return MeshCoreAdapter({"name": "mc-test", "transport": "serial", "port": "/dev/null", **cfg})


def test_direct_links_and_edges_expire():
    a = _adapter(topology_direct_ttl_hours=1, topology_edge_ttl_hours=2)
    a._learn_topology(["aa", "bb", "cc"])              # we heard cc direct; aa-bb-cc adjacent
    assert a._infer_route("aa") == ["cc", "bb"]
    old = time.time() - 1.5 * 3600
    a._direct_seen["cc"] = old                          # direct link 90 min old (TTL 1 h)
    assert a._infer_route("aa") is None                 # no longer trusted
    a._learn_topology(["dd", "cc"])                    # heard cc direct again just now
    assert a._infer_route("aa") == ["cc", "bb"]         # edges still within 2 h TTL
    for e in list(a._edge_seen):
        a._edge_seen[e] = time.time() - 3 * 3600        # all edges 3 h old
    assert a._infer_route("aa") is None


def test_observer_paths_never_mark_direct_to_us():
    a = _adapter()
    a._learn_topology(["aa", "bb", "cc"], observer=True)
    assert a._direct_hashes == set()
    assert "bb" in a._rf_adjacency["cc"]


def test_old_undated_topology_is_discarded_on_restore():
    a = _adapter()

    class DB:
        async def get_kv(self, k):
            return json.dumps({"direct": ["aa", "bb"], "adj": {"aa": ["bb"], "bb": ["aa"]}})

    a._db = DB()
    asyncio.run(a._restore_topology())
    assert a._direct_hashes == set() and a._rf_adjacency == {}


def test_dated_topology_round_trips():
    a = _adapter()
    a._learn_topology(["aa", "bb"])
    saved = {}

    class DB:
        async def set_kv(self, k, v):
            saved["v"] = v

        async def get_kv(self, k):
            return saved["v"]

    a._db = DB()
    asyncio.run(a._persist_topology())
    b = _adapter()
    b._db = DB()
    asyncio.run(b._restore_topology())
    assert b._direct_hashes == {"bb"} and b._rf_adjacency["aa"] == {"bb"}


def test_unanswered_trace_posts_no_reply_result():
    a = _adapter()
    a._direct_seen["c2"] = time.time() - 600
    a._trace_ctx[0xABC] = {"target": "LandOfLucy", "route": "LandOfLucy", "route_src": "heard direct",
                           "hops": 1, "first_hop_seen": a._direct_seen["c2"], "target_hash": "c2"}
    a._pending_trace_tags.add(0xABC)
    asyncio.run(a._expire_pending_tag(0xABC, 0))
    msg = a._rx_queue.get_nowait()
    assert "no reply from LandOfLucy" in msg.body and "10 min ago" in msg.body
    assert msg.raw["trace_result"] == "no_reply" and 0xABC not in a._pending_trace_tags


def test_answered_trace_posts_nothing_extra():
    a = _adapter()
    a._trace_ctx[0x1] = {"target": "RKDRPTMON"}
    asyncio.run(a._expire_pending_tag(0x1, 0))          # tag not pending → reply already arrived
    assert a._rx_queue.empty()


def test_compare_adapter_parses_comma_paths_as_observer():
    from ech.adapters.letsmesh_compare import LetsMeshCompareAdapter
    mc = _adapter()
    cmp_ = LetsMeshCompareAdapter({"name": "observers-compare", "host": "127.0.0.1", "meshcore_adapter": "mc-test"})
    cmp_._get_sibling_adapter = lambda name: mc
    payload = json.dumps({"type": "PACKET", "origin_id": "AB" * 32, "path": "c2,e2,7a"}).encode()
    asyncio.run(cmp_._handle_message("meshcore/RKD/x/packets", payload))
    assert cmp_._paths_merged == 1
    assert mc._direct_hashes == set() and mc._rf_adjacency["e2"] == {"c2", "7a"}


def test_trace_uses_path_discovery_before_guessing():
    """Field report (2026-10-16): multi-hop traces went out along routes guessed
    from overheard traffic ("0f? -> Brunswick-RPT-1 -> 0f?") and never came back.
    With no stored route, ping() now asks the mesh for the real one (CMD 0x34,
    as the official apps do) and builds the trace from the 0x8D answer."""
    from ech.core.models import MeshNode
    a = _adapter(path_discovery_timeout=2)
    a._connected = True
    key = "C2" + "11" * 31
    a._nodes[key[:12]] = MeshNode(node_id=key[:12], display_name="LandOfLucy")
    a._nodes[key[:12]].meta.update({"pubkey": key, "out_path": ""})
    sent = []

    async def fake_send(payload):
        sent.append(payload)
        if payload[0] == 0x34:   # answer the discovery: out path 15 -> 8b
            assert payload[1:] == b"\x00" + bytes.fromhex(key)
            reply = bytes([0x8D, 0]) + bytes.fromhex(key[:12]) + bytes([2, 0x15, 0x8B, 0])
            asyncio.get_running_loop().call_soon(lambda: asyncio.ensure_future(a._dispatch_frame(reply)))

    a._send_cmd = fake_send
    asyncio.run(a.ping(key[:12]))
    trace = sent[-1]
    assert trace[0] == 0x24
    assert trace[10:] == bytes([0x15, 0x8B, 0xC2, 0x8B, 0x15])   # out via relays, back the same way
    assert a._nodes[key[:12]].meta["out_path"] == "158b"           # remembered for next time


def test_trace_falls_back_when_discovery_gets_no_answer():
    from ech.core.models import MeshNode
    a = _adapter(path_discovery_timeout=0.05)
    a._connected = True
    key = "C2" + "22" * 31
    a._nodes[key[:12]] = MeshNode(node_id=key[:12], display_name="Quiet")
    a._nodes[key[:12]].meta.update({"pubkey": key, "out_path": ""})
    sent = []

    async def fake_send(payload):
        sent.append(payload)

    a._send_cmd = fake_send
    asyncio.run(a.ping(key[:12]))
    assert sent[0][0] == 0x34 and sent[-1][0] == 0x24


def test_inferred_route_rejects_far_hash_collisions_and_long_chains():
    """Field report (2026-10-16): guessed trace routes ran through a Boston
    repeater sharing a 1-byte hash with a local one, and 9-hop round trips."""
    from ech.core.models import MeshNode
    a = _adapter()
    a._base_lat, a._base_lon = 44.16, -69.12                     # Rockland, ME
    for nid, lat, lon in (("AA0000000001", 44.10, -69.10),       # local direct repeater
                          ("BB0000000001", 42.33, -71.08),       # "bb" only known in Boston
                          ("CC0000000001", 44.30, -69.00)):      # target near us
        a._nodes[nid] = MeshNode(node_id=nid, display_name=nid)
        a._nodes[nid].lat, a._nodes[nid].lon = lat, lon
    a._learn_topology(["cc", "bb", "aa"])                        # cc-bb-aa adjacency, aa heard direct
    assert a._infer_route("cc") is None                          # aa -> Boston -> cc is impossible
    a._nodes["BB0000000001"].lat, a._nodes["BB0000000001"].lon = 44.2, -69.05
    assert a._infer_route("cc") == ["aa", "bb"]                  # same hops, plausible positions
    a._learn_topology(["ee", "dd", "cc", "bb", "aa"])
    assert a._infer_route("ee") is None                          # would need 3 relays (cap is 2)
    assert a._infer_route("ee", max_hops=4) == ["aa", "bb", "cc", "dd"]


def _sweep_adapter(answers):
    """Adapter whose probes answer according to ``answers``: tuple(outbound) -> bool."""
    from ech.core.models import MeshNode
    a = _adapter()
    a._connected = True
    a._base_lat, a._base_lon = 44.16, -69.12
    for nid, name in (("15AAAAAAAAAA", "RKDRPTMON"), ("6BAAAAAAAAAA", "LandOfLucy"),
                      ("C2AAAAAAAAAA", "Ragged"), ("E7AAAAAAAAAA", "SouthThomaston")):
        a._nodes[nid] = MeshNode(node_id=nid, display_name=name)
        a._nodes[nid].lat, a._nodes[nid].lon = 44.15, -69.1
    probed = []

    async def fake_probe(outbound, timeout, last_full=None):
        probed.append(tuple(outbound))
        ok = answers.get(tuple(outbound), False)
        return {"ok": ok, "hops": 2 * len(outbound) - 1, "snrs": [8.5] * (2 * len(outbound)),
                "heard": 0 if ok else 1}

    a._probe = fake_probe
    return a, probed


def test_sweep_walks_hop_by_hop_and_names_the_break():
    a, probed = _sweep_adapter({("15",): True})
    a._learn_topology(["e7", "6b", "15"])           # e7-6b-15, 15 heard direct
    out = asyncio.run(a.trace_sweep("E7AAAAAAAAAA", gap=0))
    assert probed == [("15",), ("15", "6b")]          # stops at the first failing hop
    assert out["reached"] is False
    assert "✓ us → RKDRPTMON" in out["summary"] and "✗ RKDRPTMON → LandOfLucy" in out["summary"]
    assert "we heard it get 1 of 3 hops" in out["summary"]
    assert a._rx_queue.get_nowait().raw["trace_result"] == "sweep"


def test_sweep_tries_next_route_and_reuses_proven_hops():
    a, probed = _sweep_adapter({("15",): True, ("15", "c2"): True, ("15", "c2", "e7"): True})
    a._learn_topology(["e7", "6b", "15"])
    a._learn_topology(["e7", "c2", "15"])
    out = asyncio.run(a.trace_sweep("E7AAAAAAAAAA", gap=0))
    assert out["reached"] is True
    assert probed.count(("15",)) == 1                 # first hop probed once, reused
    assert ("15", "c2", "e7") in probed and "REACHED" in out["summary"]


def test_sweep_respects_probe_limit_and_single_run():
    a, probed = _sweep_adapter({})
    a._learn_topology(["e7", "6b", "15"])
    out = asyncio.run(a.trace_sweep("E7AAAAAAAAAA", max_probes=1, gap=0))
    assert len(probed) == 1 and out["probes"] == 1
    a._sweep_running = True
    assert asyncio.run(a.trace_sweep("E7AAAAAAAAAA", gap=0))["status"] == "error"


def test_probe_resolves_on_trace_data_and_stays_quiet():
    a = _adapter()
    a._connected = True

    async def fake_send(payload):
        assert payload[0] == 0x24 and payload[10:] == bytes([0x15, 0x6B, 0x15])
        tag = payload[1:5]
        reply = (bytes([0x89, 0, 3, 0]) + tag + b"\x00" * 4 + bytes([0x15, 0x6B, 0x15])
                 + bytes([34, 20, 30, 40]))
        asyncio.get_running_loop().call_soon(lambda: asyncio.ensure_future(a._dispatch_frame(reply)))

    a._send_cmd = fake_send
    res = asyncio.run(a._probe(["15", "6b"], timeout=2))
    assert res["ok"] and res["snrs"][0] == 8.5
    assert a._rx_queue.empty()                       # no per-probe feed message
    assert not a._pending_trace_tags and not a._quiet_tags


def test_mixed_width_hashes_are_one_repeater():
    """2026-10-16: the mesh mixes 1/2/3-byte path hashes; "15", "1551" and
    "1551f6" were stored as three repeaters, splitting the graph."""
    a = _adapter()
    a._learn_topology(["6b0896", "1551f6"])
    a._learn_topology(["c2", "6b"])
    assert a._direct_hashes == {"15", "6b"}               # last hop of each chain
    assert a._rf_adjacency["6b"] == {"15", "c2"}
    saved = {}

    class DB:
        async def get_kv(self, k):
            return json.dumps({"v": 2, "direct_seen": {"1551": time.time(), "15": time.time() - 60},
                               "edges": [["1551", "6b0896", time.time()], ["15", "6b", time.time() - 5]]})

    b = _adapter()
    b._db = DB()
    asyncio.run(b._restore_topology())
    assert b._direct_hashes == {"15"} and b._rf_adjacency["15"] == {"6b"}


def test_one_way_link_is_called_out():
    from ech.core.models import MeshNode
    a = _adapter()
    a._nodes["6B0896049993"] = MeshNode(node_id="6B0896049993", display_name="LandOfLucy")
    now = time.time()
    a._rx_link["6b"] = collections.deque([(now - 60, 4.5), (now - 30, 5.0), (now, 4.0)])
    hint = a._one_way_hint("6b")
    assert "one-way link" in hint and "LandOfLucy" in hint and "+4.5 dB" in hint
    a._tx_answered["6b"] = now
    assert a._one_way_hint("6b") == ""                  # it has answered us: not one-way
    assert "out of range" in a._one_way_hint("e7")


def _wide_adapter():
    from ech.core.models import MeshNode
    a = _adapter()
    a._connected = True
    for nid, name, typ in (("1551F6BB53BC", "RKDRPTMON", "REP"), ("15AA00000000", "SWANS-EAST", "REP"),
                           ("6B0896049993", "LandOfLucy", "REP"), ("6BFF00000000", "NH node", "REP"),
                           ("E7E22EB637A3", "South Thomaston", "REP")):
        a._nodes[nid] = MeshNode(node_id=nid, display_name=name)
        a._nodes[nid].meta["node_type"] = typ
    return a


def test_trace_uses_two_byte_hashes_when_every_hop_resolves():
    """Operator 2026-10-16: use the longer repeater addresses to avoid
    same-ID duplicates. Long hashes seen in traffic pin each hop."""
    a = _wide_adapter()
    a._learn_topology(["e7", "6b0896", "1551f6"])      # adverts relayed with 3-byte hashes
    sent = []

    async def fake_send(payload):
        sent.append(payload)

    a._send_cmd = fake_send
    a.config["path_discovery_timeout"] = 0.01
    asyncio.run(a.ping("E7E22EB637A3"))
    trace = sent[-1]
    assert trace[0] == 0x24 and trace[9] == 1                       # flags: 2-byte hashes
    assert trace[10:] == bytes.fromhex("1551" "6b08" "e7e2" "6b08" "1551")


def test_unknown_repeater_sharing_a_byte_keeps_one_byte_hashes():
    a = _wide_adapter()
    a._learn_topology(["e7", "6b0896", "1551f6"])
    a._learn_topology(["15cc11", "6b"])                # a long "15" hash matching no known node
    assert a._resolve_hop("15") is None
    assert a._resolve_hop("6b") == "6B0896049993"
    assert a._widen(["15", "6b", "e7"], last_full="E7E22EB637A3") is None


def test_link_evidence_survives_restart():
    a = _adapter()
    a._learn_topology(["6b0896", "1551f6"])
    a._rx_link["6b"] = collections.deque([(time.time(), 4.5)])
    a._tx_answered["15"] = time.time()
    saved = {}

    class DB:
        async def set_kv(self, k, v):
            saved["v"] = v

        async def get_kv(self, k):
            return saved["v"]

    a._db = DB()
    asyncio.run(a._persist_topology())
    b = _adapter()
    b._db = DB()
    asyncio.run(b._restore_topology())
    assert list(b._rx_link["6b"])[0][1] == 4.5 and "15" in b._tx_answered
    assert "1551f6" in b._long_seen["15"]


def test_observer_heard_trace_counts_as_sweep_progress():
    from ech.adapters.meshcore import MeshCoreAdapter
    a = MeshCoreAdapter({"name": "mc", "port": "/dev/null"})
    tag = 0xA53C4FC4
    a._pending_trace_tags.add(tag)
    # header 0x26 (direct, TRACE), path_len 1 (one SNR byte), tag, auth, flags, hash
    raw = bytes([0x26, 0x01, 0x2D]) + tag.to_bytes(4, "little") + bytes(4) + b"\x01" + bytes.fromhex("1551")
    assert a.note_observer_trace(raw, "MQTT Listener")
    assert a._trace_progress[tag] == 1
    assert not a.note_observer_trace(bytes([0x26, 0x00]) + (123).to_bytes(4, "little") + bytes(5))
    assert not a.note_observer_trace(bytes([0x12, 0x00]) + bytes(40))      # an advert


def test_discovery_pulse_adverts_hourly_not_every_pulse(monkeypatch):
    import asyncio
    import ech.adapters.meshcore as mod
    from ech.adapters.meshcore import MeshCoreAdapter, CMD_SEND_ADVERT
    a = MeshCoreAdapter({"name": "mc", "port": "/dev/null"})
    a._connected = True
    sent = []

    async def cmd(b):
        sent.append(b[0])

    async def nosleep(_):
        pass

    a._send_cmd = cmd
    monkeypatch.setattr(mod.asyncio, "sleep", nosleep)
    clock = [1000.0]
    monkeypatch.setattr(mod.time, "monotonic", lambda: clock[0])
    adverts = lambda: sent.count(CMD_SEND_ADVERT)
    asyncio.run(a._discovery_pulse())
    assert adverts() == 1                      # first pulse adverts
    clock[0] += 300
    asyncio.run(a._discovery_pulse())
    assert adverts() == 1                      # 5 min later: contacts only
    clock[0] += 3300
    asyncio.run(a._discovery_pulse())
    assert adverts() == 2                      # an hour after the last advert
    asyncio.run(a.announce())
    assert adverts() == 3                      # manual Announce: exactly one more


def test_manual_path_starting_at_our_own_node_drops_it():
    """Field 2026-10-16 (tag b4cfd7c5): a map-drawn path that began at ECH's
    own node went out as [C6, C2, 4E, C2, C6] and died at 0 hops."""
    from ech.core.models import MeshNode
    a = _wide_adapter()
    a.config["trace_hash_bytes"] = "1"
    a._self_node_id = "C65FDBF8C77E"
    for nid, name in (("C65FDBF8C77E", "SignalMatrix"), ("C29986EE51CC", "Ragged-RPT-1"),
                      ("4E982D8E2B12", "WRKD-RPT-02")):
        a._nodes[nid] = MeshNode(node_id=nid, display_name=name)
    sent = []

    async def fake_send(payload):
        sent.append(payload)

    a._send_cmd = fake_send
    asyncio.run(a.ping("4E982D8E2B12", via=["C65FDBF8C77E", "C29986EE51CC"]))
    assert sent[-1][10:] == bytes.fromhex("c2" "4e" "c2")
