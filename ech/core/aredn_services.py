"""
AREDN services: everything advertised as a service on the mesh, sorted into
categories, checked once a day and placed at the node that advertises it.

Source: the mesh-wide service list in a local AREDN node's sysinfo
(?services=1&hosts=1, the same URL as the MeshPhone map). Phones are left to
the MeshPhone map. Cameras keep their detailed check (aredn_cameras.py: what
it is, software, which app views it); every other service gets one request a
day, nothing more than a visitor would make:
  * http/https link: one GET (open / password needed / error);
  * any other link with a port (winlink://, telnet://, mumble://, ...): a TCP
    connect (open / closed);
  * ADS-B and AIS receivers: also a look for their public data file
    (dump1090 / readsb / tar1090 aircraft.json, AIS-catcher ships.json), so the
    map can say which ones publish a feed.

Results also feed aredn_cameras.service (the camera layer and /cameras/).

Config (radio_directory.aredn_services, all optional):
  enabled         bool   default true
  refresh_hours   float  default 24 (at least 6)
  hosts_url       str    default radio_directory.meshphone.hosts_url, else
                         radio_directory.aredn_discovery.sysinfo_url
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from urllib.parse import urlsplit

from ech.core import aredn_cameras as cams

log = logging.getLogger(__name__)

REFRESH_S = 24 * 3600
FIRST_DELAY_S = 300
RETRY_S = 1800
MAX_SERVICES = 2500

# (key, label, icon). Order = precedence when a service matches several.
CATEGORIES = [
    ("adsb", "ADS-B receivers", "✈"),
    ("ais", "AIS receivers", "⚓"),
    ("satellite", "Satellite receivers", "🛰"),
    ("camera", "Cameras", "📷"),
    ("packet", "Packet / BBS", "📟"),
    ("winlink", "Winlink", "📧"),
    ("aprs", "APRS", "📍"),
    ("tak", "TAK", "🎯"),
    ("meshradio", "Meshtastic / MeshCore", "📡"),
    ("radio", "Radio / SDR / AllStar", "📻"),
    ("chat", "Chat", "💬"),
    ("weather", "Weather", "🌦"),
    ("time", "Time servers", "🕒"),
    ("mail", "Mail", "✉"),
    ("internet", "Internet gateways", "🌐"),
    ("power", "Power / solar", "🔋"),
    ("map", "Maps", "🗺"),
    ("wiki", "Wikis / info", "📖"),
    ("other", "Other", "•"),
]
CATEGORY_KEYS = [c[0] for c in CATEGORIES]

_RX = {
    "adsb": r"ads-?b|piaware|flightaware|tar1090|dump1090|readsb|skyaware|fr24|flightradar|planefinder|adsbexchange|\bplanes?\b|aircraft",
    "ais": r"\bais\b|ais-?catcher|aiscatcher|shipplotter|marinetraffic|\bvessels?\b|ship ?track",
    "satellite": r"satnogs|\bgoes\b|noaa[- ]?(apt|1[589])|meteor[- ]?m|wxsat|weather ?sat|satdump|wxtoimg|hrpt|\bsatellite|qo-?100",
    "packet": r"\bbbs\b|\bbpq|linbpq|\bjnos|\bfbb\b|packet|\bax\.?25|xrouter|\bnet/?rom|bpqchat|\bpbbs",
    "winlink": r"winlink|\brms\b|post ?office|\bpat\b",
    "aprs": r"\baprs|igate|direwolf|aprsc",
    "tak": r"\b(a|win|i)?tak\b|freetak|tak ?server",
    "meshradio": r"meshtastic|meshcore|meshsense|corescope|meshmonitor",
    "radio": r"websdr|openwebrx|kiwisdr|sdr\b|remote ?rig|radiosonde|auto_?rx|allstar|allmon|allscan|echolink|supermon|repeater|\bdmr\b|\bysf\b|\bm17\b|\bdstar|hamvoip",
    "chat": r"meshchat|teamtalk|mumble|jitsi|\bmatrix\b|element|\birc\b|chat",
    "weather": r"weewx|cumulus|weather|\bpws\b|\bwx\b",
    "time": r"\bntp\b|chrony|time ?server|gps ?time",
    "mail": r"webmail|\bsmtp\b|\bimap\b|citadel|\bmail\b",
    "power": r"solar|battery|\bups\b|power|victron|generator",
    "wiki": r"wiki|kiwix|library|\bdocs?\b",
}
_TAG_CAT = {"camera": "camera", "video": "camera", "chat": "chat", "weather": "weather", "time": "time",
            "mail": "mail", "internet": "internet", "power": "power", "solar": "power", "battery": "power",
            "map": "map", "wiki": "wiki", "winlink": "winlink"}
_TAG_RE = re.compile(r"\[(\w+)\]\s*$")
# untagged services that are just a MeshPhone number or extension: "503-4409", "x6225"
_PHONE_NAME_RE = re.compile(r"(?<![\d.])(\(?\d{3}\)?[- ])?\d{2,3}-\d{4}(?![\d.])|\bx\d{3,5}\b|^\W*\d{5,7}\W*$", re.I)

ADSB_PATHS = ["data/aircraft.json", "/tar1090/data/aircraft.json", "/skyaware/data/aircraft.json",
              "/dump1090-fa/data/aircraft.json", "/dump1090/data/aircraft.json", "/data/aircraft.json"]
AIS_PATHS = ["api/ships.json", "ships.json", "/api/ships.json", "/ships.json", "/ships_array.json"]


def tag_of(service: dict) -> str:
    m = _TAG_RE.search(str(service.get("name") or ""))
    return m.group(1).lower() if m else ""


def categorize(service: dict) -> str | None:
    """The service's category, or None for phones (the MeshPhone map's)."""
    name, link = str(service.get("name") or ""), str(service.get("link") or "")
    text = f"{name} {link}"
    tag = tag_of(service)
    for key in ("adsb", "ais", "satellite"):
        if re.search(_RX[key], text, re.I):
            return key
    if cams.is_camera(service) and tag not in ("phone",):
        return "camera"
    for key in ("radio", "winlink", "packet", "aprs", "tak", "meshradio"):
        if re.search(_RX[key], text, re.I):
            return key
    if tag == "phone" or (not tag and _PHONE_NAME_RE.search(name)):
        return None
    if tag in _TAG_CAT:
        return _TAG_CAT[tag]
    for key in ("chat", "weather", "time", "mail", "power", "wiki"):
        if re.search(_RX[key], text, re.I):
            return key
    return "other"


def feed_paths(link: str, kind: str) -> list[str]:
    """Where an ADS-B / AIS receiver's data file may be, relative to its link."""
    base = urlsplit(link).path or "/"
    base = base if base.endswith("/") else base.rsplit("/", 1)[0] + "/"
    out = []
    for p in (ADSB_PATHS if kind == "adsb" else AIS_PATHS):
        path = p if p.startswith("/") else base + p
        if path not in out:
            out.append(path)
    return out


def feed_count(kind: str, data) -> int | None:
    """Number of aircraft / ships in a feed file, None if it isn't one."""
    if kind == "adsb" and isinstance(data, dict) and isinstance(data.get("aircraft"), list):
        return len(data["aircraft"])
    if kind == "ais":
        if isinstance(data, dict):
            for k in ("ships", "values", "vessels"):
                if isinstance(data.get(k), list):
                    return len(data[k])
        if isinstance(data, list):
            return len(data)
    return None


class _NtpProto(asyncio.DatagramProtocol):
    def __init__(self):
        self.fut = asyncio.get_event_loop().create_future()

    def datagram_received(self, data, addr):
        if len(data) >= 48 and not self.fut.done():
            self.fut.set_result(data)

    def error_received(self, exc):
        pass


async def ntp_check(ip: str, port: int = 123, timeout: float = 3.0, tries: int = 2) -> dict:
    """One NTP client request (UDP): open + stratum when the server answers."""
    loop = asyncio.get_event_loop()
    for _ in range(tries):
        try:
            transport, proto = await loop.create_datagram_endpoint(_NtpProto, remote_addr=(ip, port))
        except OSError:
            return {"status": "closed", "kind": "ntp"}
        try:
            t0 = time.monotonic()
            transport.sendto(b"\x1b" + 47 * b"\0")          # LI 0, version 3, mode 3 (client)
            data = await asyncio.wait_for(proto.fut, timeout)
            stratum = data[1]
            return {"status": "open" if 0 < stratum < 16 else "error", "kind": "ntp",
                    "software": f"NTP stratum {stratum}" if 0 < stratum < 16 else "NTP: not synchronised",
                    "ms": round((time.monotonic() - t0) * 1000)}
        except asyncio.TimeoutError:
            continue
        finally:
            transport.close()
    return {"status": "no_reply", "kind": "ntp"}


async def tcp_check(ip: str, port: int, timeout: float = 6.0) -> dict:
    t0 = time.monotonic()
    ok = await cams.port_open(ip, port, timeout)
    return {"status": "open" if ok else "closed", "ms": round((time.monotonic() - t0) * 1000) if ok else None}


class ArednServices:
    def __init__(self) -> None:
        self.data: dict | None = None
        self.last_error: str | None = None
        self.enabled = True
        self._config: dict = {}
        self._task: asyncio.Task | None = None
        self.cache_path: str | None = None
        self.refresh_s = REFRESH_S
        self.running = False
        self._cam = cams.ArednCameras()          # its per-camera check

    def configure(self, config: dict) -> None:
        self._config = config or {}
        rd = self._config.get("radio_directory") or {}
        cfg = rd.get("aredn_services") or {}
        self.enabled = bool(cfg.get("enabled", True)) and bool((rd.get("aredn_cameras") or {}).get("enabled", True))
        try:
            self.refresh_s = max(6 * 3600, int(float(cfg.get("refresh_hours", REFRESH_S / 3600)) * 3600))
        except (TypeError, ValueError):
            self.refresh_s = REFRESH_S
        cams.service.configure(config)          # camera cache path + last cameras for the camera layer
        db_path = (self._config.get("database") or {}).get("path", "ech.db")
        base = os.path.dirname(os.path.abspath(db_path)) if db_path != ":memory:" else os.getcwd()
        self.cache_path = os.path.join(base, "aredn_services.json")
        try:
            with open(self.cache_path, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict) and "services" in d:
                self.data = d
        except (OSError, ValueError):
            pass

    def _hosts_url(self) -> str:
        rd = self._config.get("radio_directory") or {}
        return str((rd.get("aredn_services") or {}).get("hosts_url")
                   or (rd.get("meshphone") or {}).get("hosts_url")
                   or (rd.get("aredn_discovery") or {}).get("sysinfo_url") or "")

    async def start(self, config: dict) -> None:
        self.configure(config)
        if self.enabled and self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        age = time.time() - float((self.data or {}).get("generated_at") or 0)
        await asyncio.sleep(max(FIRST_DELAY_S, self.refresh_s - age))
        while True:
            wait = self.refresh_s
            try:
                await self.refresh()
            except Exception as exc:
                self.last_error = f"{exc.__class__.__name__}: {exc}"
                cams.service.last_error = self.last_error
                log.warning("AREDN services refresh failed (retrying in 30 min): %s", self.last_error)
                wait = RETRY_S
            await asyncio.sleep(wait)

    async def _feed(self, client, link: str, ip: str, port: int, kind: str) -> dict | None:
        u = urlsplit(link)
        host = u.hostname or ""
        hdr = {"Host": host if port in (80, 443) else f"{host}:{port}"}
        for path in feed_paths(link, kind):
            url = f"{u.scheme}://{ip}:{port}{path}"
            try:
                r = await client.get(url, timeout=8, headers=hdr, follow_redirects=False)
                if r.status_code != 200 or "json" not in r.headers.get("content-type", "") and not r.text[:1] in "[{":
                    continue
                n = feed_count(kind, r.json())
                if n is not None:
                    return {"path": path, "count": n}
            except Exception:
                continue
        return None

    async def _check(self, client, s: dict, hosts: list[dict], cat: str) -> dict:
        link = str(s.get("link") or "")
        if cat == "time" and "://" in link:
            # NTP is UDP: ask the host for the time (a web link is usually its status page)
            scheme, ip, port = cams.link_target(link, hosts)
            name = _TAG_RE.sub("", str(s.get("name") or "")).strip()[:80]
            web = scheme in ("http", "https")
            c = {"name": name, "tag": tag_of(s), "link": cams.mask_secrets(link)[:300], "href": link[:500] if web else "",
                 "scheme": scheme, "ip": ip, "port": 123 if web or not port else port, "node_ip": s.get("ip") or "",
                 "open_ports": [], "other_apps": [], "kind": "ntp", "software": "", "view": ""}
            if ip:
                c.update(await ntp_check(ip, c["port"]))
            else:
                c["status"] = "unreachable"
            c["category"] = cat
            return c
        if cat == "camera" or re.match(r"(https?|rtsp)://", link, re.I):
            c = await self._cam._check(client, s, hosts, probe_ports=(cat == "camera"))
        else:
            scheme, ip, port = cams.link_target(link, hosts) if "://" in link else ("", "", 0)
            name = _TAG_RE.sub("", str(s.get("name") or "")).strip()[:80]
            c = {"name": name, "tag": tag_of(s), "link": cams.mask_secrets(link)[:300], "href": "",
                 "scheme": scheme, "ip": ip, "port": port, "node_ip": s.get("ip") or "", "open_ports": [],
                 "other_apps": [], "kind": scheme or "listed", "software": "", "view": ""}
            if ip and port:
                c.update(await tcp_check(ip, port))
            else:
                c["status"] = "listed" if not link else "unreachable"
        c["category"] = cat
        if cat in ("adsb", "ais") and c.get("status") in ("open", "password") and c.get("scheme", "").startswith("http"):
            f = await self._feed(client, link, c["ip"], c["port"], cat)
            if f:
                c["feed"] = f
        return c

    async def refresh(self) -> dict | None:
        import httpx
        from ech.core.meshphone_map import service as mp_map
        url = self._hosts_url()
        if not url:
            raise RuntimeError("set radio_directory.meshphone.hosts_url (or aredn_discovery.sysinfo_url)")
        for q in ("hosts=1", "services=1"):
            if q not in url:
                url += ("&" if "?" in url else "?") + q
        self.running = cams.service.running = True
        try:
            async with httpx.AsyncClient(follow_redirects=True, verify=False,
                                         headers={"User-Agent": "SignalMatrix"}) as client:
                j = (await client.get(url, timeout=60)).json()
                hosts = [h for h in j.get("hosts") or [] if isinstance(h, dict)]
                todo, seen = [], set()
                for s in j.get("services") or []:
                    if not isinstance(s, dict):
                        continue
                    cat = categorize(s)
                    key = (str(s.get("name") or ""), str(s.get("link") or ""))
                    if cat is None or key in seen:
                        continue
                    if cat == "camera" and not re.match(r"(https?|rtsp)://", key[1], re.I):
                        cat = "other"
                    seen.add(key)
                    todo.append((s, cat))
                todo = todo[:MAX_SERVICES]
                sem = asyncio.Semaphore(12)

                async def one(s, cat):
                    async with sem:
                        try:
                            return await self._check(client, s, hosts, cat)
                        except Exception as exc:
                            log.debug("AREDN services: check of %s failed: %s", s.get("name"), exc)
                            return None
                checked = [c for c in await asyncio.gather(*(one(s, c) for s, c in todo)) if c]
                node_names = {str(h.get("ip") or ""): str(h.get("name") or "") for h in hosts}
                sem2 = asyncio.Semaphore(8)

                async def ninfo(ip):
                    async with sem2:
                        return ip, await mp_map._node_info(client, ip)
                nodes = dict(await asyncio.gather(*(ninfo(ip) for ip in {c["node_ip"] for c in checked if c["node_ip"]})))
                for n in nodes.values():
                    if n.get("lat") is not None:
                        n["place"] = await mp_map._place(client, n["lat"], n["lon"])
        finally:
            self.running = cams.service.running = False
        out = []
        for c in checked:
            n = nodes.get(c["node_ip"]) or {}
            out.append({**c, "node": n.get("name") or node_names.get(c["node_ip"], ""),
                        "lat": n.get("lat"), "lon": n.get("lon"), "grid": n.get("grid", ""),
                        "place": n.get("place", "")})
        now = time.time()

        def summary(items):
            st: dict[str, int] = {}
            for c in items:
                st[c.get("status") or "unknown"] = st.get(c.get("status") or "unknown", 0) + 1
            return {"status": st, "located": sum(1 for c in items if c["lat"] is not None)}
        by_cat = {k: sum(1 for c in out if c["category"] == k) for k in CATEGORY_KEYS}
        data = {"generated_at": now, "services": out,
                "categories": [{"key": k, "label": l, "icon": i, "count": by_cat[k]} for k, l, i in CATEGORIES],
                "summary": {**summary(out), "services": len(out),
                            "feeds": sum(1 for c in out if c.get("feed"))}}
        self.data, self.last_error = data, None
        cam_list = [c for c in out if c["category"] == "camera"]
        cams.service.data = {"generated_at": now, "cameras": cam_list,
                             "summary": {"cameras": len(cam_list), "located": summary(cam_list)["located"],
                                         "status": summary(cam_list)["status"]}}
        cams.service.last_error = None
        log.info("AREDN services: %d checked, %d located, %d data feeds, by category %s",
                 len(out), data["summary"]["located"], data["summary"]["feeds"],
                 {k: v for k, v in by_cat.items() if v})
        for path, obj in ((self.cache_path, data), (cams.service.cache_path, cams.service.data)):
            if path:
                try:
                    tmp = path + ".tmp"
                    with open(tmp, "w", encoding="utf-8") as f:
                        json.dump(obj, f)
                    os.replace(tmp, path)
                except OSError as exc:
                    log.debug("AREDN services: cache write failed: %s", exc)
        return data


service = ArednServices()
