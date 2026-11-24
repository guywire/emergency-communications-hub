"""
Regions and sharing: the mesh's services are checked one region at a time, and
instances on the same mesh share what they found, so a region checked by one
of them isn't checked again by the others.

Regions
-------
Every address falls in one of four regions by its second octet:

  region 1: x.0-63.x.x    region 2: x.64-127.x.x
  region 3: x.128-191.x.x region 4: x.192-255.x.x

AREDN's 10.x addresses are made from the radio's MAC address, so the four come
out even (one large mesh, 2026-11: 1149 / 1113 / 1026 / 1192 hosts). 44-net
(AMPRNet) addresses, and anything else, follow the same rule (44.192.0.0/10 is
no longer amateur space, so region 4 holds only 10.x in practice). A service belongs
to the region of the node that advertises it. One region is checked every
refresh_hours / 4 (6 h by default): each region once a day, spread over the day.

Sharing
-------
The exchange between copies is organised by layer. /peer/v1/info says which
layers a copy offers; each has its own paths under /peer/v1/<layer>/, and every
shared result names its layer. Two layers so far:

  services  /peer/v1/services/region/N   signed results, one per region (below)
  tiles     /peer/v1/tiles/Z/X/Y.png      map tiles (tile_cache.py)

More layers (ADS-B / AIS from the mesh's receivers, APRS, MeshPhone status,
weather) are planned the same way: see docs/SHARING.md in guywire/aredn-services-map.
APP stays "aredn-services-map": it names the exchange, which ECH speaks too.

Before checking a region the instance asks the other instances it knows on the
mesh (advertised as a service named like "AREDN Services Map", or listed in the
config) what they have. If one checked that region within the last 4 hours
(sharing.max_age_hours) and its result passes every test below, that result
is used and the region isn't checked again: a few requests instead of hundreds.

Is the data legitimate?
-----------------------
A shared result is used only when all of this holds:

 1. Signed. The instance that checked the region signs it (Ed25519); the
    signature must verify over the exact text received. Its key is pinned to
    its station name the first time it is seen, and a different key under the
    same name is refused. A result passed along by a third instance keeps its
    original signature, so it can be relayed but not altered.
 2. Current and in scope: same region scheme, this region, checked within the
    last 4 hours and not in the future.
 3. Nothing added: every entry must be a service this station's own node lists
    in that region right now (matched by advertising node, name and link). Its
    name, link and category come from our own list; only the measurements are
    taken from the peer, and only in the words this program itself writes
    (status, kind, software, viewer, ports, feed), with sane numbers and
    locations. Anything else is dropped.
 4. Complete: it covers at least 90% of the region's services. The rest are
    checked here.
 5. Spot-checked: a random sample (5%, at least 8, at most 30) is checked again
    from here, and at least 80% must agree (reachable or not, and the node's
    location within 2 km). Services do come and go, so the bar isn't 100%.
    A result that fails isn't used and its signer isn't trusted for 7 days.
"""
from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import logging
import math
import os
import re
import time

log = logging.getLogger(__name__)

APP = "aredn-services-map"
PROTO = 1
SCHEME = "octet2/4"
REGIONS = 4
MAX_AGE_S = 4 * 3600
FUTURE_SKEW_S = 600
MIN_COVERAGE = 0.9
SAMPLE_FRAC, SAMPLE_MIN, SAMPLE_MAX = 0.05, 8, 30
MIN_AGREE = 0.8
LOCATION_KM = 2.0
DISTRUST_S = 7 * 86400
MAX_DOC_BYTES = 5 * 1024 * 1024
MAX_INFO_BYTES = 64 * 1024
MAX_PEERS = 10
INFO_PATH = "/peer/v1/info"
REGION_PATH = "/peer/v1/services/region/{k}"
TILE_PATH = "/peer/v1/tiles/{z}/{x}/{y}.png"
LAYER = "services"
PEER_NAME_RE = re.compile(r"services[ _-]?map", re.I)
UP = {"open", "password", "blocked"}         # the service answered


# ── regions ──────────────────────────────────────────────────────────────────
def region_of(ip: str) -> int | None:
    """0-3 for an IPv4 address, by its second octet; None if it isn't one."""
    try:
        a = ipaddress.IPv4Address(str(ip).strip())
    except ValueError:
        return None
    return a.packed[1] * REGIONS // 256


def region_range(k: int) -> tuple[int, int]:
    return k * 256 // REGIONS, (k + 1) * 256 // REGIONS - 1


def region_label(k: int) -> str:
    lo, hi = region_range(k)
    return f"10.{lo}-{hi}.x.x" + (f" / 44.{lo}-{hi}.x.x" if lo < 192 else "")   # 44.192/10 isn't amateur


def svc_id(s: dict) -> str:
    """The same id on every instance for the same advertised service."""
    raw = f"{s.get('ip') or ''}\n{s.get('name') or ''}\n{s.get('link') or ''}"
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:20]


# ── identity: an Ed25519 key per instance ───────────────────────────────────
class Identity:
    def __init__(self, path: str) -> None:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        raw = None
        try:
            with open(path, "rb") as f:
                raw = f.read()
        except OSError:
            pass
        if raw and len(raw) == 32:
            self._key = Ed25519PrivateKey.from_private_bytes(raw)
        else:
            from cryptography.hazmat.primitives import serialization
            self._key = Ed25519PrivateKey.generate()
            raw = self._key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                          serialization.NoEncryption())
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(raw)
        from cryptography.hazmat.primitives import serialization
        pub = self._key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.key = base64.b64encode(pub).decode()

    def sign(self, data: bytes) -> str:
        return base64.b64encode(self._key.sign(data)).decode()


def verify(key_b64: str, data: bytes, sig_b64: str) -> bool:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    try:
        pub = base64.b64decode(key_b64, validate=True)
        sig = base64.b64decode(sig_b64, validate=True)
        if len(pub) != 32 or len(sig) != 64:
            return False
        Ed25519PublicKey.from_public_bytes(pub).verify(sig, data)
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


# ── what is shared ───────────────────────────────────────────────────────────
MEASURED = ("status", "kind", "software", "view", "ms", "open_ports", "other_apps", "feed",
            "node", "lat", "lon", "grid", "place")


def shareable(rec: dict) -> dict:
    return {"id": rec["id"], **{k: rec.get(k) for k in MEASURED if k in rec}}


def make_doc(identity: Identity, station: str, region: int, scanned_at: float, records: list[dict]) -> dict:
    payload = {"app": APP, "proto": PROTO, "layer": LAYER, "scheme": SCHEME, "region": region,
               "scanned_at": round(float(scanned_at), 1),
               "scanner": {"station": station[:63], "key": identity.key},
               "services": [shareable(r) for r in records]}
    text = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    return {"payload": text, "sig": identity.sign(text.encode("utf-8"))}


# ── the words this program writes (anything else from a peer is dropped) ──────
def _vocab():
    from ech.core import aredn_cameras as cams
    software = {sw for _, sw, _ in cams._SOFTWARE} | {"", "NTP: not synchronised"}
    views = ({v for _, _, v in cams._SOFTWARE} | {v for _, v in cams.CAMERA_PORTS.values()}
             | {"", "browser", "browser (with the password)", "browser (still image)", "browser (live MJPEG)"})
    apps = {v for _, v in cams.CAMERA_PORTS.values()}
    return software, views, apps, set(cams.CAMERA_PORTS)


STATUSES = {"open", "password", "blocked", "no_reply", "closed", "unreachable", "error", "listed"}
KINDS = {"", "login", "forbidden", "snapshot", "mjpeg", "page", "redirect", "rtsp", "ntp", "listed", "http", "https"}
_KIND_RE = re.compile(r"^(http|rtsp) \d{3}$")
_NTP_RE = re.compile(r"^NTP stratum (1[0-5]|[1-9])$")
_NODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
_GRID_RE = re.compile(r"^[A-Ra-r]{2}\d{2}([A-Xa-x]{2}(\d{2})?)?$")
_PLACE_RE = re.compile(r"^[^\x00-\x1f<>\"`]{0,80}$")


def clean_measured(m: dict, base: dict) -> dict | None:
    """The peer's measurements for one service, or None if any value isn't one
    this program would write. `base` is our own record (from our own list)."""
    if not isinstance(m, dict):
        return None
    software, views, apps, ports = _vocab()
    st = m.get("status")
    if st not in STATUSES:
        return None
    kind = m.get("kind") or ""
    if not isinstance(kind, str) or not (kind in KINDS or _KIND_RE.match(kind) or kind == base.get("scheme")):
        return None
    sw = m.get("software") or ""
    if not isinstance(sw, str) or not (sw in software or _NTP_RE.match(sw)):
        return None
    view = m.get("view") or ""
    if not isinstance(view, str) or view not in views:
        return None
    ms = m.get("ms")
    if ms is not None and (not isinstance(ms, (int, float)) or isinstance(ms, bool) or not 0 <= ms <= 120000):
        return None
    op = m.get("open_ports") or []
    if not isinstance(op, list) or len(op) > len(ports) or any(p not in ports for p in op):
        return None
    oa = m.get("other_apps") or []
    if not isinstance(oa, list) or len(oa) > len(apps) or any(a not in apps for a in oa):
        return None
    out = {"status": st, "kind": kind, "software": sw, "view": view,
           "ms": None if ms is None else int(ms), "open_ports": list(op), "other_apps": list(oa)}
    feed = m.get("feed")
    if feed is not None:
        from ech.core.aredn_services import feed_paths
        cat = base.get("category")
        if (cat not in ("adsb", "ais") or not isinstance(feed, dict)
                or feed.get("path") not in feed_paths(base.get("href") or base.get("link") or "", cat)
                or not isinstance(feed.get("count"), int) or isinstance(feed.get("count"), bool)
                or not 0 <= feed["count"] <= 1_000_000):
            return None
        out["feed"] = {"path": feed["path"], "count": feed["count"]}
    node = m.get("node") or ""
    if not isinstance(node, str) or (node and not _NODE_RE.match(node)):
        return None
    out["node"] = node
    lat, lon = m.get("lat"), m.get("lon")
    if lat is None and lon is None:
        out["lat"] = out["lon"] = None
    elif (isinstance(lat, (int, float)) and isinstance(lon, (int, float)) and not isinstance(lat, bool)
          and not isinstance(lon, bool) and -90 <= lat <= 90 and -180 <= lon <= 180 and (lat, lon) != (0, 0)):
        out["lat"], out["lon"] = round(float(lat), 4), round(float(lon), 4)
    else:
        return None
    grid = m.get("grid") or ""
    if not isinstance(grid, str) or (grid and not _GRID_RE.match(grid)):
        return None
    out["grid"] = grid
    place = m.get("place") or ""
    if not isinstance(place, str) or not _PLACE_RE.match(place):
        return None
    out["place"] = place
    return out


class Rejected(Exception):
    pass


def open_doc(doc, region: int, now: float, max_age_s: float, own_key: str) -> dict:
    """Checks 1-2 (signature, scheme, region, age): the payload, or Rejected."""
    if not isinstance(doc, dict) or not isinstance(doc.get("payload"), str) or not isinstance(doc.get("sig"), str):
        raise Rejected("not a signed result")
    try:
        p = json.loads(doc["payload"])
    except ValueError:
        raise Rejected("unreadable payload")
    if not isinstance(p, dict) or p.get("app") != APP or p.get("proto") != PROTO:
        raise Rejected("not this program's result")
    if p.get("layer") != LAYER:
        raise Rejected(f"not a {LAYER} result")
    sc = p.get("scanner")
    if not isinstance(sc, dict) or not isinstance(sc.get("key"), str) or not isinstance(sc.get("station"), str):
        raise Rejected("no signer")
    if not verify(sc["key"], doc["payload"].encode("utf-8"), doc["sig"]):
        raise Rejected("bad signature")
    if sc["key"] == own_key:
        raise Rejected("our own result")
    if p.get("scheme") != SCHEME:
        raise Rejected(f"different region scheme {p.get('scheme')!r}")
    if p.get("region") != region:
        raise Rejected("a different region")
    at = p.get("scanned_at")
    if not isinstance(at, (int, float)) or isinstance(at, bool):
        raise Rejected("no time")
    if at > now + FUTURE_SKEW_S:
        raise Rejected("checked in the future")
    if now - at > max_age_s:
        raise Rejected(f"too old ({(now - at) / 3600:.1f} h)")
    if not isinstance(p.get("services"), list):
        raise Rejected("no services")
    return p


def measured_from(payload: dict, ours: dict[str, dict]) -> dict[str, dict]:
    """Check 3: the peer's measurements for services in OUR list, cleaned.
    `ours` maps service id -> our own base record for this region."""
    out: dict[str, dict] = {}
    for m in payload["services"]:
        if not isinstance(m, dict) or not isinstance(m.get("id"), str):
            continue
        base = ours.get(m["id"])
        if base is None or m["id"] in out:
            continue                                   # not a service we know of here: ignored
        c = clean_measured(m, base)
        if c is not None:
            out[m["id"]] = c
    return out


def sample_ids(ids: list[str], rng=None) -> list[str]:
    import random
    rng = rng or random.SystemRandom()
    n = min(len(ids), max(SAMPLE_MIN, min(SAMPLE_MAX, math.ceil(len(ids) * SAMPLE_FRAC))))
    return rng.sample(ids, n)


def km(a_lat, a_lon, b_lat, b_lon) -> float:
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp, dl = p2 - p1, math.radians(b_lon - a_lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(min(1.0, h)))


def agrees(theirs: dict, ours: dict) -> bool:
    if (theirs.get("status") in UP) != (ours.get("status") in UP):
        return False
    if None not in (theirs.get("lat"), ours.get("lat")):
        return km(theirs["lat"], theirs["lon"], ours["lat"], ours["lon"]) <= LOCATION_KM
    return True


# ── who we trust ─────────────────────────────────────────────────────────────
class Trust:
    """Pinned station keys and signers not to trust for now, kept on disk."""

    def __init__(self, path: str | None) -> None:
        self.path = path
        self.pins: dict[str, str] = {}
        self.distrust: dict[str, dict] = {}
        self.seen: dict[str, dict] = {}
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            self.pins = dict(d.get("pins") or {})
            self.distrust = dict(d.get("distrust") or {})
            self.seen = dict(d.get("seen") or {})
        except (OSError, ValueError, TypeError, AttributeError):
            pass

    def check(self, station: str, key: str, now: float) -> None:
        bad = self.distrust.get(key)
        if bad and bad.get("until", 0) > now:
            raise Rejected(f"not trusted until {time.strftime('%Y-%m-%d %H:%M', time.localtime(bad['until']))}: {bad.get('reason', '')}")
        pinned = self.pins.get(station.lower())
        if pinned and pinned != key:
            raise Rejected(f"a different key than {station} had before")

    def accept(self, station: str, key: str, now: float, note: str) -> None:
        self.pins.setdefault(station.lower(), key)
        self.seen[key] = {"station": station, "at": now, "note": note}
        self.save()

    def reject(self, station: str, key: str, now: float, reason: str) -> None:
        self.distrust[key] = {"station": station, "until": now + DISTRUST_S, "reason": reason}
        self.save()

    def save(self) -> None:
        if not self.path:
            return
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"pins": self.pins, "distrust": self.distrust, "seen": self.seen}, f, indent=1)
            os.replace(tmp, self.path)
        except OSError as exc:
            log.debug("sharing: trust file write failed: %s", exc)


# ── finding the other instances ──────────────────────────────────────────────
def peer_targets(mesh_services: list[dict], hosts: list[dict], extra: list[str]) -> list[tuple[str, str]]:
    """(base URL by IP, Host header) for every other instance we know of."""
    from urllib.parse import urlsplit
    from ech.core import aredn_cameras as cams
    out, seen = [], set()
    for s in mesh_services:
        link = str((s or {}).get("link") or "")
        if not PEER_NAME_RE.search(str((s or {}).get("name") or "")) or not re.match(r"https?://", link, re.I):
            continue
        scheme, ip, port = cams.link_target(link, hosts)
        if ip and port:
            u = urlsplit(link)
            host = (u.hostname or ip) if port in (80, 443) else f"{u.hostname or ip}:{port}"
            key = (ip, port)
            if key not in seen:
                seen.add(key)
                out.append((f"{scheme}://{ip}:{port}", host))
    for url in extra or []:
        u = urlsplit(str(url))
        if u.scheme in ("http", "https") and u.hostname:
            port = u.port or (443 if u.scheme == "https" else 80)
            key = (u.hostname, port)
            if key not in seen:
                seen.add(key)
                out.append((f"{u.scheme}://{u.hostname}:{port}", u.netloc))
    return out[:MAX_PEERS]


async def get_json(client, base: str, host: str, path: str, cap: int):
    """A small GET with a size cap; None on any failure."""
    try:
        async with client.stream("GET", base + path, headers={"Host": host}, timeout=20,
                                 follow_redirects=False) as r:
            if r.status_code != 200:
                return None
            body = b""
            async for chunk in r.aiter_bytes():
                body += chunk
                if len(body) > cap:
                    return None
        return json.loads(body)
    except Exception:
        return None
