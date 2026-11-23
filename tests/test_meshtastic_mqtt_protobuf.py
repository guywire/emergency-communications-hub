"""Meshtastic MQTT protobuf uplinks: decrypt, dedup across gateways, node learning."""
import asyncio
import json

import pytest

pytest.importorskip("meshtastic.protobuf.mqtt_pb2")
pytest.importorskip("aiomqtt")

from meshtastic.protobuf import mesh_pb2, mqtt_pb2, portnums_pb2  # noqa: E402

from ech.adapters import meshtastic_mqtt_codec as codec  # noqa: E402
from ech.adapters.mqtt_adapter import MQTTAdapter  # noqa: E402
from ech.core.models import NormalizedMessage  # noqa: E402

TOPIC = "msh/US/ME/2/e/LongFast/!gw000001"


def _envelope(portnum, payload: bytes, *, from_num=0x1234ABCD, to=0xFFFFFFFF, pid=4242,
              channel="LongFast", gateway="!gw000001", key=codec.DEFAULT_PSK, encrypt=True) -> bytes:
    data = mesh_pb2.Data(portnum=portnum, payload=payload)
    pkt = mesh_pb2.MeshPacket(to=to, id=pid, hop_start=3, hop_limit=1)
    setattr(pkt, "from", from_num)
    if encrypt:
        nonce = pid.to_bytes(8, "little") + from_num.to_bytes(4, "little") + b"\x00" * 4
        pkt.encrypted = codec._aes_ctr(key, nonce, data.SerializeToString())  # CTR: enc == dec
    else:
        pkt.decoded.CopyFrom(data)
    return mqtt_pb2.ServiceEnvelope(packet=pkt, channel_id=channel, gateway_id=gateway).SerializeToString()


def _adapter(**cfg):
    a = MQTTAdapter({"name": "meshtastic-mqtt-me", "host": "x", **cfg})
    out = []

    async def grab(msg):
        out.append(msg)
    a._enqueue = grab
    return a, out


def test_expand_psk_rules():
    assert codec.expand_psk("AQ==") == codec.DEFAULT_PSK
    assert codec.expand_psk("Ag==")[-1] == codec.DEFAULT_PSK[-1] + 1
    assert codec.expand_psk("AA==") is None
    assert codec.expand_psk("00" * 16) == bytes(16)
    with pytest.raises(ValueError):
        codec.expand_psk("AAAA")  # 3 bytes


def test_encrypted_text_becomes_chat_message():
    a, out = _adapter()
    payload = _envelope(portnums_pb2.TEXT_MESSAGE_APP, b"hello maine")
    asyncio.run(a._handle_message(TOPIC, payload))
    assert len(out) == 1
    m = out[0]
    assert m.body == "hello maine"
    assert m.source_channel == "LongFast"
    assert m.from_id == "!1234abcd"
    assert m.via_mqtt is True
    assert m.hop_count == 2
    assert m.raw["gateway"] == "!gw000001"


def test_same_packet_from_two_gateways_is_deduped():
    a, out = _adapter()
    p1 = _envelope(portnums_pb2.TEXT_MESSAGE_APP, b"once", gateway="!gw000001")
    p2 = _envelope(portnums_pb2.TEXT_MESSAGE_APP, b"once", gateway="!gw000002")
    asyncio.run(a._handle_message(TOPIC, p1))
    asyncio.run(a._handle_message("msh/US/ME/2/e/LongFast/!gw000002", p2))
    assert len(out) == 1
    assert a._mt_stats["duplicates"] == 1


def test_nodeinfo_names_later_messages_and_nodes_list():
    a, out = _adapter()
    user = mesh_pb2.User(id="!1234abcd", long_name="Portland Base", short_name="PB")
    asyncio.run(a._handle_message(TOPIC, _envelope(portnums_pb2.NODEINFO_APP, user.SerializeToString(), pid=1)))
    pos = mesh_pb2.Position(latitude_i=436614000, longitude_i=-702553000)
    asyncio.run(a._handle_message(TOPIC, _envelope(portnums_pb2.POSITION_APP, pos.SerializeToString(), pid=2)))
    asyncio.run(a._handle_message(TOPIC, _envelope(portnums_pb2.TEXT_MESSAGE_APP, b"hi", pid=3)))
    assert out[0].from_display == "Portland Base"
    nodes = asyncio.run(a.nodes())
    assert len(nodes) == 1 and nodes[0].short_name == "PB"
    assert round(nodes[0].lat, 3) == 43.661


def test_wrong_key_and_binary_noise_do_not_enqueue():
    a, out = _adapter(channel_keys={"LongFast": "Ag=="}, fallback_keys=[])
    asyncio.run(a._handle_message(TOPIC, _envelope(portnums_pb2.TEXT_MESSAGE_APP, b"secret")))
    asyncio.run(a._handle_message("msh/US/ME/2/map/", b"\x08\x96\x01\xff\xfe"))
    asyncio.run(a._handle_message("msh/US/ME/2/stat/!gw000001", b"online"))
    asyncio.run(a._handle_message("some/other/topic", b"\xff\xfe\x00binary"))
    assert out == []
    assert a._mt_stats["undecryptable"] == 2   # bad key + junk map report


def test_unencrypted_dm_and_custom_channel_key():
    key = "ABEiM0RVZneImaq7zN3u/w=="  # 16-byte custom PSK
    a, out = _adapter(channel_keys={"PrivateCh": key})
    raw = codec.expand_psk(key)
    asyncio.run(a._handle_message("msh/US/ME/2/e/PrivateCh/!gw",
                                  _envelope(portnums_pb2.TEXT_MESSAGE_APP, b"custom", channel="PrivateCh", key=raw)))
    me = a._mt_node_num
    asyncio.run(a._handle_message("msh/US/ME/2/e/LongFast/!gw",
                                  _envelope(portnums_pb2.TEXT_MESSAGE_APP, b"psst", to=me, pid=7, encrypt=False)))
    # A DM between two OTHER nodes goes to its own channel, addressed to its recipient
    asyncio.run(a._handle_message("msh/US/ME/2/e/LongFast/!gw",
                                  _envelope(portnums_pb2.TEXT_MESSAGE_APP, b"not ours", to=0x0BADBEEF, pid=8, encrypt=False)))
    assert [m.body for m in out] == ["custom", "[DM] psst", "[to !0badbeef] not ours"]
    assert out[2].source_channel == "Other DMs" and out[2].raw["channel_idx"] is None
    assert out[0].source_channel == "PrivateCh"
    assert out[1].source_channel == "DM" and out[1].to_id == codec.node_id_str(me)
    assert a._mt_stats["others_dm"] == 1


# -- channels, publish, own-echo -------------------------------------------

class _FakeClient:
    def __init__(self):
        self.published = []

    async def publish(self, topic, payload=None, qos=0):
        self.published.append((topic, payload))


def _pub_adapter(**cfg):
    a, out = _adapter(meshtastic_publish=True, node_id="!ec000001",
                      channel_keys={"LongFast": "AQ==", "MaineNet": "Vg=="}, **cfg)
    a._client = _FakeClient()
    return a, out


def test_longfast_channel_hash_is_8():
    assert codec.channel_hash("LongFast", codec.expand_psk("AQ==")) == 8


def test_channels_list_and_rx_channel_idx():
    a, out = _pub_adapter()
    asyncio.run(a._handle_message("msh/US/2/e/MaineNet/!gw",
                                  _envelope(portnums_pb2.TEXT_MESSAGE_APP, b"maine",
                                            channel="MaineNet", key=codec.expand_psk("Vg=="))))
    assert out[0].source_channel == "MaineNet" and out[0].raw["channel_idx"] == 1
    h = a._health_detail()
    assert h["channels_list"] == [{"idx": 0, "name": "LongFast"}, {"idx": 1, "name": "MaineNet"}]
    assert h["meshtastic"]["channels"]["MaineNet"]["roots"] == ["msh/US"]


def test_publish_roundtrips_through_decoder_on_every_seen_root():
    a, _ = _pub_adapter()
    a._mt_roots["MaineNet"] = {"msh/US", "msh/US/ME"}
    msg = NormalizedMessage(source_adapter="x", source_channel="outbound", from_id="local",
                            from_display="op", body="hello MaineNet", raw={"channel_name": "#MaineNet"})
    assert asyncio.run(a.send(msg)) is True
    topics = sorted(t for t, _ in a._client.published)
    assert topics == ["msh/US/2/e/MaineNet/!ec000001", "msh/US/ME/2/e/MaineNet/!ec000001"]
    pkt = codec.decode_envelope(a._client.published[0][1], {"MaineNet": codec.expand_psk("Vg==")})
    assert pkt["payload"] == b"hello MaineNet" and pkt["from_id"] == "!ec000001"
    assert pkt["decoded"].bitfield & 1               # ok_to_mqtt
    assert msg.source_channel == "MaineNet"


def test_publish_by_index_dm_and_failures():
    a, _ = _pub_adapter(allow_dm=True)   # DMs are opt-in
    dm = NormalizedMessage(source_adapter="x", source_channel="outbound", from_id="local",
                           from_display="op", body="hi", to_id="!0badbeef", raw={"channel_idx": 0})
    assert asyncio.run(a.send(dm)) is True
    pkt = codec.decode_envelope(a._client.published[-1][1], {"LongFast": codec.DEFAULT_PSK})
    assert pkt["to_id"] == "!0badbeef" and dm.source_channel == "DM"
    bad = NormalizedMessage(source_adapter="x", source_channel="o", from_id="local",
                            from_display="op", body="x", raw={"channel_idx": 9})
    assert asyncio.run(a.send(bad)) is False
    a._client = None
    assert asyncio.run(a.send(NormalizedMessage(source_adapter="x", source_channel="o", from_id="local",
                                                from_display="op", body="x"))) is False


def test_own_packet_echo_is_not_shown_but_rf_rehear_is_counted():
    a, out = _pub_adapter()
    mine = 0xEC000001
    asyncio.run(a._handle_message(TOPIC, _envelope(portnums_pb2.TEXT_MESSAGE_APP, b"me",
                                                   from_num=mine, gateway="!ec000001", pid=11)))
    asyncio.run(a._handle_message(TOPIC, _envelope(portnums_pb2.TEXT_MESSAGE_APP, b"me",
                                                   from_num=mine, gateway="!gw999999", pid=11)))
    assert out == []
    assert a._mt_stats["own_heard"] == 1


def test_receive_only_send_still_fails_cleanly():
    a, _ = _adapter()
    assert asyncio.run(a.send(NormalizedMessage(source_adapter="x", source_channel="o", from_id="local",
                                                from_display="op", body="x"))) is False


# -- node names: map reports + persistence ---------------------------------

class _FakeDB:
    def __init__(self):
        self.rows = {}

    async def upsert_mesh_node(self, adapter, node):
        self.rows[(adapter, node.node_id)] = {
            "node_id": node.node_id, "display_name": node.display_name, "lat": node.lat, "lon": node.lon,
            "last_heard": node.last_heard.isoformat() if node.last_heard else None,
            "meta_json": json.dumps(node.meta)}

    async def get_mesh_nodes(self, adapter):
        return [r for (a, _), r in self.rows.items() if a == adapter]


def test_map_report_names_node():
    a, out = _adapter()
    rep = mqtt_pb2.MapReport(long_name="Cadillac Mtn Router", short_name="CMR", role=2,
                             latitude_i=443526000, longitude_i=-682250000)
    env = _envelope(portnums_pb2.MAP_REPORT_APP, rep.SerializeToString(), channel="", encrypt=False)
    asyncio.run(a._handle_message("msh/US/ME/2/map/", env))
    node = asyncio.run(a.nodes())[0]
    assert node.display_name == "Cadillac Mtn Router" and node.short_name == "CMR"
    assert node.name_source == "map_report" and round(node.lat, 2) == 44.35
    assert out == []


def test_names_survive_restart():
    db = _FakeDB()

    async def run():
        a, _ = _adapter()
        a._db = db
        user = mesh_pb2.User(id="!1234abcd", long_name="Portland Base", short_name="PB")
        await a._handle_message(TOPIC, _envelope(portnums_pb2.NODEINFO_APP, user.SerializeToString()))
        await asyncio.sleep(0)                        # let the persist task run
        b, out = _adapter()
        b._db = db
        await b._mt_load_nodes()
        await b._handle_message(TOPIC, _envelope(portnums_pb2.TEXT_MESSAGE_APP, b"back again", pid=99))
        return b, out
    b, out = asyncio.run(run())
    assert out[0].from_display == "Portland Base"
    assert asyncio.run(b.nodes())[0].short_name == "PB"


def _named(name, **cfg):
    a = MQTTAdapter({"name": name, "host": "x", **cfg})
    out = []

    async def grab(msg):
        out.append(msg)
    a._enqueue = grab
    return a, out


def test_public_broker_defers_to_main_broker_copy():
    main, main_out = _named("meshtastic-mqtt-me")
    pub, pub_out = _named("meshtastic-mqtt-public", dedup_with=["meshtastic-mqtt-me"], dedup_delay=0.05)

    async def go():
        # Public copy arrives FIRST; the main broker's copy lands during the hold.
        await pub._handle_message("msh/US/ME/2/e/LongFast/!gw2", _envelope(portnums_pb2.TEXT_MESSAGE_APP, b"same", pid=77))
        await main._handle_message(TOPIC, _envelope(portnums_pb2.TEXT_MESSAGE_APP, b"same", pid=77))
        # A text only the public broker carried still comes through.
        await pub._handle_message("msh/US/ME/2/e/LongFast/!gw2", _envelope(portnums_pb2.TEXT_MESSAGE_APP, b"only public", pid=78))
        await asyncio.sleep(0.2)
    asyncio.run(go())
    assert [m.body for m in main_out] == ["same"]
    assert [m.body for m in pub_out] == ["only public"]
    assert pub._mt_stats["cross_duplicates"] == 1


def test_public_broker_ignores_main_adapters_own_posts():
    main, _ = _named("meshtastic-mqtt-me", meshtastic_publish=True)
    pub, pub_out = _named("meshtastic-mqtt-public", dedup_with=["meshtastic-mqtt-me"], dedup_delay=0)
    env = _envelope(portnums_pb2.TEXT_MESSAGE_APP, b"from ECH", from_num=main._mt_node_num, pid=99)
    asyncio.run(pub._handle_message("msh/US/ME/2/e/LongFast/!gw2", env))
    assert pub_out == []


def test_dm_over_mqtt_is_refused_with_reason():
    a, _ = _pub_adapter()
    msg = NormalizedMessage(source_adapter="", source_channel="", from_id="local", from_display="op",
                            body="hi", to_id="!1234abcd")
    assert asyncio.run(a.send(msg)) is False
    assert "DM" in (a.last_send_error or "")
    assert a._client.published == []


def test_bridged_prefixed_topics_decode_and_tag_source():
    # A local broker re-homes each upstream broker under its own prefix.
    a, out = _adapter(topics=["mt/state/#", "mt/public/#"])
    env = _envelope(portnums_pb2.TEXT_MESSAGE_APP, b"via bridge", pid=501)
    asyncio.run(a._handle_message("mt/state/msh/US/ME/2/e/LongFast/!gw000001", env))
    asyncio.run(a._handle_message("mt/public/msh/US/ME/2/e/LongFast/!gw000002", env))  # same packet, other broker
    assert [m.body for m in out] == ["via bridge"]
    assert out[0].raw["source"] == "state"
    node = asyncio.run(a.nodes())[0]
    assert node.meta["protocol"] == "meshtastic"
    assert "mt/state/msh/US/ME" in a._mt_roots["LongFast"]


def test_publish_through_local_bridge_prefix():
    a, _ = _pub_adapter(publish_roots=["mt/out/state"])
    msg = NormalizedMessage(source_adapter="x", source_channel="o", from_id="local", from_display="op",
                            body="hello", raw={"channel_idx": 0})
    assert asyncio.run(a.send(msg)) is True
    assert [t for t, _ in a._client.published] == ["mt/out/state/2/e/LongFast/!ec000001"]


def test_text_carries_senders_last_known_position():
    a, out = _adapter()
    pos = mesh_pb2.Position(latitude_i=int(44.31 * 1e7), longitude_i=int(-69.78 * 1e7))
    asyncio.run(a._handle_message(TOPIC, _envelope(portnums_pb2.POSITION_APP, pos.SerializeToString(), pid=601)))
    asyncio.run(a._handle_message(TOPIC, _envelope(portnums_pb2.TEXT_MESSAGE_APP, b"here", pid=602)))
    m = out[-1]
    assert m.body == "here" and round(m.lat, 2) == 44.31 and round(m.lon, 2) == -69.78
    assert m.raw["position"] == "last_known"
    asyncio.run(a._handle_message(TOPIC, _envelope(portnums_pb2.TEXT_MESSAGE_APP, b"stranger", from_num=0x0BADF00D, pid=603)))
    assert out[-1].lat is None and out[-1].raw["position"] is None


def test_json_uplink_text_lands_on_its_channel_with_a_name():
    a, out = _adapter()
    jt = "msh/US/ME/2/json/Bot&Ch/!0abcdef1"
    info = {"from": 0x0ABCDEF1, "id": 1, "type": "nodeinfo", "sender": "!0abcdef1",
            "payload": {"longname": "Plane Spotter", "shortname": "PLN", "hardware": 43}}
    txt = {"from": 0x0ABCDEF1, "to": 0xFFFFFFFF, "id": 2, "type": "text", "sender": "!0abcdef1",
           "hops_away": 0, "payload": {"text": "[MIL: ACFT] N/A Alt:21000ft Sq:5367 Hex:AE0146"}}
    asyncio.run(a._handle_message(jt, json.dumps(info).encode()))
    asyncio.run(a._handle_message(jt, json.dumps(txt).encode()))
    asyncio.run(a._handle_message(jt, json.dumps(txt).encode()))   # second gateway's copy
    assert len(out) == 1
    m = out[0]
    assert m.source_channel == "Bot&Ch" and m.from_id == "!0abcdef1" and m.from_display == "Plane Spotter"
    assert "Bot&Ch" in a._mt_channel_names


def test_json_copy_of_an_already_decrypted_packet_is_dropped():
    a, out = _adapter()
    asyncio.run(a._handle_message(TOPIC, _envelope(portnums_pb2.TEXT_MESSAGE_APP, b"once", pid=700)))
    dup = {"from": 0x1234ABCD, "id": 700, "type": "text", "payload": {"text": "once"}}
    asyncio.run(a._handle_message("msh/US/ME/2/json/LongFast/!gw000001", json.dumps(dup).encode()))
    assert [m.body for m in out] == ["once"]


def test_json_dm_between_other_nodes_gets_its_own_channel():
    a, out = _adapter()
    who = {"from": 0x0E0E0E01, "id": 8, "type": "nodeinfo", "payload": {"longname": "Example Station"}}
    dm = {"from": 0x0C0FFEE1, "to": 0x0E0E0E01, "id": 9, "type": "text", "payload": {"text": "your weather"}}
    asyncio.run(a._handle_message("msh/US/ME/2/json/LongFast/!0abcdef1", json.dumps(who).encode()))
    asyncio.run(a._handle_message("msh/US/ME/2/json/PKI/!0abcdef1", json.dumps(dm).encode()))
    assert len(out) == 1
    m = out[0]
    assert m.source_channel == "Other DMs" and m.body == "[to Example Station] your weather"
    assert m.raw["channel_idx"] is None and "PKI" not in a._mt_channel_names[:2]


# -- bridge node (2026-11-21): bridged posts don't carry the gateway's name -----

def test_bridged_post_comes_from_the_bridge_node_with_its_own_nodeinfo():
    a, out = _pub_adapter(bridge_node={"long_name": "MeshCore Bridge", "short_name": "MCB", "node_id": "!ec0000b1"})
    a._mt_roots["MaineNet"] = {"msh/US/ME"}
    keys = {"MaineNet": codec.expand_psk("Vg==")}
    bridged = NormalizedMessage(source_adapter="meshcore-usb", source_channel="outbound", from_id="local",
                                from_display="op", body="Alice: hello from MeshCore",
                                raw={"channel_name": "MaineNet", "_bridged_by": "MaineNet rule"})
    assert asyncio.run(a.send(bridged)) is True
    pkts = [codec.decode_envelope(p, keys) for _, p in a._client.published]
    info, text = pkts[0], pkts[1]
    assert info["portnum"] == "NODEINFO_APP" and info["from_id"] == "!ec0000b1"
    user = mesh_pb2.User(); user.ParseFromString(info["payload"])
    assert (user.long_name, user.short_name, user.id) == ("MeshCore Bridge", "MCB", "!ec0000b1")
    assert text["from_id"] == "!ec0000b1" and text["payload"] == b"Alice: hello from MeshCore"
    # the uplinking gateway is still ours (topic + envelope), only the sender changes
    assert a._client.published[1][0].endswith("/!ec000001")
    # second bridged post: no second NodeInfo; an operator post still comes from the gateway
    a.send and asyncio.run(a.send(NormalizedMessage(source_adapter="m", source_channel="o", from_id="local",
                                                    from_display="op", body="again",
                                                    raw={"channel_name": "MaineNet", "_bridged_by": "r"})))
    asyncio.run(a.send(NormalizedMessage(source_adapter="x", source_channel="o", from_id="local",
                                         from_display="op", body="operator", raw={"channel_name": "MaineNet"})))
    froms = [codec.decode_envelope(p, keys)["from_id"] for _, p in a._client.published]
    assert froms == ["!ec0000b1", "!ec0000b1", "!ec0000b1", "!ec000001"]
    # the bridge node's own packets coming back are ours, not new messages
    asyncio.run(a._handle_message(TOPIC, _envelope(portnums_pb2.TEXT_MESSAGE_APP, b"echo",
                                                   from_num=0xEC0000B1, gateway="!gw999999", pid=77)))
    assert out == [] and a._mt_stats["own_heard"] == 1


def test_bridge_node_can_be_turned_off():
    a, _ = _pub_adapter(bridge_node=False)
    a._mt_roots["MaineNet"] = {"msh/US/ME"}
    asyncio.run(a.send(NormalizedMessage(source_adapter="m", source_channel="o", from_id="local", from_display="op",
                                         body="x", raw={"channel_name": "MaineNet", "_bridged_by": "r"})))
    pkt = codec.decode_envelope(a._client.published[0][1], {"MaineNet": codec.expand_psk("Vg==")})
    assert len(a._client.published) == 1 and pkt["from_id"] == "!ec000001"


def test_reply_goes_back_on_the_exact_case_channel_it_came_from():
    # "Bot" and "bot" are different Meshtastic channels (the name is in the hash).
    # Only "bot" is configured; a message on "Bot" opens with the default fallback key
    # and the reply must go back on "Bot", not on our "bot".
    a, out = _adapter(meshtastic_publish=True, node_id="!ec000001",
                      channel_keys={"LongFast": "AQ==", "bot": "AQ=="})
    a._client = _FakeClient()
    asyncio.run(a._handle_message("msh/US/ME/2/e/Bot/!gw",
                                  _envelope(portnums_pb2.TEXT_MESSAGE_APP, b"wx", channel="Bot", gateway="!gw")))
    assert out and out[0].source_channel == "Bot"
    reply = NormalizedMessage(source_adapter="x", source_channel="outbound", from_id="local",
                              from_display="op", body="WX base", raw={"channel_name": "Bot"})
    assert asyncio.run(a.send(reply)) is True
    assert a._client.published[-1][0] == "msh/US/ME/2/e/Bot/!ec000001"
    assert reply.source_channel == "Bot"
    pkt = codec.decode_envelope(a._client.published[-1][1], {"Bot": codec.DEFAULT_PSK})
    assert pkt["payload"] == b"WX base" and pkt["channel"] == "Bot"
    # the lower-case channel still gets lower-case replies
    lower = NormalizedMessage(source_adapter="x", source_channel="outbound", from_id="local",
                              from_display="op", body="hi", raw={"channel_name": "bot"})
    assert asyncio.run(a.send(lower)) is True and lower.source_channel == "bot"
