"""Military / rescue / emergency aircraft, and our ADS-B compared with NGR&S.

The New Gloucester gateway on Meshtastic MQTT (channel NGR&S) posts only the
aircraft it finds interesting, one line each:

    [HELO: LIFE] N901XM Alt:1275ft Sq:0377 Hex:AC74FC @12:12
    [MIL: C17] RCH144 Alt:34000ft Sq:0714 Hex:AE07B9 @08:20

with no position. Matching the ICAO hex against our own track history (ADS-B
receiver and OpenSky, kept 7 days) gives each spot a position when we heard the
same aircraft, and shows which ones only Gloucester Hill heard.

Our own live aircraft are classed with the same idea:
  emergency   squawk 7500 / 7600 / 7700
  military    US military ICAO block (AE0000-AFFFFF), military callsigns
              (RCH/REACH, SAM, PAT, CNV, ...), or NGR&S tagged it MIL
  rescue      LifeFlight / medevac callsigns, US Coast Guard (C + 4 digits),
              or NGR&S tagged it HELO: LIFE
  helicopter  ADS-B emitter category A7 (rotorcraft), or NGR&S tagged it HELO
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone

NGRS_CHANNEL = "NGR&S"
_SPOT_RE = re.compile(
    r"^\s*\[(?P<kind>[A-Z\-]+):\s*(?P<type>[^\]]+)\]\s*(?P<call>\S+)\s+Alt:(?P<alt>[^\s]+)\s+"
    r"Sq:(?P<sq>\S+)\s+Hex:(?P<hex>[0-9A-Fa-f]{6})(?:\s+@(?P<hhmm>\d{1,2}:\d{2}))?")
MIL_CALL_RE = re.compile(
    r"^(RCH|REACH|SAM|SPAR|PAT|CNV|OTIS|EVAC|TOPCAT|DUKE|JAKE|KING|PEDRO|ARMY|NAVY|NVY|VV|CFC|CANFORCE|"
    r"RRR|ASCOT|GAF|BAF|NATO|HKY|BOXER|TREK|SNTRY|IRON|VIPER|TEAL|HOBO|SKULL|DOOM|BRAVO)\d", re.I)
USCG_CALL_RE = re.compile(r"^C\d{4}$", re.I)
RESCUE_CALL_RE = re.compile(r"^(LIFE|LIFEGUARD|LFLT|LN\d|MEDEVAC|MEDIC|AIRMED|ANGEL|MERCY|DART|BOSTON ?MED)", re.I)
EMERGENCY_SQUAWK = {"7500": "hijack", "7600": "radio failure", "7700": "emergency"}
ORDER = ["emergency", "military", "rescue", "helicopter"]


def parse_spot(body: str) -> dict | None:
    m = _SPOT_RE.match(body or "")
    if not m:
        return None
    alt = m.group("alt").rstrip("ft")
    return {"kind": m.group("kind").upper(), "type": m.group("type").strip().upper(),
            "call": None if m.group("call") in ("N/A", "-") else m.group("call").upper(),
            "alt_ft": int(alt) if alt.isdigit() else None,
            "squawk": None if m.group("sq") in ("N/A", "-") else m.group("sq"),
            "hex": m.group("hex").lower(), "hhmm": m.group("hhmm")}


def tag_class(spot: dict) -> str:
    """NGR&S's own tag -> our class."""
    k, t = spot.get("kind", ""), spot.get("type", "")
    if k.startswith("MIL"):
        return "military"
    if k == "HELO" and t == "LIFE":
        return "rescue"
    if "HELO" in k:
        return "helicopter"
    return "military" if "MIL" in k else "helicopter"


def is_us_military_hex(hex_id: str) -> bool:
    try:
        v = int(hex_id, 16)
    except (TypeError, ValueError):
        return False
    return 0xAE0000 <= v <= 0xAFFFFF


HELI_TYPES = re.compile(r"^(H\d{2}|EC\d{2,3}|AS\d{2,3}|A109|A119|A139|A169|AW\d{3}|B06|B407|B412|B429|B505|R22|R44|R66|"
                        r"S76|S92|UH\d+|MH\d+|HH\d+|CH\d+|V22|MD\d{2,3}|EXPL|BK17)$", re.I)


def classify(hex_id: str, flight: str = "", squawk: str = "", category: str = "",
             learned: dict | None = None, military: bool = False,
             aircraft_type: str = "") -> tuple[str, str] | None:
    """(class, why) for an interesting aircraft, else None. `military` is the
    adsb.lol database flag; `aircraft_type` its ICAO type (C17, H60, ...)."""
    flight = (flight or "").strip().upper()
    ty = (aircraft_type or "").strip().upper()
    sq = str(squawk or "").strip()
    if sq in EMERGENCY_SQUAWK:
        return "emergency", f"squawk {sq} ({EMERGENCY_SQUAWK[sq]})"
    tag = (learned or {}).get((hex_id or "").lower())
    if flight and RESCUE_CALL_RE.match(flight):
        return "rescue", f"callsign {flight}"
    if flight and USCG_CALL_RE.match(flight):
        return "rescue", f"Coast Guard {flight}"
    if tag and tag["class"] == "rescue":
        return "rescue", f"NGR&S: {tag['label']}"
    if military:
        return "military", "military (adsb.lol database)" + (f", {ty}" if ty else "")
    if is_us_military_hex(hex_id):
        return "military", "US military address" + (f", {tag['label']}" if tag else "")
    if flight and MIL_CALL_RE.match(flight):
        return "military", f"callsign {flight}"
    if tag and tag["class"] == "military":
        return "military", f"NGR&S: {tag['label']}"
    if str(category or "").upper() == "A7" or (ty and HELI_TYPES.match(ty)):
        return "helicopter", "rotorcraft" + (f" ({ty})" if ty else "")
    if tag:
        return tag["class"], f"NGR&S: {tag['label']}"
    return None


async def _rows(db, sql: str, params: tuple) -> list:
    async with db._db.execute(sql, params) as cur:
        return list(await cur.fetchall())


async def learned_tags(db, days: int = 30) -> dict:
    """hex -> {class, label, call, last} from NGR&S posts."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    out: dict[str, dict] = {}
    for ts, body in await _rows(db, "SELECT timestamp, body FROM messages WHERE source_channel=? AND timestamp>=? "
                                    "ORDER BY timestamp", (NGRS_CHANNEL, since)):
        s = parse_spot(body)
        if s:
            out[s["hex"]] = {"class": tag_class(s), "label": f"{s['kind']}: {s['type']}",
                             "call": s["call"] or (out.get(s["hex"]) or {}).get("call"), "last": ts}
    return out


async def ngrs_spots(db, hours: float = 24, window_s: float = 900) -> list[dict]:
    """Recent NGR&S spots, each matched to our nearest tracked position (+/- window_s)."""
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    spots = []
    for ts, body in await _rows(db, "SELECT timestamp, body FROM messages WHERE source_channel=? AND timestamp>=? "
                                    "ORDER BY timestamp DESC", (NGRS_CHANNEL, since)):
        s = parse_spot(body)
        if not s:
            continue
        t = datetime.fromisoformat(ts).timestamp()
        hit = await _rows(db, "SELECT ts, lat, lon, adapter, info FROM node_positions WHERE node_id=? AND ts BETWEEN ? AND ? "
                              "ORDER BY abs(ts-?) LIMIT 1", (f"icao:{s['hex']}", t - window_s, t + window_s, t))
        spot = {**s, "time": ts, "class": tag_class(s), "label": f"{s['kind']}: {s['type']}", "seen_by_us": bool(hit)}
        if hit:
            pts, lat, lon, adapter, _info = hit[0]
            spot.update({"lat": lat, "lon": lon, "matched_by": adapter, "match_offset_s": round(pts - t)})
        spots.append(spot)
    return spots


async def watch(router, db, hours: float = 24) -> dict:
    learned = await learned_tags(db)
    live = []
    for name, ad in (getattr(router, "_adapters", None) or {}).items():
        if type(ad).__name__ not in ("ADSBAdapter", "OpenSkyAdapter", "ADSBLolAdapter"):
            continue
        try:
            nodes = await ad.nodes()
        except Exception:
            continue
        for n in nodes:
            if not str(n.node_id).startswith("icao:") or n.lat is None:
                continue
            meta = getattr(n, "meta", None) or {}
            hx = n.node_id[5:]
            c = classify(hx, meta.get("flight", ""), meta.get("squawk", ""), meta.get("category", ""), learned,
                         bool(meta.get("military")), meta.get("aircraft_type", ""))
            if not c:
                continue
            live.append({"hex": hx, "flight": meta.get("flight") or "", "class": c[0], "why": c[1],
                         "lat": n.lat, "lon": n.lon, "alt_ft": meta.get("altitude_ft"),
                         "speed_kts": meta.get("speed_kts"), "track_deg": meta.get("track_deg"),
                         "squawk": meta.get("squawk") or "", "source": name,
                         "type": meta.get("aircraft_type") or "", "registration": meta.get("registration") or "",
                         "kind": type(ad).__name__})
    # one entry per aircraft (several feeds report it): our receiver, then adsb.lol, then OpenSky
    rank = {"ADSBAdapter": 0, "ADSBLolAdapter": 1, "OpenSkyAdapter": 2}
    best: dict[str, dict] = {}
    for a in live:
        if a["hex"] not in best or rank.get(a["kind"], 9) < rank.get(best[a["hex"]]["kind"], 9):
            best[a["hex"]] = a
    live = sorted(best.values(), key=lambda a: ORDER.index(a["class"]))
    spots = await ngrs_spots(db, hours)
    week = await ngrs_spots(db, 24 * 7) if hours < 24 * 7 else spots
    by_class: dict[str, int] = {}
    for s in week:
        by_class[s["class"]] = by_class.get(s["class"], 0) + 1
    seen = sum(1 for s in week if s["seen_by_us"])
    return {"live": live, "ngrs": spots,
            "stats": {"week_spots": len(week), "week_seen_by_us": seen,
                      "week_only_ngrs": len(week) - seen, "week_by_class": by_class,
                      "learned_aircraft": len(learned)},
            "generated": time.time()}
