"""Outlook (sap, ski, trains, doom-o-meter), "are you a bot?" banter, and NWS
alerts on the Public channel."""
import csv
import io
import zipfile
from datetime import date, datetime, timedelta, timezone

import pytest

from ech.core import outlook as o
from ech.core.weather import PublicAlertGate, public_alert_text


def _day(dow, lo, hi, d="2027-03-01", **kw):
    return {"date": d, "dow": dow, "lo_f": lo, "hi_f": hi, "snow_in": kw.get("snow", 0.0),
            "rain_in": kw.get("rain", 0.0), "snowmaking_h": kw.get("make", 0), "hours": 24}


def test_sap_freeze_thaw_ratings():
    days = [_day("Mon", 24, 44), _day("Tue", 30, 39), _day("Wed", 40, 55), _day("Thu", 10, 25)]
    s = o.sap_outlook(days, today=date(2027, 3, 1))
    assert [d["rating"] for d in s["days"]] == ["good", "fair", "fair", "none"]   # Wed: night before froze
    assert s["in_season"] and s["sap_days"] == 3 and s["good_days"] == 1
    assert "Buckets out" in s["advice"]
    off = o.sap_outlook(days, today=date(2026, 11, 22))
    assert not off["in_season"] and "off-season" in off["season"]


def test_ski_ratings():
    assert o.ski_outlook("A", [_day("Sat", 10, 25, snow=7.5)])["rating"] == "powder day"
    assert o.ski_outlook("A", [_day("Sat", 10, 25, snow=1.5), _day("Sun", 10, 25, snow=1.0)])["rating"] == "fresh snow"
    assert o.ski_outlook("A", [_day("Sat", 40, 55, rain=1.2, make=20)])["rating"] == "thaw / rain"
    assert o.ski_outlook("A", [_day("Sat", 5, 20, make=30)])["rating"] == "snowmaking weather"


def test_daily_from_gridpoint_series():
    props = {
        "temperature": {"values": [{"validTime": "2027-01-10T12:00:00+00:00/PT2H", "value": -10.0},
                                   {"validTime": "2027-01-10T14:00:00+00:00/PT4H", "value": 1.0}]},
        "relativeHumidity": {"values": [{"validTime": "2027-01-10T12:00:00+00:00/PT6H", "value": 60}]},
        "snowfallAmount": {"values": [{"validTime": "2027-01-10T12:00:00+00:00/PT6H", "value": 50.8}]},
    }
    d = o.daily(props, tz=timezone.utc)
    assert len(d) == 1 and d[0]["hours"] == 6
    assert d[0]["lo_f"] == 14 and d[0]["hi_f"] == 34
    assert d[0]["snow_in"] == 2.0                     # 50.8 mm spread over the period
    assert d[0]["snowmaking_h"] == 2                  # only the -10 C hours


def _gtfs():
    def table(rows):
        s = io.StringIO()
        w = csv.writer(s)
        w.writerows(rows)
        return s.getvalue()
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("routes.txt", table([["route_id", "route_long_name", "route_type"],
                                        ["61", "Downeaster", "2"], ["99", "Thruway Bus", "3"]]))
        z.writestr("trips.txt", table([["route_id", "service_id", "trip_id", "trip_short_name", "trip_headsign"],
                                       ["61", "S", "t1", "680", "Boston"], ["61", "S", "t2", "681", "Brunswick"],
                                       ["99", "S", "b1", "9999", "Bangor"]]))
        z.writestr("stop_times.txt", table([
            ["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence"],
            ["t1", "05:50:00", "05:50:00", "BRK", "1"], ["t1", "06:05:00", "06:05:00", "FRE", "2"],
            ["t1", "09:10:00", "09:10:00", "BON", "3"],
            ["t2", "08:00:00", "08:00:00", "BON", "1"], ["t2", "11:10:00", "11:10:00", "FRE", "2"],
            ["t2", "25:30:00", "25:30:00", "BRK", "3"],
            ["b1", "10:00:00", "10:00:00", "BGR", "1"]]))
        z.writestr("stops.txt", table([["stop_id", "stop_name", "stop_lat", "stop_lon", "stop_timezone"],
                                       ["BRK", "Brunswick Amtrak Station", "43.91", "-69.97", "America/New_York"],
                                       ["FRE", "Freeport Amtrak Station", "43.86", "-70.10", "America/New_York"],
                                       ["BON", "Boston North Station", "42.37", "-71.06", "America/New_York"],
                                       ["BGR", "Bangor bus stop", "44.80", "-68.77", "America/New_York"]]))
        z.writestr("calendar.txt", table([["service_id", "monday", "tuesday", "wednesday", "thursday", "friday",
                                           "saturday", "sunday", "start_date", "end_date"],
                                          ["S", "1", "1", "1", "1", "1", "1", "1", "20260101", "20281231"]]))
    return b.getvalue()


def test_timetable_nearest_rail_stations_and_next_trains():
    tt = o.build_timetable(_gtfs(), 44.80, -68.78)
    assert [s["id"] for s in tt["stations"]] == ["BRK", "FRE"]     # bus stop is ignored, Boston too far
    from zoneinfo import ZoneInfo
    now = datetime(2027, 1, 5, 7, 0, tzinfo=ZoneInfo("America/New_York"))
    brk = o.next_trains(tt, now=now)[0]
    first = brk["next"][0]
    assert first["train"] == "681" and first["terminates"] and first["label"] == "Wed 1:30a"   # 25:30 = next day
    assert any(t["train"] == "680" and t["headsign"] == "Boston" for t in brk["next"])


def test_doom_score_and_parsers():
    score, label = o.doom_score(kp=2, closest_ld=50, quake_mag=5.5, local_warnings=0,
                                extreme_local=False, clock_seconds=85)
    assert score < 15 and "normal" in label
    big, _ = o.doom_score(kp=8, closest_ld=0.5, quake_mag=7.8, local_warnings=1, extreme_local=True, clock_seconds=85)
    assert big >= 85
    assert o.parse_kp([{"time_tag": "x", "Kp": 1.0}, {"time_tag": "y", "Kp": 6.33}]) == (6.33, 6.33)
    assert o.parse_kp([["time_tag", "Kp"], ["x", "2.00"], ["y", "3.33"]]) == (3.33, 3.33)
    cad = {"fields": ["des", "cd", "dist", "h"], "data": [["2026 WJ", "2026-Nov-22 04:21", "0.0043", "27.0"]]}
    rock = o.parse_cad(cad)[0]
    assert rock["dist_ld"] == pytest.approx(1.67, abs=0.02) and 5 < rock["size_m"] < 30


@pytest.mark.asyncio
async def test_bot_replies_fit_one_message():
    class St:
        _base_lat, _base_lon = 44.80, -68.78
    svc = o.OutlookService({"database": {"path": ":memory:"}}, state=St())
    days = [_day(d, 24, 44, d=f"2027-03-0{i + 1}") for i, d in enumerate(["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"])]

    async def grid_days(lat, lon):
        return days
    svc.grid_days = grid_days
    svc._tt = {**o.build_timetable(_gtfs(), 44.80, -68.78), "built": 9e12}
    svc._doom = (9e12, {"score": 9, "label": "Situation normal.", "kp_24h_max": 2.0, "aurora_possible": False,
                        "closest_asteroid": None, "biggest_quake": None, "doomsday_clock": {"seconds": 85}})
    for reply in (await svc.bot_sap(), await svc.bot_ski(), await svc.bot_trains(), await svc.bot_doom()):
        assert 0 < len(reply) <= 160, reply


# ── "are you a bot?" ───────────────────────────────────────────────────────

def test_are_you_a_bot_detection():
    from ech.core.mesh_bot import _is_are_you_bot, RAILROAD_BOT_REPLIES
    for s in ("are you a bot?", "@[SM] are you a bot", "r u a bot", "Hey, are you human?", "is this a bot?",
              "you a bot?", "bot?", "are you a bot or a human?", "are you real"):
        assert _is_are_you_bot(s), s
    for s in ("is the bot down again? it ate my reply", "bot", "botany is fun", "who are you?",
              "are you going to the net tonight?", "ping"):
        assert not _is_are_you_bot(s), s
    assert all(len(r) <= 160 for r in RAILROAD_BOT_REPLIES)


def _bot(**cfg):
    from ech.core.mesh_bot import MeshBot
    sent = []

    class Router:
        async def send(self, **kw):
            sent.append(kw)
            return {}

        async def broadcast_ws_event(self, *a):
            pass
    b = MeshBot({"mesh_bot": {"enabled": True, "channels": ["ch2"], "banter_channels": ["ch0"],
                              "mention_name": "SM", **cfg}}, router=Router())
    b._client = object()
    return b, sent


def _msg(body, ch="ch0:Public", who="Alice"):
    from ech.core.models import NormalizedMessage
    return NormalizedMessage(source_adapter="meshcore-usb", source_channel=ch, from_id=who,
                             from_display=who, body=body)


@pytest.mark.asyncio
async def test_public_gets_banter_but_no_commands():
    import asyncio
    from ech.core.mesh_bot import RAILROAD_BOT_REPLIES
    b, sent = _bot()
    await b.handle(_msg("are you a bot?"))
    await asyncio.sleep(0.05)
    assert len(sent) == 1 and sent[0]["body"] in RAILROAD_BOT_REPLIES
    await b.handle(_msg("wx"))                       # a command on Public: ignored
    await b.handle(_msg("ping"))
    await b.handle(_msg("are you a bot?", who="Bob"))   # banter cooldown per channel
    await b.handle(_msg("are you a bot?", who="EWR-BOT"))
    await asyncio.sleep(0.05)
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_banter_on_bot_channel_and_other_mentions():
    import asyncio
    b, sent = _bot()
    await b.handle(_msg("are you a bot?", ch="ch2:#bot"))
    await asyncio.sleep(0.05)
    assert len(sent) == 1
    b2, sent2 = _bot()
    await b2.handle(_msg("@[Bob] are you a bot?"))   # asked of someone else
    await asyncio.sleep(0.05)
    assert not sent2


# ── NWS warnings on Public ─────────────────────────────────────────────────

def _alert(event="Blizzard Warning", aid="a1", sev="Severe", mtype="Alert", refs=()):
    return {"id": aid, "event": event, "severity": sev, "messageType": mtype,
            "areaDesc": "Knox; Waldo; Lincoln; Hancock", "references": [{"identifier": r} for r in refs],
            "ends": "2027-01-12T19:00:00-05:00"}


def test_public_alert_gate():
    g = PublicAlertGate({"enabled": True, "max_per_day": 2, "min_interval_sec": 1800})
    now = 1_800_000_000.0
    assert g.decide(_alert(event="Winter Weather Advisory"), now, 12) == "not a public-channel event"
    assert g.decide(_alert(), now, 23) == "quiet hours"
    assert g.decide(_alert(sev="Extreme"), now, 23) is None              # extreme ignores quiet hours
    assert g.decide(_alert(), now, 12) is None
    g.record(_alert(), now)
    assert g.decide(_alert(aid="a2", mtype="Update", refs=["a1"]), now + 60, 12).startswith("update")
    assert g.decide(_alert(aid="a3"), now + 7200, 12).startswith("Blizzard Warning posted")
    assert g.decide(_alert(event="High Wind Warning", aid="w1"), now + 600, 12) == "too soon after the last post"
    g.record(_alert(event="High Wind Warning", aid="w1"), now + 3600)
    assert g.decide(_alert(event="Flood Warning", aid="f1"), now + 9000, 12) == "daily cap 2 reached"
    g2 = PublicAlertGate({"enabled": True})
    g2.load(g.dump())
    assert g2.decide(_alert(), now + 60, 12) == "already posted"         # survives a restart


def test_public_alert_text():
    t = public_alert_text(_alert())
    assert t.startswith("NWS Blizzard Warning until ") and "Knox, Waldo, Lincoln +more" in t
    assert t.endswith("Details on #weather") and len(t) <= 150


# ── Garden ─────────────────────────────────────────────────────────────────

def test_tomato_plan_stages_zone_6a():
    nov = o.tomato_plan("6a", today=date(2026, 11, 22))
    assert nov["stage"] == "wait" and nov["seed_start"] == ["2027-03-15", "2027-03-29"]
    assert nov["plant_out"] == ["2027-05-17", "2027-05-24"] and nov["last_frost"] == "2027-05-10"
    assert o.tomato_plan("6a", today=date(2027, 3, 20))["stage"] == "start seeds"
    cold = o.tomato_plan("6a", today=date(2027, 5, 20), days=[{"lo_f": 30}] * 7)
    assert cold["stage"] == "plant out" and "Frost risk" in cold["advice"]
    warm = o.tomato_plan("6a", today=date(2027, 5, 20), days=[{"lo_f": 52}] * 7)
    assert "plant tomatoes out" in warm["advice"]
    own = o.tomato_plan("6a", today=date(2027, 1, 5), last_frost="05-20")
    assert own["last_frost"] == "2027-05-20" and own["frost_source"] == "config"


def test_market_panel_groups_cover_maine_extras():
    from ech.core.news_markets import NewsMarkets, DEFAULT_PANEL
    syms = {s for _, g in DEFAULT_PANEL for s, _ in g}
    assert {"ES=F", "HO=F", "CL=F", "GC=F", "^TNX", "BTC-USD", "CADUSD=X", "LBR=F"} <= syms
    nm = NewsMarkets({"markets": {"panel": [["Mine", [["XX=F", "Thing"]]]]}})
    assert nm.panel == [("Mine", [("XX=F", "Thing")])]


def test_sap_needs_a_deep_freeze_first():
    days = [_day("Mon", 24, 44), _day("Tue", 26, 45), _day("Wed", 25, 42)]
    mild = [{"date": f"2027-02-{i:02d}", "hi_f": 40, "lo_f": 30} for i in range(1, 28)]
    s = o.sap_outlook(days, today=date(2027, 3, 1), history=mild)
    assert s["primed"] is False and not s["in_season"] and "waiting for a deep cold spell" in s["season"]
    cold = [{"date": f"2027-02-{i:02d}", "hi_f": 25, "lo_f": 5} for i in range(1, 28)]
    s = o.sap_outlook(days, today=date(2027, 3, 1), history=cold)
    assert s["primed"] and s["in_season"] and s["cold_days_45"] == 27
    nov = o.sap_outlook(days, today=date(2026, 11, 22), history=mild)
    assert not nov["in_season"] and "not worth tapping" in nov["summary"]


def test_sun_times_and_hunt_outlook():
    rise, sset = o.sun_times(44.80, -68.78, date(2026, 11, 22))
    assert (rise.hour, sset.hour) in ((6, 16), (11, 21))          # local or UTC test box
    days = [_day("Mon", 30, 45, d="2026-11-23"), _day("Tue", 20, 30, d="2026-11-24", snow=1.0),
            _day("Sun", 20, 30, d="2026-11-29")]
    h = o.hunt_outlook(days, 44.80, -68.78, o.MAINE_DEER_2026, today=date(2026, 11, 23))
    assert "Firearms" in h["open"] and not h["sunday_closed"] and h["legal_hours"]
    assert h["days"][1]["rating"] in ("good", "excellent") and "cold snap" in h["days"][1]["why"]
    assert h["days"][2]["closed"]                                   # Sunday
    assert h["next_season"]["name"].startswith("Muzzleloader")


def test_hunt_wmd_filter():
    svc = o.OutlookService({"database": {"path": ":memory:"}, "outlook": {"hunting": {"wmd": 5}}})
    names = [s["name"] for s in o.MAINE_DEER_2026 if not s.get("wmds") or 5 in s["wmds"]]
    assert not any("extended" in n for n in names)


def test_mold_index():
    from ech.core.map_layers import mold_index, grid_points
    assert mold_index([90] * 10, [60] * 10) == (100, "very high")
    assert mold_index([50] * 10, [60] * 10) == (0, "low")
    assert mold_index([90, 90, 50, 50], [60, 30, 60, 60]) == (25, "moderate")
    assert len(grid_points(44.8, -68.78)) == 49


def test_phone_outlook_screens_and_generic_cfg():
    from ech.core import yealink_xml as yx
    from ech.core.phone_provision import generic_cfg
    h = o.hunt_outlook([_day("Mon", 30, 45, d="2026-11-23")], 44.8, -68.78, o.MAINE_DEER_2026, today=date(2026, 11, 23))
    x = yx.outlook_screen("hunt", h)
    assert "Firearms" in x and "Legal hours" in x and "<YealinkIPPhoneTextScreen" in x
    assert "Unavailable" in yx.outlook_screen("sap", {"error": "base location not set"})
    assert "outlook/hunt.xml" in yx.outlook_menu("http://x/yealink/k")
    assert "/outlook.xml" in yx.main_menu("http://x/yealink/k")
    cfg = generic_cfg("10.0.0.5", 2, {"xml_key": "K"})
    assert "account.2.cid_source = 6" in cfg and "linekey.1." not in cfg and "linekey.2.type = 27" in cfg
    assert "app.hide.icon" not in cfg


# ── Bot channels across MeshCore and Meshtastic ────────────────────────────

def _bot2(**cfg):
    from ech.core.mesh_bot import MeshBot
    sent = []

    class MeshtasticAdapter:          # type name is what the bot checks
        pass

    class MQTTAdapter:
        pass

    class Router:
        _adapters = {"Meshtastic": MeshtasticAdapter(), "meshtastic-mqtt-me": MQTTAdapter(), "meshcore-usb": object()}

        async def send(self, **kw):
            sent.append(kw)
            return {}

        async def broadcast_ws_event(self, *a):
            pass
    b = MeshBot({"mesh_bot": {"enabled": True, "mention_name": "SM", "per_user_cooldown_sec": 0,
                              "global_cooldown_sec": 0, **cfg}}, router=Router())
    b._client = object()
    return b, sent


@pytest.mark.asyncio
async def test_bot_channel_entries_per_network():
    import asyncio
    from ech.core.models import NormalizedMessage

    def m(adapter, ch, who="Al"):
        return NormalizedMessage(source_adapter=adapter, source_channel=ch, from_id=who, from_display=who, body="ping")
    b, sent = _bot2(channels=["ch2", "meshtastic-mqtt-me/MaineNet", "Meshtastic/ch1"])
    await b.handle(m("Meshtastic", "ch2", "a"))                 # MeshCore entry must not catch the radio's ch2
    await b.handle(m("meshtastic-mqtt-me", "LongFast", "b"))    # not listed
    await asyncio.sleep(0.05)
    assert sent == []
    await b.handle(m("meshcore-usb", "ch2:#bot", "c"))
    await b.handle(m("meshtastic-mqtt-me", "MaineNet", "d"))
    await b.handle(m("Meshtastic", "ch1", "e"))
    await asyncio.sleep(0.05)
    by = {s["adapter_names"][0]: s["raw"] for s in sent}
    assert by == {"meshcore-usb": {"channel_idx": 2}, "meshtastic-mqtt-me": {"channel_name": "MaineNet"},
                  "Meshtastic": {"channel_idx": 1}}


@pytest.mark.asyncio
async def test_meshtastic_dm_reply_is_a_dm():
    import asyncio
    from ech.core.models import NormalizedMessage
    b, sent = _bot2(channels=[], adapters=[])
    await b.handle(NormalizedMessage(source_adapter="meshtastic-mqtt-me", source_channel="DM", from_id="!849a8830",
                                     from_display="Al", body="ping"))
    await asyncio.sleep(0.05)
    assert sent and sent[0]["to_id"] == "!849a8830"
