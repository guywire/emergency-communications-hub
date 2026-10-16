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
