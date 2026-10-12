"""
ech/core/aprs_hub.py
--------------------
APRS as a status system, not just messaging (O133). Every APRS adapter
(APRS-IS and RF/KISS) hands each decoded packet to this hub, which keeps:

  packets    raw packet log (ring buffer, in memory)
  objects    objects (;) and items ()) other stations have set, alive/killed
  weather    weather stations with their full decoded report + 24 h trend
  bulletins  BLN* bulletins (NWS bulletins live in aprs_wx.py)
  outgoing   messages we sent, with ack tracking and timed retries

and runs our own transmissions:

  own objects  objects the operator created (by hand, or from a map item),
               re-sent every interval_min until killed; can follow a node's
               live position. Manual only — nothing is turned into an
               object automatically (operator decision, 2026-10-12).
  beacons      our position beacon per adapter (symbol, comment, interval)

Adapters that can transmit implement `send_info(info) -> bool`, where info is
the APRS information field (everything after "SRC>DEST,PATH:"); each adapter
adds its own header and path (TCPIP* for APRS-IS, tx_path for RF).

Packet formats: APRS 1.0.1 spec (objects ch. 11, messages/acks/bulletins
ch. 14, weather ch. 12). aprslib decodes everything except items, which are
decoded here.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import deque
from datetime import datetime, timezone

log = logging.getLogger(__name__)

# Minimum spacing between re-sends of one of our objects / beacons. APRS
# etiquette: objects every 10-30 min, fixed beacons no more than every 10
# min on RF; APRS-IS tolerates 5 min.
MIN_OBJECT_INTERVAL_MIN = 10
DEFAULT_OBJECT_INTERVAL_MIN = 20
MIN_BEACON_INTERVAL_SEC = {"rf": 600, "is": 300}
# Message retries when no ack: after 30 s, 60 s, 120 s, then give up
RETRY_DELAYS = (30, 60, 120)

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ./\-]{0,8}$")
_ITEM_RE = re.compile(r"^\)([!-~ ]{3,9}?)([!_])(\d{4}\.\d{2}[NS])(.)(\d{5}\.\d{2}[EW])(.)(.*)$")


# ── Packet builders ──────────────────────────────────────────────────────

def _lat(lat: float) -> str:
    d = int(abs(lat))
    m = round((abs(lat) - d) * 60.0, 2)
    if m >= 60.0:
        d, m = d + 1, 0.0
    return f"{d:02d}{m:05.2f}{'N' if lat >= 0 else 'S'}"


def _lon(lon: float) -> str:
    d = int(abs(lon))
    m = round((abs(lon) - d) * 60.0, 2)
    if m >= 60.0:
        d, m = d + 1, 0.0
    return f"{d:03d}{m:05.2f}{'E' if lon >= 0 else 'W'}"


def clean_text(s: str, limit: int) -> str:
    """Printable ASCII only (APRS is 7-bit), no '|' or '~' (reserved)."""
    return "".join(c for c in (s or "") if 32 <= ord(c) < 127 and c not in "|~")[:limit]


def valid_symbol(sym: str) -> bool:
    return (isinstance(sym, str) and len(sym) == 2
            and (sym[0] in "/\\" or sym[0].isdigit() or "A" <= sym[0] <= "Z")
            and 33 <= ord(sym[1]) <= 126)


def valid_object_name(name: str) -> bool:
    return bool(_NAME_RE.match(name or ""))


def build_object_info(name: str, lat: float, lon: float, symbol: str = "/.",
                      comment: str = "", alive: bool = True, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return (";" + name.ljust(9)[:9] + ("*" if alive else "_") + now.strftime("%d%H%Mz")
            + _lat(lat) + symbol[0] + _lon(lon) + symbol[1] + clean_text(comment, 43))


def build_position_info(lat: float, lon: float, symbol: str = "/-", comment: str = "") -> str:
    # '=' = position without timestamp, station is message-capable
    return "=" + _lat(lat) + symbol[0] + _lon(lon) + symbol[1] + clean_text(comment, 43)


def build_message_info(to: str, text: str, msgno: str | None = None) -> str:
    return ":" + to.upper().ljust(9)[:9] + ":" + clean_text(text, 67) + (f"{{{msgno}" if msgno else "")


def build_ack_info(to: str, msgno: str) -> str:
    return ":" + to.upper().ljust(9)[:9] + ":ack" + msgno


def parse_item(info: str) -> dict | None:
    """Uncompressed item report ')NAME!DDMM.mmN/DDDMM.mmW$comment' (aprslib
    doesn't decode items)."""
    m = _ITEM_RE.match(info or "")
    if not m:
        return None
    name, flag, la, table, lo, code, comment = m.groups()
    try:
        lat = int(la[:2]) + float(la[2:7]) / 60.0
        lon = int(lo[:3]) + float(lo[3:8]) / 60.0
    except ValueError:
        return None
    return {"name": name.strip(), "alive": flag == "!",
            "latitude": lat if la[-1] == "N" else -lat, "longitude": lon if lo[-1] == "E" else -lon,
            "symbol_table": table, "symbol": code, "comment": comment.strip()}


def _summary(p: dict) -> str:
    fmt = p.get("format", "")
    if fmt == "message":
        if p.get("response"):
            return f"{p['response']} {p.get('msgNo', '')} → {p.get('addresse', '').strip()}"
        return f"→ {p.get('addresse', '').strip()}: {p.get('message_text', '')}"
    if fmt == "bulletin":
        return f"BLN{p.get('bid', '')}{p.get('identifier', '')}: {p.get('message_text', '')}"
    if fmt == "object":
        return f"object {p.get('object_name', '').strip()} {'alive' if p.get('alive', True) else 'killed'}"
    if fmt == "status":
        return f"status: {p.get('status', '')}"
    if p.get("weather"):
        w = p["weather"]
        t = w.get("temperature")
        return "weather" + (f" {t * 9 / 5 + 32:.0f}°F" if t is not None else "")
    if p.get("latitude") is not None:
        return f"position {p.get('latitude'):.4f}, {p.get('longitude'):.4f} {p.get('comment', '')}".strip()
    return fmt or "?"


class AprsHub:
    def __init__(self):
        self.packets: deque = deque(maxlen=2000)
        self.objects: dict[str, dict] = {}
        self.weather: dict[str, dict] = {}
        self.bulletins: dict[str, dict] = {}
        self.outgoing: dict[str, dict] = {}       # "adapter|to|msgno" → state
        self.own_objects: dict[str, dict] = {}
        self._seen_incoming: dict[str, float] = {}  # "from|msgno" → time, dup suppression
        self._router = None
        self._db = None
        self._task: asyncio.Task | None = None
        self._msg_counter = int(time.time()) % 900

    # ── wiring

    def set_router(self, router) -> None:
        self._router = router

    async def set_db(self, db) -> None:
        self._db = db
        try:
            raw = await db.get_kv("aprs_own_objects")
            if raw:
                self.own_objects = {o["name"]: o for o in json.loads(raw)}
                log.info("APRS hub: restored %d own object(s)", len(self.own_objects))
        except Exception as exc:
            log.warning("APRS hub: could not restore own objects: %s", exc)

    async def _save(self) -> None:
        if self._db:
            await self._db.set_kv("aprs_own_objects", json.dumps(list(self.own_objects.values())))

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="aprs-hub")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def _adapter(self, name: str):
        return self._router._adapters.get(name) if self._router else None

    def tx_adapters(self) -> list[dict]:
        """APRS adapters that can transmit right now."""
        out = []
        if not self._router:
            return out
        for name, a in self._router._adapters.items():
            if hasattr(a, "send_info"):
                out.append({"name": name, "kind": getattr(a, "aprs_kind", "is"),
                            "callsign": getattr(a, "_callsign", ""),
                            "can_transmit": bool(getattr(a, "can_transmit", lambda: False)())})
        return out

    # ── receive side

    def ingest(self, packet: dict, raw: str, adapter: str) -> None:
        now = time.time()
        fmt = packet.get("format", "")
        src = packet.get("from", "?")
        self.packets.append({
            "ts": now, "adapter": adapter, "from": src, "format": fmt or "?",
            "raw": (raw or "")[:300], "summary": _summary(packet)[:200],
        })
        try:
            if fmt == "object":
                self._ingest_object(packet, src, adapter, now, "object")
            elif raw and ":)" in raw:
                item = parse_item(raw.split(":", 1)[1])
                if item:
                    self._ingest_object({**item, "object_name": item["name"]}, src, adapter, now, "item")
            if packet.get("weather"):
                self._ingest_weather(packet, src, adapter, now)
            if fmt == "bulletin":
                key = f"{src}|BLN{packet.get('bid', '')}{packet.get('identifier', '')}"
                self.bulletins[key] = {"from": src, "id": f"BLN{packet.get('bid', '')}{packet.get('identifier', '')}",
                                       "text": packet.get("message_text", ""), "ts": now, "adapter": adapter}
            if fmt == "message" and packet.get("response") in ("ack", "rej"):
                self._ingest_ack(packet, src, adapter)
        except Exception as exc:
            log.debug("APRS hub: ingest error: %s", exc)

    def _ingest_object(self, p: dict, src: str, adapter: str, now: float, kind: str) -> None:
        name = (p.get("object_name") or "").strip()
        if not name:
            return
        alive = p.get("alive", True)
        prev = self.objects.get(name)
        self.objects[name] = {
            "name": name, "kind": kind, "owner": src, "alive": alive,
            "lat": p.get("latitude", prev and prev.get("lat")),
            "lon": p.get("longitude", prev and prev.get("lon")),
            "symbol": (p.get("symbol_table") or "/")[:1] + (p.get("symbol") or ".")[:1],
            "comment": (p.get("comment") or "").strip()[:80],
            "ts": now, "adapter": adapter, "mine": name in self.own_objects,
        }

    def _ingest_weather(self, p: dict, src: str, adapter: str, now: float) -> None:
        w = dict(p["weather"])
        # Position weather reports carry wind in the CSE/SPD field, and there
        # SPD is mph (APRS 1.0.1 ch. 12). aprslib treats it as a normal speed
        # in knots and converts to km/h, so undo that (÷1.852) to get the mph
        # value sent, then convert to m/s like the rest of aprslib's weather.
        if "wind_direction" not in w and p.get("course") is not None:
            w["wind_direction"] = p["course"]
        if "wind_speed" not in w and p.get("speed") is not None:
            w["wind_speed"] = p["speed"] / 1.852 * 0.44704
        st = self.weather.get(src) or {"call": src, "history": deque(maxlen=288)}
        if p.get("latitude") is not None:
            st["lat"], st["lon"] = p["latitude"], p["longitude"]
        st.update({"wx": w, "ts": now, "adapter": adapter, "comment": (p.get("comment") or "")[:80]})
        hist = st["history"]
        if not hist or now - hist[-1]["ts"] >= 300:
            hist.append({"ts": now, **{k: w.get(k) for k in
                         ("temperature", "wind_speed", "wind_gust", "pressure", "humidity", "rain_1h")}})
        self.weather[src] = st

    def _ingest_ack(self, p: dict, src: str, adapter: str) -> None:
        msgno = str(p.get("msgNo", ""))
        for key, o in self.outgoing.items():
            if o["to"].upper() == src.upper() and o["msgno"] == msgno and o["state"] == "pending":
                o["state"] = "acked" if p["response"] == "ack" else "rejected"
                o["done_ts"] = time.time()
                log.info("APRS hub: message %s to %s %s", msgno, src, o["state"])

    def incoming_message_seen(self, src: str, msgno: str) -> bool:
        """True if this (sender, msgno) arrived in the last 30 min — the
        sender is retrying because our ack was lost. Ack again, but don't
        deliver twice. Records it either way."""
        now = time.time()
        for k in [k for k, t in self._seen_incoming.items() if now - t > 1800]:
            del self._seen_incoming[k]
        key = f"{src.upper()}|{msgno}"
        seen = key in self._seen_incoming
        self._seen_incoming[key] = now
        return seen

    # ── messages with ack + retry

    def next_msgno(self) -> str:
        self._msg_counter = self._msg_counter % 99999 + 1
        return str(self._msg_counter)

    def track_outgoing(self, adapter: str, to: str, text: str, msgno: str, info: str) -> None:
        self.outgoing[f"{adapter}|{to.upper()}|{msgno}"] = {
            "adapter": adapter, "to": to.upper(), "text": text, "msgno": msgno, "info": info,
            "state": "pending", "tries": 1, "sent_ts": time.time(), "next_ts": time.time() + RETRY_DELAYS[0],
        }
        if len(self.outgoing) > 300:
            for k in sorted(self.outgoing, key=lambda k: self.outgoing[k]["sent_ts"])[:50]:
                del self.outgoing[k]

    async def _retry_due(self) -> None:
        now = time.time()
        for o in list(self.outgoing.values()):
            if o["state"] != "pending" or now < o["next_ts"]:
                continue
            if o["tries"] > len(RETRY_DELAYS):
                o["state"] = "no_ack"
                continue
            a = self._adapter(o["adapter"])
            if a and await a.send_info(o["info"]):
                o["tries"] += 1
            idx = min(o["tries"] - 1, len(RETRY_DELAYS) - 1)
            o["next_ts"] = now + RETRY_DELAYS[idx]

    # ── own objects

    async def upsert_object(self, spec: dict) -> dict:
        name = (spec.get("name") or "").strip()
        if not valid_object_name(name):
            raise ValueError("Object name: 1-9 characters, letters/digits/space . / - (start with a letter or digit)")
        try:
            lat, lon = float(spec["lat"]), float(spec["lon"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("lat/lon required")
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError("lat/lon out of range")
        symbol = spec.get("symbol") or "/."
        if not valid_symbol(symbol):
            raise ValueError("symbol: table char (/ \\ 0-9 A-Z) + symbol char")
        adapters = [a for a in (spec.get("adapters") or []) if self._adapter(a) and hasattr(self._adapter(a), "send_info")]
        if not adapters:
            raise ValueError("choose at least one APRS adapter to send on")
        interval = max(MIN_OBJECT_INTERVAL_MIN, int(spec.get("interval_min") or DEFAULT_OBJECT_INTERVAL_MIN))
        prev = self.own_objects.get(name, {})
        obj = {
            "name": name, "lat": lat, "lon": lon, "symbol": symbol,
            "comment": clean_text(spec.get("comment", ""), 43),
            "adapters": adapters, "interval_min": interval,
            "source": spec.get("source") or prev.get("source"),
            "follow": bool(spec.get("follow", prev.get("follow", False))),
            "created": prev.get("created", time.time()), "last_sent": 0.0, "sends": prev.get("sends", 0),
            "last_result": {},
        }
        self.own_objects[name] = obj
        await self._send_object(obj)
        await self._save()
        return obj

    async def kill_object(self, name: str) -> bool:
        obj = self.own_objects.pop(name, None)
        if not obj:
            return False
        info = build_object_info(obj["name"], obj["lat"], obj["lon"], obj["symbol"], obj["comment"], alive=False)
        for an in obj["adapters"]:
            a = self._adapter(an)
            if a:
                await a.send_info(info)
        # Repeat the kill once more after 2 min for stations that missed it
        asyncio.get_running_loop().call_later(
            120, lambda: asyncio.ensure_future(self._send_raw(obj["adapters"], info)))
        if name in self.objects:
            self.objects[name]["alive"] = False
        await self._save()
        return True

    async def _send_raw(self, adapters: list[str], info: str) -> None:
        for an in adapters:
            a = self._adapter(an)
            if a:
                try:
                    await a.send_info(info)
                except Exception as exc:
                    log.debug("APRS hub: send on %s failed: %s", an, exc)

    async def _follow_position(self, obj: dict) -> None:
        src = obj.get("source") or {}
        if not obj.get("follow") or src.get("kind") != "node" or not self._router:
            return
        a = self._router._adapters.get(src.get("adapter", ""))
        if not a:
            return
        try:
            for n in await a.nodes():
                if n.node_id == src.get("id") and n.lat is not None and n.lon is not None:
                    obj["lat"], obj["lon"] = float(n.lat), float(n.lon)
                    return
        except Exception as exc:
            log.debug("APRS hub: follow lookup failed: %s", exc)

    async def _send_object(self, obj: dict) -> None:
        await self._follow_position(obj)
        info = build_object_info(obj["name"], obj["lat"], obj["lon"], obj["symbol"], obj["comment"])
        result = {}
        for an in obj["adapters"]:
            a = self._adapter(an)
            ok = False
            if a:
                try:
                    ok = await a.send_info(info)
                except Exception as exc:
                    log.warning("APRS hub: object %s on %s failed: %s", obj["name"], an, exc)
            result[an] = ok
        obj["last_sent"] = time.time()
        obj["sends"] = obj.get("sends", 0) + 1
        obj["last_result"] = result
        obj["last_info"] = info
        log.info("APRS hub: object %s sent %s", obj["name"], result)

    # ── loop

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(10)
                await self._retry_due()
                now = time.time()
                changed = False
                for obj in list(self.own_objects.values()):
                    if now - obj.get("last_sent", 0) >= obj["interval_min"] * 60:
                        await self._send_object(obj)
                        changed = True
                if changed:
                    await self._save()
                # Forget other stations' objects/weather not heard for 6 h
                for d in (self.objects, self.weather):
                    for k in [k for k, v in d.items() if now - v["ts"] > 6 * 3600 and not v.get("mine")]:
                        del d[k]
                for k in [k for k, v in self.bulletins.items() if now - v["ts"] > 48 * 3600]:
                    del self.bulletins[k]
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("APRS hub: loop error: %s", exc)

    # ── beacons

    def beacon_view(self) -> list[dict]:
        out = []
        for info in self.tx_adapters():
            a = self._adapter(info["name"])
            out.append({**info,
                        "lat": getattr(a, "_beacon_lat", None), "lon": getattr(a, "_beacon_lon", None),
                        "symbol": getattr(a, "_beacon_symbol", "/-"),
                        "comment": getattr(a, "_beacon_comment", ""),
                        "interval_sec": getattr(a, "_beacon_interval", 0),
                        "min_interval_sec": MIN_BEACON_INTERVAL_SEC.get(info["kind"], 600),
                        "last_beacon_ts": getattr(a, "_last_beacon_ts", 0.0),
                        "tx_path": getattr(a, "_tx_path", "TCPIP*")})
        return out

    def _apply_beacon(self, a, kind: str, b: dict) -> dict:
        if "symbol" in b:
            if not valid_symbol(b["symbol"]):
                raise ValueError("symbol: table char (/ \\ 0-9 A-Z) + symbol char")
            a._beacon_symbol = b["symbol"]
        if "comment" in b:
            a._beacon_comment = clean_text(b["comment"], 43)
        if "interval_sec" in b:
            iv = int(b["interval_sec"] or 0)
            a._beacon_interval = max(iv, MIN_BEACON_INTERVAL_SEC.get(kind, 600)) if iv > 0 else 0
        for k in ("lat", "lon"):
            if b.get(k) not in (None, ""):
                v = float(b[k])
                if not (-90 <= v <= 90 if k == "lat" else -180 <= v <= 180):
                    raise ValueError(f"{k} out of range")
                setattr(a, f"_beacon_{k}", v)
        return {"symbol": a._beacon_symbol, "comment": a._beacon_comment,
                "interval_sec": a._beacon_interval}

    async def set_beacon(self, adapter: str, b: dict) -> dict:
        a = self._adapter(adapter)
        if not a or not hasattr(a, "send_info"):
            raise ValueError("no APRS adapter by that name")
        saved = self._apply_beacon(a, getattr(a, "aprs_kind", "is"), b)
        if self._db:
            raw = await self._db.get_kv("aprs_beacon_settings")
            allb = json.loads(raw) if raw else {}
            allb[adapter] = saved
            await self._db.set_kv("aprs_beacon_settings", json.dumps(allb))
        return saved

    async def restore_beacon_settings(self) -> None:
        """Beacon settings saved from the APRS page override config.yaml."""
        if not self._db:
            return
        try:
            raw = await self._db.get_kv("aprs_beacon_settings")
            for name, b in (json.loads(raw) if raw else {}).items():
                a = self._adapter(name)
                if a and hasattr(a, "send_info"):
                    self._apply_beacon(a, getattr(a, "aprs_kind", "is"), b)
        except Exception as exc:
            log.warning("APRS hub: could not restore beacon settings: %s", exc)

    # ── views for the API

    def weather_view(self) -> list[dict]:
        out = []
        for st in self.weather.values():
            out.append({k: (list(v) if k == "history" else v) for k, v in st.items()})
        return sorted(out, key=lambda s: -s["ts"])


# One hub per process; both APRS adapter types feed it.
hub = AprsHub()
