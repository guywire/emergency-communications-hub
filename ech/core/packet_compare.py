"""
ech/core/packet_compare.py
--------------------------
Packet-level comparison: what THIS station heard vs what other MeshCore
observers (LetsMesh, or your own observer broker) heard.

Both sides are keyed by the observer packet hash (ech/core/meshcore_packet.py
— path-independent, identical to the "hash" observers publish), so one
transmission matches across stations regardless of hops:

  * local side    — every raw OTA packet the local MeshCore node logs
                    (LOG_DATA 0x88), recorded by the MeshCore adapter
  * observer side — every packet a compare adapter receives over MQTT

Answers "did we hear it?", "who else heard it?", "what are we missing?" per
packet type, over a rolling window. In memory only, bounded.
"""

from __future__ import annotations

import time
from collections import OrderedDict

from ech.core.meshcore_packet import PAYLOAD_TYPES, packet_hash, parse

WINDOW_S = 3600
MAX_PACKETS = 5000


class PacketCompare:
    def __init__(self):
        self._pkts: "OrderedDict[str, dict]" = OrderedDict()
        self._own_ids: set[str] = set()     # our own key prefix(es) — ignore our echo via a broker
        self._raw_listeners: list = []      # e.g. the MQTT bridge publishing our raw packets

    def add_raw_listener(self, fn) -> None:
        """fn(raw: bytes, snr, rssi, hash) for every locally heard packet."""
        if fn not in self._raw_listeners:
            self._raw_listeners.append(fn)

    def _is_own(self, origin: str) -> bool:
        return any(origin.startswith(p) for p in self._own_ids)

    # ── recording ─────────────────────────────────────────────────────────

    def _entry(self, h: str, ptype: int | None, now: float) -> dict:
        e = self._pkts.get(h)
        if e is None:
            e = {"hash": h, "type": ptype, "first": now, "last": now,
                 "local": None, "observers": {}}
            self._pkts[h] = e
            while len(self._pkts) > MAX_PACKETS:
                self._pkts.popitem(last=False)
        else:
            self._pkts.move_to_end(h)
            e["last"] = now
            if e["type"] is None:
                e["type"] = ptype
        return e

    def set_own_id(self, origin_id: str) -> None:
        if origin_id:
            self._own_ids.add(origin_id.upper())

    def record_local(self, raw: bytes, snr: float | None, rssi: int | None, adapter: str = "") -> str | None:
        p = parse(raw)
        if p is None:
            return None
        h = packet_hash(raw)
        now = time.time()
        e = self._entry(h, p["payload_type"], now)
        if e["local"] is None:
            e["local"] = {"t": now, "snr": snr, "rssi": rssi, "hops": len(p["path"]),
                          "route": p["route"], "adapter": adapter}
        for fn in list(self._raw_listeners):
            try:
                fn(raw, snr, rssi, h)
            except Exception:
                pass
        return h

    def record_observer(self, data: dict, source: str = "") -> str | None:
        """One packet JSON as observers publish it (hash and/or raw)."""
        origin = str(data.get("origin_id") or "").upper()
        if origin and self._is_own(origin):
            return None                         # our own report coming back via a broker
        raw_hex = str(data.get("raw") or "").strip()
        h = str(data.get("hash") or "").strip().upper()
        ptype = None
        if raw_hex:
            try:
                raw = bytes.fromhex(raw_hex)
                p = parse(raw)
                if p:
                    ptype = p["payload_type"]
                    h = packet_hash(raw)      # recompute: never trust a missing/odd hash field
            except ValueError:
                pass
        if not h or h == "0000000000000000":
            return None
        if ptype is None:
            try:
                ptype = int(data.get("packet_type"))
            except (TypeError, ValueError):
                ptype = None
        now = time.time()
        e = self._entry(h, ptype, now)
        key = origin[:12] or (data.get("origin") or "?")
        if key not in e["observers"]:
            e["observers"][key] = {"t": now, "name": data.get("origin", ""), "source": source,
                                   "snr": data.get("SNR"), "rssi": data.get("RSSI")}
        return h

    # ── reporting ─────────────────────────────────────────────────────────

    def _recent(self, window_s: int):
        cutoff = time.time() - window_s
        return [e for e in self._pkts.values() if e["last"] >= cutoff]

    def summary(self, window_s: int = WINDOW_S) -> dict:
        rows = self._recent(window_s)
        both = sum(1 for e in rows if e["local"] and e["observers"])
        local_only = sum(1 for e in rows if e["local"] and not e["observers"])
        obs_only = sum(1 for e in rows if not e["local"] and e["observers"])
        by_type: dict[str, dict] = {}
        for e in rows:
            name = PAYLOAD_TYPES[e["type"]] if isinstance(e["type"], int) and 0 <= e["type"] < 16 else "?"
            t = by_type.setdefault(name, {"both": 0, "local_only": 0, "observers_only": 0})
            if e["local"] and e["observers"]:
                t["both"] += 1
            elif e["local"]:
                t["local_only"] += 1
            else:
                t["observers_only"] += 1
        observers = {}
        for e in rows:
            for k, o in e["observers"].items():
                ob = observers.setdefault(k, {"name": o["name"], "source": o["source"], "packets": 0, "shared_with_us": 0})
                ob["packets"] += 1
                if e["local"]:
                    ob["shared_with_us"] += 1
        heard_by_others = both + obs_only
        return {
            "window_s": window_s, "packets": len(rows),
            "heard_by_both": both, "local_only": local_only, "observers_only": obs_only,
            # Of everything other observers reported, what fraction did we hear too?
            "local_coverage_pct": round(100.0 * both / heard_by_others, 1) if heard_by_others else None,
            "by_type": by_type, "observers": observers,
        }

    def recent(self, limit: int = 100, window_s: int = WINDOW_S, only: str = "") -> list[dict]:
        rows = sorted(self._recent(window_s), key=lambda e: e["last"], reverse=True)
        out = []
        for e in rows:
            status = "both" if e["local"] and e["observers"] else ("local_only" if e["local"] else "observers_only")
            if only and status != only:
                continue
            out.append({
                "hash": e["hash"], "status": status, "first": e["first"],
                "type": PAYLOAD_TYPES[e["type"]] if isinstance(e["type"], int) and 0 <= e["type"] < 16 else "?",
                "local": e["local"],
                "observers": [{"id": k, **v} for k, v in e["observers"].items()],
            })
            if len(out) >= limit:
                break
        return out


# Process-wide: fed by the MeshCore adapter (local) and compare adapters (observers)
registry = PacketCompare()
