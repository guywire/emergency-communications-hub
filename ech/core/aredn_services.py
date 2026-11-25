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

The mesh is checked one region (a quarter of the address space) at a time, one
region every refresh_hours / 4, and a region another instance checked within
the last 4 hours is taken from it after verification: see aredn_sharing.py (the same exchange as the
standalone aredn-services-map, so ECH and its copies share with each other).

Config (radio_directory.aredn_services, all optional):
  enabled         bool   default true
  refresh_hours   float  default 24 (at least 6): each region once per refresh
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
from ech.core import aredn_sharing as sharing

log = logging.getLogger(__name__)

REFRESH_S = 24 * 3600
FIRST_DELAY_S = 300
RETRY_S = 1800
# A region taken from another copy comes due again this much later than the
# original copy's (random in this range), so that copy checks first and the
# others take its fresh result instead of all checking at the same moment.
ADOPTED_DEFER_S = (3600, 3 * 3600)
PEER_POLL_S = 3600          # look for new copies (and whether the known ones answer) this often
MAX_SERVICES = 2500
# The name other copies see when `station` isn't set (plus a key fingerprint,
# so two copies reading the same node never share a name). "" = the node's name.
DEFAULT_STATION = "SignalMatrix"

# (key, label, icon). Order = precedence when a service matches several.
# (key, label, icon, group): the checklist shows them in this order, under
# their group's heading. MeshPhone offices & trunks (the measured directory,
# not a service kind) sits at the top of Phones on the page.
GROUPS = ["Phones", "Emergency & coordination", "Messaging", "Radio", "Cameras & sensors", "Network"]
CATEGORIES = [
    ("voip", "Other phones & PBXs", "📞", "Phones"),
    ("dispatch", "CAD / dispatch", "🚨", "Emergency & coordination"),
    ("eoc", "EOC / agencies", "🏛", "Emergency & coordination"),
    ("tak", "TAK", "🎯", "Emergency & coordination"),
    ("collab", "Collaboration", "📝", "Emergency & coordination"),
    ("files", "File sharing", "📁", "Emergency & coordination"),
    ("map", "Maps", "🗺", "Emergency & coordination"),
    ("wiki", "Wikis / info", "📖", "Emergency & coordination"),
    ("chat", "Chat", "💬", "Messaging"),
    ("mail", "Mail", "✉", "Messaging"),
    ("winlink", "Winlink", "📧", "Messaging"),
    ("packet", "Packet / BBS", "📟", "Messaging"),
    ("aprs", "APRS", "📍", "Radio"),
    ("meshradio", "Meshtastic / MeshCore", "📡", "Radio"),
    ("radio", "Radio / SDR / AllStar", "📻", "Radio"),
    ("hamtools", "Ham tools / logging", "🎚", "Radio"),
    ("camera", "Cameras", "📷", "Cameras & sensors"),
    ("weather", "Weather", "🌦", "Cameras & sensors"),
    ("adsb", "ADS-B receivers", "✈", "Cameras & sensors"),
    ("ais", "AIS receivers", "⚓", "Cameras & sensors"),
    ("satellite", "Satellite receivers", "🛰", "Cameras & sensors"),
    ("power", "Power / solar", "🔋", "Cameras & sensors"),
    ("internet", "Internet gateways", "🌐", "Network"),
    ("time", "Time servers", "🕒", "Network"),
    ("status", "Mesh status / monitoring", "📊", "Network"),
    ("speedtest", "Network tests", "⏱", "Network"),
    ("infra", "Network gear", "🔧", "Network"),
    ("other", "Other", "•", "Network"),
]
CATEGORY_KEYS = [c[0] for c in CATEGORIES]
# kinds broken out of "other" (v1.1.142 / 0.3.0): results saved as "other"
# move to them without a new check, as they were checked the same way
OTHER_SPLIT = {"dispatch", "eoc", "files", "voip", "collab", "hamtools", "status", "speedtest", "infra"}

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
    # kinds broken out of "other": checked after the ones above, in this order
    "dispatch": r"\bcad\b|dispatch|\btickets?\b|incident|\bics[- ]?\d{3}\b|net ?logger|net ?control|check-?in|sahana|\bd4h\b",
    "files": r"nextcloud|owncloud|seafile|file ?browser|file ?server|\bfiles?\b|\bs?ftp\b|\bnas\b|samba|\bsmb\b|syncthing|\bmirror\b|downloads?\b|\brepo\b",
    "voip": r"\bpbx\b|freepbx|raspbx|asterisk|\bvoip\b|\bsip\b|ip ?phone|hamsoverip|meshphone|dialing",
    "collab": r"etherpad|ethercalc|mattermost|rocket\.?chat|cryptpad|hedgedoc|discourse|\bforum\b",
    "hamtools": r"ham ?clock|hamdash|ham ?dashboard|dx ?cluster|dxspider|pskreporter|cloudlog|wavelog|logbook|log ?server|\bqrz\b|contest",
    "status": r"mesh ?map|mesh ?stats|nodographer|grafana|uptime ?kuma|zabbix|librenms|smokeping|prometheus|portainer|arednstatus|\bstatus\b|dashboard|monitor",
    "speedtest": r"iperf|speed ?test|librespeed|openspeedtest",
    "infra": r"edge ?switch|\bswitch\b|\brouter\b|cambium|mikrotik|ubiquiti|\buisp\b|\bhap\b|\bsector\b|\bptm?p\b|openwrt|pfsense|opnsense|firewall|\bvpn\b|tunnel|wireguard|proxmox",
    "eoc": r"\beoc\b|emergency op|county|city ?hall|\bfire\b|sheriff|police|hospital|red ?cross|\bares\b|\braces\b|\bcert\b|salvation army",
}
_TAG_CAT = {"camera": "camera", "video": "camera", "chat": "chat", "weather": "weather", "time": "time",
            "mail": "mail", "internet": "internet", "power": "power", "solar": "power", "battery": "power",
            "map": "map", "wiki": "wiki", "winlink": "winlink", "cad": "dispatch", "dispatch": "dispatch",
            "eoc": "eoc", "files": "files", "file": "files", "nas": "files", "pbx": "voip", "voip": "voip",
            "status": "status", "monitor": "status", "speedtest": "speedtest", "network": "infra"}
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
    # the newer kinds look at the name and the link's path, not its host name:
    # mesh host names carry words like hap, router, fire or county for any service
    try:
        where = urlsplit(link).path if "://" in link else link
    except ValueError:
        where = ""
    text = f"{name} {where}"
    for key in ("dispatch", "voip", "files", "collab", "hamtools", "status", "speedtest", "infra", "eoc"):
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
            cams.note_traffic(1, 48)
            transport.sendto(b"\x1b" + 47 * b"\0")          # LI 0, version 3, mode 3 (client)
            data = await asyncio.wait_for(proto.fut, timeout)
            cams.note_traffic(0, len(data))
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


def base_record(s: dict, hosts: list[dict], cat: str) -> dict:
    """Who and where a service is, from our own copy of the mesh's list (never
    from a peer): its id, name, link, address and category."""
    link = str(s.get("link") or "")
    scheme, ip, port = cams.link_target(link, hosts) if "://" in link else ("", "", 0)
    if cat == "time" and "://" in link and (scheme in ("http", "https") or not port):
        port = 123
    return {"id": sharing.svc_id(s), "name": _TAG_RE.sub("", str(s.get("name") or "")).strip()[:80],
            "tag": tag_of(s), "link": cams.mask_secrets(link)[:300],
            "href": link[:500] if re.match(r"(https?|rtsp)://", link, re.I) else "",
            "scheme": scheme, "ip": ip, "port": port, "node_ip": s.get("ip") or "", "category": cat}


class ArednServices:
    def __init__(self) -> None:
        self.data: dict | None = None
        self.last_error: str | None = None
        self.enabled = True
        self._config: dict = {}
        self._task: asyncio.Task | None = None
        self._peer_task: asyncio.Task | None = None
        self.cache_path: str | None = None
        self.region_dir: str | None = None
        self.refresh_s = REFRESH_S
        self.running = False
        self.running_region: int | None = None
        self._cam = cams.ArednCameras()          # its per-camera check
        self.regions: dict[int, dict] = {}       # region -> its last result (own or shared)
        self.identity: sharing.Identity | None = None
        self.trust: sharing.Trust | None = None
        self.station = ""
        self._node_name = ""
        self.share = {"enabled": True, "max_age_s": sharing.MAX_AGE_S, "peers": []}
        self.peer_log: list[dict] = []
        self.peer_seen: dict[str, dict] = {}          # Host -> last contact with that copy
        self._self_hosts: set[str] = set()            # targets that turned out to be us
        self._peer_status_path: str | None = None
        self.peer_targets: list[tuple[str, str]] = []   # other copies found on the mesh (tiles use them too)
        self._targets_path: str | None = None
        self._last_pass = 0.0
        self._fail_until: dict[int, float] = {}

    def configure(self, config: dict) -> None:
        self._config = config or {}
        rd = self._config.get("radio_directory") or {}
        cfg = rd.get("aredn_services") or {}
        self.enabled = bool(cfg.get("enabled", True)) and bool((rd.get("aredn_cameras") or {}).get("enabled", True))
        try:
            self.refresh_s = max(6 * 3600, int(float(cfg.get("refresh_hours", REFRESH_S / 3600)) * 3600))
        except (TypeError, ValueError):
            self.refresh_s = REFRESH_S
        sh = cfg.get("sharing") or self._config.get("sharing") or {}
        try:
            max_age = min(24.0, max(0.5, float(sh.get("max_age_hours", sharing.MAX_AGE_S / 3600)))) * 3600
        except (TypeError, ValueError):
            max_age = sharing.MAX_AGE_S
        self.share = {"enabled": bool(sh.get("enabled", True)), "max_age_s": max_age,
                      "peers": [str(p) for p in (sh.get("peers") or []) if p]}
        self.station = str(cfg.get("station") or self._config.get("station") or "")[:63]
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
        self.region_dir = os.path.join(base, "aredn_regions")
        os.makedirs(self.region_dir, exist_ok=True)
        self.regions = {}
        for k in range(sharing.REGIONS):
            try:
                with open(os.path.join(self.region_dir, f"r{k + 1}.json"), encoding="utf-8") as f:
                    st = json.load(f)
                if isinstance(st, dict) and isinstance(st.get("services"), list):
                    self.regions[k] = st
            except (OSError, ValueError):
                pass
        self._last_pass = max([float(st.get("done_at") or 0) for st in self.regions.values()] or [0.0])
        if self.regions or self.data:
            try:
                self._rebuild()                 # saved results under today's kinds
            except Exception as exc:
                log.debug("AREDN services: rebuild at start failed: %s", exc)
        self.identity = sharing.Identity(os.path.join(base, "aredn_identity.key"))
        self.trust = sharing.Trust(os.path.join(base, "aredn_peers.json"))
        self._targets_path = os.path.join(base, "aredn_peer_targets.json")
        try:
            with open(self._targets_path, encoding="utf-8") as f:
                self.peer_targets = [tuple(t) for t in json.load(f) if isinstance(t, list) and len(t) == 2]
        except (OSError, ValueError, TypeError):
            self.peer_targets = []
        self._peer_status_path = self._targets_path.replace("peer_targets", "peer_status")
        try:
            with open(self._peer_status_path, encoding="utf-8") as f:
                d = json.load(f)
            self.peer_seen = {str(h): v for h, v in (d.get("peers") or {}).items() if isinstance(v, dict)}
            self._self_hosts = {str(h) for h in d.get("self") or []}
        except (OSError, ValueError, TypeError, AttributeError):
            self.peer_seen, self._self_hosts = {}, set()

    def _hosts_url(self) -> str:
        rd = self._config.get("radio_directory") or {}
        return str((rd.get("aredn_services") or {}).get("hosts_url")
                   or (rd.get("meshphone") or {}).get("hosts_url")
                   or (rd.get("aredn_discovery") or {}).get("sysinfo_url") or "")

    async def start(self, config: dict) -> None:
        self.configure(config)
        if self.enabled and self._task is None:
            self._task = asyncio.create_task(self._loop())
            self._peer_task = asyncio.create_task(self._peer_loop())

    async def _peer_loop(self) -> None:
        await asyncio.sleep(FIRST_DELAY_S + 60)
        while True:
            try:
                await self.find_peers()
            except Exception as exc:
                log.debug("AREDN services: looking for other copies failed: %s", exc)
            await asyncio.sleep(PEER_POLL_S)

    async def find_peers(self) -> int:
        """Look for other copies of this map: the mesh's service list (one
        request to the node) and a small info request to each copy. Nothing is
        checked; a region still takes a shared result only when it comes due.
        Returns how many other copies answered."""
        import httpx
        url = self._hosts_url()
        if not url or not self.share.get("enabled", True) or not self.identity:
            return 0
        for q in ("hosts=1", "services=1"):
            if q not in url:
                url += ("&" if "?" in url else "?") + q
        async with httpx.AsyncClient(follow_redirects=True, verify=False,
                                     headers={"User-Agent": "SignalMatrix"}) as client:
            j = (await client.get(url, timeout=60)).json()
            hosts = [h for h in j.get("hosts") or [] if isinstance(h, dict)]
            mesh_services = [s for s in j.get("services") or [] if isinstance(s, dict)]
            if hosts and mesh_services:
                self._set_targets(sharing.peer_targets(mesh_services, hosts, self.share["peers"]))
            targets = list(self.peer_targets)
            if not targets:
                return 0
            infos = await asyncio.gather(*(sharing.get_json(client, b, h, sharing.INFO_PATH, sharing.MAX_INFO_BYTES)
                                           for b, h in targets))
        self._record_peers(targets, infos, time.time())
        return sum(1 for h in self._others() if (self.peer_seen.get(h) or {}).get("answered"))

    # ── schedule: one region at a time, spread over the day ─────────────────
    def next_region(self) -> tuple[int, float]:
        """(region, when): the region due soonest, no sooner than refresh/4
        after the last pass (a failed pass retries after RETRY_S)."""
        best = None
        for k in range(sharing.REGIONS):
            st = self.regions.get(k) or {}
            due = max(float(st.get("scanned_at") or 0) + self.refresh_s + float(st.get("defer_s") or 0),
                      self._fail_until.get(k, 0.0))
            if best is None or due < best[1]:
                best = (k, due)
        k, due = best
        if k not in self._fail_until:
            due = max(due, self._last_pass + self.refresh_s / sharing.REGIONS)
        return k, due

    async def _loop(self) -> None:
        first = True
        while True:
            k, due = self.next_region()
            wait = max(0.0, due - time.time())
            if first:
                wait, first = max(wait, FIRST_DELAY_S), False
            await asyncio.sleep(wait)
            try:
                await self.refresh_region(k)
                self._fail_until.pop(k, None)
            except Exception as exc:
                self.last_error = f"region {k + 1}: {exc.__class__.__name__}: {exc}"
                cams.service.last_error = self.last_error
                log.warning("AREDN services: %s (retrying in 30 min)", self.last_error)
                self._fail_until[k] = time.time() + RETRY_S
                await asyncio.sleep(RETRY_S)          # nothing else until then either

    # ── checking ────────────────────────────────────────────────────────────
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

    @staticmethod
    def items(mesh_services: list[dict]) -> list[tuple[dict, str]]:
        """(service, category) for every service to check, phones left out."""
        todo, seen = [], set()
        for s in mesh_services:
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
        return todo[:MAX_SERVICES]

    async def _check_and_locate(self, client, todo: list[tuple[dict, str]], hosts: list[dict]) -> list[dict]:
        sem = asyncio.Semaphore(12)

        async def one(s, cat):
            async with sem:
                try:
                    return {**await self._check(client, s, hosts, cat), **base_record(s, hosts, cat)}
                except Exception as exc:
                    log.debug("AREDN services: check of %s failed: %s", s.get("name"), exc)
                    return None
        checked = [c for c in await asyncio.gather(*(one(s, c) for s, c in todo)) if c]
        return await self._locate(client, checked, hosts)

    async def _locate(self, client, recs: list[dict], hosts: list[dict]) -> list[dict]:
        from ech.core.meshphone_map import service as mp_map
        node_names = {str(h.get("ip") or ""): str(h.get("name") or "") for h in hosts}
        sem = asyncio.Semaphore(8)

        async def ninfo(ip):
            async with sem:
                return ip, await mp_map._node_info(client, ip)
        nodes = dict(await asyncio.gather(*(ninfo(ip) for ip in {c["node_ip"] for c in recs if c["node_ip"]})))
        for n in nodes.values():
            if n.get("lat") is not None:
                n["place"] = await mp_map._place(client, n["lat"], n["lon"])
        out = []
        for c in recs:
            n = nodes.get(c["node_ip"]) or {}
            out.append({**c, "node": n.get("name") or node_names.get(c["node_ip"], ""),
                        "lat": n.get("lat"), "lon": n.get("lon"), "grid": n.get("grid", ""),
                        "place": n.get("place", "")})
        return out

    # ── one region ──────────────────────────────────────────────────────────
    async def refresh_region(self, k: int) -> dict:
        """Region k: a fresh, verified result from another instance if there is
        one, else checked from here. Updates the merged services and cameras."""
        import httpx
        url = self._hosts_url()
        if not url:
            raise RuntimeError("set radio_directory.meshphone.hosts_url (or aredn_discovery.sysinfo_url)")
        for q in ("hosts=1", "services=1"):
            if q not in url:
                url += ("&" if "?" in url else "?") + q
        self.running = cams.service.running = True
        self.running_region = k
        traffic = {"requests": 0, "bytes": 0}             # what this check moved, for the About page
        tok = cams.TRAFFIC.set(traffic)
        try:
            async with httpx.AsyncClient(transport=cams.counting_transport(), follow_redirects=True,
                                         headers={"User-Agent": "SignalMatrix"}) as client:
                j = (await client.get(url, timeout=60)).json()
                hosts = [h for h in j.get("hosts") or [] if isinstance(h, dict)]
                mesh_services = [s for s in j.get("services") or [] if isinstance(s, dict)]
                if not hosts or not mesh_services:
                    raise RuntimeError(f"the node's sysinfo listed {len(hosts)} hosts and {len(mesh_services)} services")
                mine = [(s, c) for s, c in self.items(mesh_services) if sharing.region_of(s.get("ip")) == k]
                self._set_targets(sharing.peer_targets(mesh_services, hosts, self.share["peers"]))
                before = int((self.regions.get(k) or {}).get("count") or 0)
                if not mine and before >= 5:
                    raise RuntimeError(f"the node's list has none of the region's {before} services now; kept the last result")
                self._node_name = str(j.get("node") or "")
                station = self.station_name()
                now = time.time()
                got = None
                if self.share["enabled"] and mine:
                    got = await self._from_peers(client, k, mine, hosts, mesh_services, now)
                if got:
                    recs, state = got
                else:
                    recs = await self._check_and_locate(client, mine, hosts)
                    state = {"doc": sharing.make_doc(self.identity, station, k, now, recs), "scanned_at": now,
                             "station": station, "own": True, "note": f"checked here: {len(recs)} services"}
        finally:
            cams.TRAFFIC.reset(tok)
            self.running = cams.service.running = False
            self.running_region = None
        state.update(region=k, services=recs, count=len(recs), done_at=time.time(), traffic=traffic)
        self.regions[k] = state
        self._last_pass = state["done_at"]
        if self.region_dir:
            try:
                path = os.path.join(self.region_dir, f"r{k + 1}.json")
                with open(path + ".tmp", "w", encoding="utf-8") as f:
                    json.dump(state, f)
                os.replace(path + ".tmp", path)
            except OSError as exc:
                log.debug("AREDN services: region file write failed: %s", exc)
        self.last_error = None
        log.info("AREDN services, region %d (%s): %s", k + 1, sharing.region_label(k), state["note"])
        self._rebuild()
        return state

    def station_name(self) -> str:
        """`station` as set, else the default name plus a key fingerprint."""
        if self.station:
            return self.station
        import hashlib
        fp = hashlib.sha256((self.identity.key if self.identity else "").encode()).hexdigest()[:6]
        base = re.sub(r"[^\w.-]", "", DEFAULT_STATION or self._node_name)[:50] or "map"
        return f"{base}/{fp}"

    def _set_targets(self, targets: list[tuple[str, str]]) -> None:
        self.peer_targets = targets
        if self._targets_path:
            try:
                with open(self._targets_path, "w", encoding="utf-8") as f:
                    json.dump(targets, f)
            except OSError:
                pass

    @staticmethod
    def _own_ips() -> set[str]:
        """This box's addresses: a target at one of them is us."""
        import socket
        ips = {"127.0.0.1"}
        try:
            import psutil
            ips |= {a.address for v in psutil.net_if_addrs().values() for a in v if a.family == socket.AF_INET}
        except Exception:
            try:
                ips |= {i[4][0] for i in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)}
            except OSError:
                pass
        return ips

    def _others(self) -> list[str]:
        """Hosts of the targets that aren't this box."""
        from urllib.parse import urlsplit
        own = self._own_ips()
        return [h for b, h in self.peer_targets if h not in self._self_hosts and urlsplit(b).hostname not in own]

    def _record_peers(self, targets, infos, now: float) -> None:
        """Remember which other copies answered, for the About page."""
        for (b, h), info in zip(targets, infos):
            if isinstance(info, dict) and info.get("key") == self.identity.key:
                self._self_hosts.add(h)                      # our own node advertises us
                self.peer_seen.pop(h, None)
                continue
            prev = self.peer_seen.get(h) or {}
            ok = isinstance(info, dict) and info.get("app") == sharing.APP
            self.peer_seen[h] = {"at": now, "answered": ok,
                                 "station": str(info.get("station") or "")[:63] if ok else prev.get("station", ""),
                                 "version": str(info.get("version") or "")[:20] if ok else prev.get("version", ""),
                                 "last_answer": now if ok else prev.get("last_answer")}
        live = {h for _, h in targets}
        self.peer_seen = {h: v for h, v in self.peer_seen.items() if h in live}
        if self._peer_status_path:
            try:
                with open(self._peer_status_path + ".tmp", "w", encoding="utf-8") as f:
                    json.dump({"peers": self.peer_seen, "self": sorted(self._self_hosts)}, f)
                os.replace(self._peer_status_path + ".tmp", self._peer_status_path)
            except OSError:
                pass

    def sharing_meta(self) -> dict:
        """This station, the next check, and the other copies it works with."""
        k, due = self.next_region()
        others = self._others()
        copies = [h for h in others if (self.peer_seen.get(h) or {}).get("last_answer")]
        used = sorted(({"station": v.get("station", ""), "at": v.get("at")}
                       for v in ((self.trust.seen if self.trust else {}) or {}).values() if isinstance(v, dict)),
                      key=lambda x: -(x["at"] or 0))
        return {"station": self.station_name(), "enabled": bool(self.share.get("enabled", True)),
                "next": {"region": k + 1, "at": due}, "found": len(copies), "candidates": len(others),
                "peers": [{"host": h, **self.peer_seen[h]} for h in copies],
                "used": used[:10]}

    def _note(self, k: int, peer: str, msg: str) -> None:
        self.peer_log = (self.peer_log + [{"at": time.time(), "region": k + 1, "peer": peer, "note": msg}])[-50:]
        log.info("AREDN services, region %d: %s: %s", k + 1, peer, msg)

    async def _from_peers(self, client, k, mine, hosts, mesh_services, now):
        """A shared result for region k that passes every check, as
        (records, state); None to check the region from here."""
        ours, byid = {}, {}
        for s, cat in mine:
            b = base_record(s, hosts, cat)
            ours[b["id"]], byid[b["id"]] = b, (s, cat)
        targets = self.peer_targets
        if not targets:
            return None
        infos = await asyncio.gather(*(sharing.get_json(client, b, h, sharing.INFO_PATH, sharing.MAX_INFO_BYTES)
                                       for b, h in targets))
        self._record_peers(targets, infos, now)
        cands = []
        for (b, h), info in zip(targets, infos):
            if (not isinstance(info, dict) or info.get("app") != sharing.APP or info.get("proto") != sharing.PROTO
                    or info.get("key") == self.identity.key):
                continue
            lay = (info.get("layers") or {}).get(sharing.LAYER)
            if not isinstance(lay, dict) or lay.get("scheme") != sharing.SCHEME:
                continue
            r = (lay.get("regions") or {}).get(str(k + 1)) or {}
            at = r.get("scanned_at") if isinstance(r, dict) else None
            if isinstance(at, (int, float)) and -sharing.FUTURE_SKEW_S <= now - at <= self.share["max_age_s"]:
                cands.append((at, b, h))
        cands.sort(reverse=True)
        for at, b, h in cands[:3]:
            doc = await sharing.get_json(client, b, h, sharing.REGION_PATH.format(k=k + 1), sharing.MAX_DOC_BYTES)
            try:
                p = sharing.open_doc(doc, k, now, self.share["max_age_s"], self.identity.key)
                st, key = p["scanner"]["station"], p["scanner"]["key"]
                self.trust.check(st, key, now)
                meas = sharing.measured_from(p, ours)
                if len(meas) < sharing.MIN_COVERAGE * len(ours):
                    raise sharing.Rejected(f"covers {len(meas)} of the region's {len(ours)} services")
            except sharing.Rejected as exc:
                self._note(k, b, f"not used: {exc}")
                continue
            # check a random sample again from here
            checkable = [i for i in meas if ours[i]["ip"] or ours[i]["node_ip"]]
            sample = sharing.sample_ids(checkable) if checkable else []
            fresh = {c["id"]: c for c in await self._check_and_locate(client, [byid[i] for i in sample], hosts)}
            agree = sum(1 for i in sample if i in fresh and sharing.agrees(meas[i], fresh[i]))
            if sample and agree < sharing.MIN_AGREE * len(sample):
                reason = f"spot check: only {agree} of {len(sample)} agree"
                self.trust.reject(st, key, now, reason)
                self._note(k, b, f"not used, {st} not trusted for 7 days: {reason}")
                continue
            missing = [byid[i] for i in ours if i not in meas]
            own = await self._check_and_locate(client, missing, hosts) if missing else []
            recs = [{**ours[i], **meas[i]} for i in ours if i in meas] + own
            note = (f"from {st}, checked {(now - p['scanned_at']) / 3600:.1f} h earlier: {agree} of {len(sample)} "
                    f"spot checks agree" + (f", {len(missing)} checked here" if missing else ""))
            self.trust.accept(st, key, now, note)
            self._note(k, b, note)
            import random
            return recs, {"doc": doc, "scanned_at": p["scanned_at"], "station": st, "own": False, "note": note,
                          "defer_s": random.randint(*ADOPTED_DEFER_S)}
        return None

    async def refresh(self) -> dict | None:
        """Every region now (one after the other)."""
        for k in range(sharing.REGIONS):
            await self.refresh_region(k)
        return self.data

    # ── the merged view ─────────────────────────────────────────────────────
    def regions_meta(self) -> list[dict]:
        out = []
        for k in range(sharing.REGIONS):
            st = self.regions.get(k) or {}
            out.append({"region": k + 1, "range": sharing.region_label(k), "scanned_at": st.get("scanned_at"),
                        "station": st.get("station", ""), "own": st.get("own"), "note": st.get("note", ""),
                        "count": st.get("count", 0), "running": self.running_region == k,
                        "traffic": st.get("traffic")})
        return out

    def _rebuild(self) -> None:
        out = []
        for k in sorted(self.regions):
            out += self.regions[k]["services"]
        # until every region has had its turn, keep the last full pass for the rest
        out += [c for c in (self.data or {}).get("services", [])
                if sharing.region_of(c.get("node_ip")) not in self.regions]
        for c in out:
            if c.get("category") == "other":                # saved before a kind was broken out of "other"
                cat = categorize({"name": c.get("name", "") + (f" [{c['tag']}]" if c.get("tag") else ""),
                                  "link": c.get("link", "")})
                if cat in OTHER_SPLIT:
                    c["category"] = cat

        def summary(items):
            st: dict[str, int] = {}
            for c in items:
                st[c.get("status") or "unknown"] = st.get(c.get("status") or "unknown", 0) + 1
            return {"status": st, "located": sum(1 for c in items if c.get("lat") is not None)}
        by_cat = {k: sum(1 for c in out if c.get("category") == k) for k in CATEGORY_KEYS}
        times = [st["scanned_at"] for st in self.regions.values() if st.get("scanned_at")]
        now = max(times) if times else float((self.data or {}).get("generated_at") or time.time())
        data = {"generated_at": now, "services": out, "regions": self.regions_meta(),
                "categories": [{"key": k, "label": l, "icon": i, "group": g, "count": by_cat[k]}
                               for k, l, i, g in CATEGORIES],
                "summary": {**summary(out), "services": len(out),
                            "feeds": sum(1 for c in out if c.get("feed"))}}
        self.data = data
        cam_list = [c for c in out if c.get("category") == "camera"]
        cams.service.data = {"generated_at": now, "cameras": cam_list,
                             "summary": {"cameras": len(cam_list), "located": summary(cam_list)["located"],
                                         "status": summary(cam_list)["status"]}}
        cams.service.last_error = None
        for path, obj in ((self.cache_path, data), (cams.service.cache_path, cams.service.data)):
            if path:
                try:
                    tmp = path + ".tmp"
                    with open(tmp, "w", encoding="utf-8") as f:
                        json.dump(obj, f)
                    os.replace(tmp, path)
                except OSError as exc:
                    log.debug("AREDN services: cache write failed: %s", exc)

    def layer_info(self) -> dict:
        """The services layer's entry in /peer/v1/info."""
        return {"scheme": sharing.SCHEME,
                "regions": {str(k + 1): {"scanned_at": st.get("scanned_at"), "station": st.get("station", ""),
                                         "count": st.get("count", 0)}
                            for k, st in self.regions.items() if st.get("doc")}}


def peer_info(version: str, layers: dict) -> dict:
    """What /peer/v1/info tells the other copies: who this is and its layers."""
    return {"app": sharing.APP, "proto": sharing.PROTO, "version": version, "station": service.station_name(),
            "key": service.identity.key if service.identity else "", "layers": layers}


service = ArednServices()
