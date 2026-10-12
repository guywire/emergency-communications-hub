"""
ech/core/aprs_wx.py
-------------------
NWS weather bulletins carried over APRS, kept for the map's weather layer.

NWS products are relayed onto APRS-IS (and from there onto RF by some
iGates) as APRS messages to a group addressee. Real traffic captured from
APRS-IS on 2026-10-12 with filter `g/NWS*`:

    CAEFLS>APRS,qAO,AE5PL-WX::NWS-WARN :122315z,FLOOD,SCC017,SCC063,SCC079{300AA
    CAEFLS>APRS,qAO,AE5PL-WX::NWS_WARN :122315z,FLOOD,SCC017,SCC063,SCC079{300AA

- source call = issuing office + product (CAE office, FLS product)
- addressee = NWS-WARN / NWS-WATCH / NWS-ADVIS / NWS-CANCL / NWS-TEST ...
  The NWS_xxxx (underscore) twin is the "compressed zone" copy of the same
  bulletin; both are accepted and de-duplicated.
- body = DDHHMMz expiry (UTC), EVENT, then UGC zone/county codes, which may
  use '>' ranges (SCC017>019)
- {seq message number

These carry zone codes only, no polygon. They are drawn from the shared
NWS zone-shape cache (nws_zones.py), the same one the internet and GOES
EMWIN paths use.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone

from ech.core.nws_zones import UGC_RE, expand_ugc

_ADDR_RE = re.compile(r"^NWS[-_]([A-Z]{4,5})$")
_BODY_RE = re.compile(r"^(\d{6})z,([^,]+),(.+)$")

# APRS bulletin class → CAP-like severity, so the map can rank them
_SEVERITY = {"WARN": "Severe", "WATCH": "Moderate", "ADVIS": "Minor"}

# Bulletins stay listed at most this long, even if the expiry is mis-parsed.
_MAX_AGE_SEC = 24 * 3600


def parse_expiry(ddhhmm: str, now: datetime | None = None) -> datetime | None:
    """DDHHMM (UTC) → aware datetime, picking the month that puts it closest
    to now (handles a bulletin issued on the 31st that expires on the 1st)."""
    now = now or datetime.now(timezone.utc)
    try:
        day, hh, mm = int(ddhhmm[0:2]), int(ddhhmm[2:4]), int(ddhhmm[4:6])
    except (ValueError, IndexError):
        return None
    best = None
    for month_shift in (-1, 0, 1):
        y, m = now.year, now.month + month_shift
        if m < 1:
            y, m = y - 1, 12
        elif m > 12:
            y, m = y + 1, 1
        try:
            cand = datetime(y, m, day, hh, mm, tzinfo=timezone.utc)
        except ValueError:
            continue
        if best is None or abs((cand - now).total_seconds()) < abs((best - now).total_seconds()):
            best = cand
    return best


def parse_bulletin(from_call: str, addressee: str, text: str,
                   now: datetime | None = None) -> dict | None:
    """Parse one APRS NWS bulletin message. Returns None if it isn't one."""
    m = _ADDR_RE.match((addressee or "").strip().upper())
    if not m:
        return None
    kind = m.group(1)
    body = (text or "").strip()
    if "{" in body:
        body = body.split("{", 1)[0]
    b = _BODY_RE.match(body)
    if not b:
        return None
    expiry_raw, event_raw, zones_raw = b.groups()
    ugcs: list[str] = []
    for part in zones_raw.split(","):
        part = part.strip().upper()
        if not part:
            continue
        codes, _ = expand_ugc(part)
        ugcs.extend(c for c in codes if UGC_RE.match(c))
    if not ugcs:
        return None
    ugcs = list(dict.fromkeys(ugcs))
    expires = parse_expiry(expiry_raw, now)
    event = event_raw.replace("_", " ").strip().title()
    src = (from_call or "").strip().upper()
    return {
        "kind": kind,
        "from": src,
        "office": src[:3],
        "product": src[3:6],
        "event": event,
        "ugcs": ugcs,
        "expires": expires.isoformat() if expires else None,
        "severity": _SEVERITY.get(kind, "Unknown"),
    }


class AprsWxStore:
    """Active APRS NWS bulletins, keyed so NWS-x / NWS_x twins collapse."""

    def __init__(self):
        self._items: dict[tuple, dict] = {}
        self.received = 0

    def ingest(self, from_call: str, addressee: str, text: str, via: str = "") -> bool:
        """Returns True if the message was an NWS bulletin (handled here)."""
        now = datetime.now(timezone.utc)
        b = parse_bulletin(from_call, addressee, text, now)
        if not b:
            return False
        self.received += 1
        if b["kind"] == "TEST":
            return True
        if b["kind"] == "CANCL":
            zones = set(b["ugcs"])
            for k in [k for k, v in self._items.items()
                      if v["from"][:3] == b["office"] and zones & set(v["ugcs"])]:
                del self._items[k]
            return True
        key = (b["from"], b["kind"], b["event"], tuple(sorted(b["ugcs"])))
        b["received"] = now.isoformat()
        b["received_ts"] = time.time()
        b["via"] = via
        b["id"] = "aprs:" + ":".join([b["from"], b["kind"], b["event"].replace(" ", "_")] + sorted(b["ugcs"]))
        self._items[key] = b
        return True

    def active(self) -> list[dict]:
        now = datetime.now(timezone.utc)
        out = []
        for k, v in list(self._items.items()):
            exp = v.get("expires")
            expired = False
            if exp:
                try:
                    expired = datetime.fromisoformat(exp) < now
                except ValueError:
                    pass
            if expired or time.time() - v["received_ts"] > _MAX_AGE_SEC:
                del self._items[k]
                continue
            out.append(v)
        return out


# One store per process; every APRS adapter (APRS-IS and RF/KISS) feeds it.
store = AprsWxStore()
