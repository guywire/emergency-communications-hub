"""
tests/test_aprs_hub.py
----------------------
O133: APRS page backend — packet builders (APRS 1.0.1), the hub's ingest of
objects/items/weather/bulletins/acks, message retries, our own objects
(manual, re-sent until killed, optional follow-a-node), beacon settings,
auto-ack of messages addressed to us, and three KISS fixes (AX.25
destination/path, message framing, RX loop surviving a quiet channel).
"""

import asyncio
import json
import time
from datetime import datetime, timezone

import aprslib
import pytest

from ech.core import aprs_hub as hubmod
from ech.core.aprs_hub import (AprsHub, build_ack_info, build_message_info, build_object_info,
                               build_position_info, parse_item)
from ech.core.models import MeshNode


class FakeAdapter:
    aprs_kind = "is"

    def __init__(self, name="aprs-test", kind="is", ok=True):
        self.name, self.aprs_kind, self.ok = name, kind, ok
        self._callsign = "KN0O-9"
        self.sent: list[str] = []
        self._nodes: list = []
        self._beacon_lat = self._beacon_lon = None
        self._beacon_symbol, self._beacon_comment, self._beacon_interval = "/-", "", 0
        self._last_beacon_ts = 0.0

    def can_transmit(self):
        return self.ok

    async def send_info(self, info):
        self.sent.append(info)
        return self.ok

    async def nodes(self):
        return self._nodes


class FakeRouter:
    def __init__(self, *adapters):
        self._adapters = {a.name: a for a in adapters}


class FakeDB:
    def __init__(self):
        self.kv = {}

    async def get_kv(self, k):
        return self.kv.get(k)

    async def set_kv(self, k, v):
        self.kv[k] = v


def _hub(*adapters):
    h = AprsHub()
    h.set_router(FakeRouter(*adapters))
    return h


def _parse(info, src="W1AW"):
    return aprslib.parse(f"{src}>APRS,TCPIP*:{info}")


# ── builders round-trip through aprslib ──────────────────────────────────

def test_object_packet_round_trips():
    now = datetime(2026, 10, 12, 14, 5, tzinfo=timezone.utc)
    info = build_object_info("SHELTER1", 44.16, -69.12, "/;", "Red Cross shelter open", now=now)
    assert info.startswith(";SHELTER1 *121405z4409.60N/06907.20W;")
    p = _parse(info)
    assert p["format"] == "object" and p["object_name"].strip() == "SHELTER1" and p["alive"]
    assert abs(p["latitude"] - 44.16) < 1e-3 and abs(p["longitude"] + 69.12) < 1e-3
    assert p["symbol_table"] == "/" and p["symbol"] == ";"
    killed = _parse(build_object_info("SHELTER1", 44.16, -69.12, "/;", alive=False, now=now))
    assert killed["alive"] is False


def test_position_message_ack_round_trip():
    p = _parse(build_position_info(44.16, -69.12, "/-", "SignalMatrix"))
    assert p["format"] == "uncompressed" and p["messagecapable"] and p["comment"] == "SignalMatrix"
    m = _parse(build_message_info("n0call-7", "hello", "42"))
    assert m["addresse"] == "N0CALL-7" and m["message_text"] == "hello" and m["msgNo"] == "42"
    a = _parse(build_ack_info("W1AW", "42"))
    assert a["response"] == "ack" and a["msgNo"] == "42"


def test_minutes_rounding_never_gives_60():
    assert "4410.00N" in build_position_info(44.16666666, -69.0)


def test_comment_is_ascii_and_capped():
    info = build_object_info("X", 0, 0, "/.", "héllo|~" + "x" * 80)
    assert info.endswith("hllo" + "x" * 39)


def test_parse_item():
    it = parse_item(")AID #2!4903.50N/07201.75WA")
    assert it["name"] == "AID #2" and it["alive"] and abs(it["latitude"] - 49.0583) < 1e-3


# ── ingest ───────────────────────────────────────────────────────────────

def test_ingest_object_item_weather_bulletin():
    h = _hub()
    for raw in ["W1AW>APRS,TCPIP*:;LEADER   *092345z4903.50N/07201.75W>088/036",
                "W1AW>APRS,TCPIP*:)AID #2!4903.50N/07201.75WA",
                "KB1X>APRS,TCPIP*:@092345z4403.50N/06907.75W_220/004g005t057r000p000P000h85b10132L000",
                "W1AW>APRS,TCPIP*::BLN1     :Net tonight 7pm"]:
        try:
            p = aprslib.parse(raw)
        except aprslib.exceptions.UnknownFormat:
            p = {"from": raw.split(">")[0], "format": "item"}
        h.ingest(p, raw, "aprs-test")
    assert len(h.packets) == 4
    assert h.objects["LEADER"]["alive"] and h.objects["LEADER"]["symbol"] == "/>"
    assert h.objects["AID #2"]["kind"] == "item"
    wx = h.weather["KB1X"]["wx"]
    assert wx["wind_direction"] == 220 and round(wx["wind_speed"] * 2.23694) == 4   # 4 mph from course/speed
    assert round(wx["temperature"] * 9 / 5 + 32) == 57
    [b] = h.bulletins.values()
    assert b["id"] == "BLN1" and b["text"] == "Net tonight 7pm"


@pytest.mark.asyncio
async def test_message_retry_until_ack():
    a = FakeAdapter()
    h = _hub(a)
    info = build_message_info("W1AW", "hi", "7")
    h.track_outgoing("aprs-test", "W1AW", "hi", "7", info)
    o = next(iter(h.outgoing.values()))
    o["next_ts"] = 0
    await h._retry_due()
    assert a.sent == [info] and o["tries"] == 2 and o["state"] == "pending"
    h.ingest(_parse(build_ack_info("KN0O-9", "7"), src="W1AW"), "", "aprs-test")
    assert o["state"] == "acked"
    o["next_ts"] = 0
    await h._retry_due()
    assert len(a.sent) == 1   # no more retries once acked


@pytest.mark.asyncio
async def test_message_gives_up_after_retries():
    a = FakeAdapter()
    h = _hub(a)
    h.track_outgoing("aprs-test", "W1AW", "hi", "8", "x")
    o = next(iter(h.outgoing.values()))
    for _ in range(5):
        o["next_ts"] = 0
        await h._retry_due()
    assert o["state"] == "no_ack" and len(a.sent) == len(hubmod.RETRY_DELAYS)


def test_incoming_duplicate_detection():
    h = AprsHub()
    assert h.incoming_message_seen("W1AW", "5") is False
    assert h.incoming_message_seen("w1aw", "5") is True
    assert h.incoming_message_seen("W1AW", "6") is False


# ── own objects ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_own_object_send_resend_kill_and_persist():
    a = FakeAdapter()
    h = _hub(a)
    db = FakeDB()
    await h.set_db(db)
    obj = await h.upsert_object({"name": "EOC", "lat": 44.1, "lon": -69.1, "symbol": "/o",
                                 "comment": "EOC open", "adapters": ["aprs-test", "nope"], "interval_min": 3})
    assert obj["interval_min"] == hubmod.MIN_OBJECT_INTERVAL_MIN      # clamped up to 10
    assert obj["adapters"] == ["aprs-test"] and a.sent[0].startswith(";EOC      *")
    assert json.loads(db.kv["aprs_own_objects"])[0]["name"] == "EOC"
    h2 = _hub(a)
    await h2.set_db(db)
    assert "EOC" in h2.own_objects                                     # survives restart
    assert await h.kill_object("EOC")
    assert a.sent[-1].startswith(";EOC      _")
    assert h.own_objects == {} and json.loads(db.kv["aprs_own_objects"]) == []


@pytest.mark.asyncio
async def test_own_object_validation():
    h = _hub(FakeAdapter())
    for bad in [{"name": "", "lat": 1, "lon": 1, "adapters": ["aprs-test"]},
                {"name": "TOOLONGNAME", "lat": 1, "lon": 1, "adapters": ["aprs-test"]},
                {"name": "OK", "lat": 91, "lon": 1, "adapters": ["aprs-test"]},
                {"name": "OK", "lat": 1, "lon": 1, "symbol": "x", "adapters": ["aprs-test"]},
                {"name": "OK", "lat": 1, "lon": 1, "adapters": []}]:
        with pytest.raises(ValueError):
            await h.upsert_object(bad)


@pytest.mark.asyncio
async def test_own_object_follows_node():
    a = FakeAdapter()
    mesh = FakeAdapter(name="meshcore-usb")
    n = MeshNode(node_id="abc", display_name="Truck 1")
    n.lat, n.lon = 44.5, -69.5
    mesh._nodes = [n]
    h = _hub(a, mesh)
    obj = await h.upsert_object({"name": "TRUCK1", "lat": 44.0, "lon": -69.0, "adapters": ["aprs-test"],
                                 "source": {"kind": "node", "adapter": "meshcore-usb", "id": "abc"}, "follow": True})
    assert obj["lat"] == 44.5 and "4430.00N" in a.sent[-1]


@pytest.mark.asyncio
async def test_beacon_settings_clamped_and_restored():
    rf = FakeAdapter(name="aprs-rf", kind="rf")
    h = _hub(rf)
    db = FakeDB()
    await h.set_db(db)
    saved = await h.set_beacon("aprs-rf", {"interval_sec": 60, "symbol": "/-", "comment": "hi", "lat": "44.1", "lon": "-69.1"})
    assert saved["interval_sec"] == 600 and rf._beacon_lat == 44.1          # RF minimum 10 min
    await h.set_beacon("aprs-rf", {"interval_sec": 0})
    assert rf._beacon_interval == 0
    rf2 = FakeAdapter(name="aprs-rf", kind="rf")
    h2 = _hub(rf2)
    await h2.set_db(db)
    await h2.restore_beacon_settings()
    assert rf2._beacon_comment == "hi" and rf2._beacon_interval == 0
    with pytest.raises(ValueError):
        await h.set_beacon("aprs-rf", {"symbol": "zz"})


# ── adapters ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_aprs_is_acks_directed_message_once(monkeypatch):
    from ech.adapters.aprs_is import APRSISAdapter
    h = AprsHub()
    monkeypatch.setattr("ech.adapters.aprs_is.aprs_hub", h)
    a = APRSISAdapter({"name": "aprs-test", "callsign": "KN0O-9", "passcode": 12345})
    sent, queued = [], []

    async def _send_info(info):
        sent.append(info)
        return True

    async def _enqueue(msg):
        queued.append(msg)
    a.send_info, a._enqueue = _send_info, _enqueue
    pkt = aprslib.parse("W1AW>APRS,TCPIP*::KN0O-9   :hello{12")
    await a._process_packet(pkt)
    await a._process_packet(pkt)      # sender retried: ack again, deliver once
    assert sent == [":W1AW     :ack12", ":W1AW     :ack12"]
    assert len(queued) == 1 and len(h.packets) == 2


def test_kiss_frame_destination_and_path():
    from ech.adapters.aprs_kiss import APRSKISSAdapter, ax25_to_aprs_string
    a = APRSKISSAdapter({"callsign": "KN0O-9", "tx_path": "WIDE1-1,WIDE2-1"})
    s = ax25_to_aprs_string(a._build_ax25_ui(":W1AW     :hi{1"))
    assert s == "KN0O-9>APRS,WIDE1-1,WIDE2-1::W1AW     :hi{1"


@pytest.mark.asyncio
async def test_kiss_send_formats_aprs_message(monkeypatch):
    from ech.adapters.aprs_kiss import APRSKISSAdapter
    from ech.core.models import NormalizedMessage
    h = AprsHub()
    monkeypatch.setattr("ech.adapters.aprs_kiss.aprs_hub", h)
    a = APRSKISSAdapter({"callsign": "KN0O-9"})
    a._connected = True
    sent = []

    async def _send_info(info):
        sent.append(info)
        return True
    a.send_info = _send_info
    ok = await a.send(NormalizedMessage(source_adapter="x", source_channel="", from_id="me",
                                        from_display="me", body="hello", to_id="w1aw"))
    assert ok and sent[0].startswith(":W1AW     :hello{")
    assert len(h.outgoing) == 1


@pytest.mark.asyncio
async def test_kiss_rx_loop_survives_quiet_channel(monkeypatch):
    from ech.adapters import aprs_kiss
    a = aprs_kiss.APRSKISSAdapter({"callsign": "KN0O-9"})
    reads = 0

    class Reader:
        async def read(self, n):
            nonlocal reads
            reads += 1
            if reads < 3:
                await asyncio.sleep(10)      # silence → wait_for times out
            a._connected = False
            return b"x"

    real_wait_for = asyncio.wait_for

    async def fast_wait_for(coro, timeout):
        return await real_wait_for(coro, 0.01)
    monkeypatch.setattr(aprs_kiss.asyncio, "wait_for", fast_wait_for)
    a._reader, a._connected = Reader(), True
    await a._run()
    assert reads == 3     # kept reading after two quiet timeouts


# ── API ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_aprs_api_endpoints(monkeypatch):
    from httpx import AsyncClient, ASGITransport
    from ech.core.database import Database
    from ech.core.router import Router
    from ech.core.anomaly import AnomalyEngine
    from ech.api.app import create_app

    db = Database(":memory:")
    await db.connect()
    router = Router(db, anomaly_engine=AnomalyEngine({}))
    fake = FakeAdapter(name="aprs-test")
    h = AprsHub()
    h.set_router(FakeRouter(fake))
    monkeypatch.setattr(hubmod, "hub", h)
    h.ingest(aprslib.parse("W1AW>APRS,TCPIP*:;LEADER   *092345z4903.50N/07201.75W>"),
             "W1AW>APRS,TCPIP*:;LEADER   *092345z4903.50N/07201.75W>", "aprs-test")

    app = create_app(router, db)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/aprs")).status_code == 200
        s = (await client.get("/api/aprs/summary")).json()
        assert s["adapters"][0]["name"] == "aprs-test" and s["counts"]["objects"] == 1
        assert (await client.get("/api/aprs/packets?q=leader")).json()["packets"][0]["from"] == "W1AW"
        assert (await client.get("/api/aprs/packets?fmt=message")).json()["packets"] == []
        r = await client.post("/api/aprs/objects", json={"name": "BAD NAME!", "lat": 1, "lon": 1, "adapters": ["aprs-test"]})
        assert r.status_code == 400
        r = await client.post("/api/aprs/objects", json={"name": "EOC", "lat": 44.1, "lon": -69.1, "adapters": ["aprs-test"]})
        assert r.status_code == 200 and fake.sent[-1].startswith(";EOC")
        assert [o["name"] for o in (await client.get("/api/aprs/objects")).json()["own"]] == ["EOC"]
        assert (await client.delete("/api/aprs/objects/EOC")).status_code == 200
        assert (await client.delete("/api/aprs/objects/EOC")).status_code == 404
        b = (await client.get("/api/aprs/beacon")).json()["beacons"][0]
        assert b["name"] == "aprs-test" and b["min_interval_sec"] == 300
        r = await client.post("/api/aprs/messages", json={"adapter": "aprs-test", "to": "W1AW", "text": "x" * 68})
        assert r.status_code == 400

    await db.close()
