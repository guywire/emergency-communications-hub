"""
ech/core/meshphone_map.py
-------------------------
A MeshPhone network map where the WT0F MeshPhone database is only the guide
(which offices and trunks to look for) and the facts are polled from the mesh:
the PBXs themselves and the AREDN nodes they sit behind.

Once a day by default (map_refresh_hours; the directory copy follows it):

  1. Mesh host list. The local AREDN node's sysinfo (hosts=1&services=1)
     lists every host on the mesh, grouped by the node it sits behind, and
     every advertised service.
  2. PBX candidates: the directory's address for each office, PBX-named hosts
     of its admin's callsign, and any other host on the mesh that looks like
     a phone system by name ("k1abc-pbx", "...-raspbx", "...meshphone...") or
     by the service linking to it ("WW4WWW PBX (MeshPhone 770-00xx)").
     Handsets are skipped by name. Never a sweep of every host.
  3. Confirmation: an IAX2 POKE to each candidate. Any Asterisk answers with a
     PONG before authentication, so a PONG = a PBX, up, with the round-trip
     time from this station. Our own trunks come straight from Asterisk.
  4. Location: from the AREDN node the PBX sits behind (its sysinfo lat/lon
     and grid square), turned into a place name once and cached. The node is
     the one the host list groups the PBX under, accepted only when that
     node's own LAN address shares the PBX's /27; otherwise (or when the PBX
     isn't in any node's list) a traceroute finds it: the hop before the PBX. The
     directory's coordinates are used only when the node publishes none, and
     an office whose node is far from its listed location is flagged.
  5. White pages: /whitepages.xml from the PBXs found (newest copy wins). A
     PBX admin's own listing names the office of a PBX the directory lacks.
  6. Trunks: the directory's trunk lists as the guide; status per direction
     from what PBXs report (their own Asterisk qualify, published only in the
     WT0F map page), and our own trunks verified from Asterisk. Trunks no
     one reports show what can be checked from here: whether both PBXs answer.

Config (radio_directory.meshphone in config.yaml):
  map_enabled        bool   default true
  map_refresh_hours  float  default 24, minimum 1
  own_office         str    this PBX's office name (e.g. MYTOWN)
  hosts_url          str    sysinfo URL of a local AREDN node (default:
                            radio_directory.aredn_discovery.sysinfo_url)
  trunk_reports_url  str    WT0F map page (default wt0f-meshphone.local.mesh/network_map.html)
  geocode            bool   node coordinates -> place names via OpenStreetMap
                            Nominatim (default true, cached)
  checkin            bool   check in to the MeshPhone directory hourly, as the
                            directory's own script does (default false): this
                            PBX's name, host name, mesh address, type, and the
                            latency of each trunk the directory lists for it
  checkin_url        str    default wt0f-meshphone.local.mesh/api/checkin
  checkin_hostname   str    default: the directory's host name for this PBX
  checkin_ip         str    default: this machine's mesh (10.x) address
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import math
import os
import random
import re
import socket
import struct
import time

log = logging.getLogger(__name__)

REFRESH_S = 24 * 3600
RETRY_S = 600
FIRST_DELAY_S = 120
IAX_PORT = 4569
DEFAULT_REPORTS_URL = "http://wt0f-meshphone.local.mesh/network_map.html"
LOCATION_OFF_KM = 25
_IP_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")
_PBX_HOST_RE = re.compile(r"pbx|asterisk|allstar|hamvoip|meshphone|mesh-phone|voip|iax|raspbx|freepbx", re.I)
_PBX_SVC_RE = re.compile(r"pbx|meshphone|mesh ?phone|asterisk|freepbx|raspbx|incredible|allstar|\biax\b|[0-9]{3}-[0-9]{2}xx", re.I)
_HANDSET_RE = re.compile(r"yealink|cisco|polycom|grandstream|gxp|fanvil|\bt[2-5]\d|sip-t|spa\d|\bata\b|handset|-phone-\d", re.I)
_ALLSTAR_ONLY_RE = re.compile(r"allstar|hamvoip|asl\d|node\d{4,}|^\w+-\d{4,6}$", re.I)
_PHONE_SYS_RE = re.compile(r"pbx|meshphone|mesh-phone|freepbx|raspbx|incredible", re.I)
_CALL_RE = re.compile(r"^(?:[A-Z]{1,2}\d{1,4}[A-Z]{1,4}|\d[A-Z]{1,2}\d[A-Z]{1,4})$")
_NUM_RE = re.compile(r"(?<![\d-])(\d{3})[- ](\d{2}(?:\d\d|xx|XX))(?![\d-])")
MAX_CANDIDATES = 500
CALLSIGN_CANDIDATES = 6


# ── IAX2 POKE ────────────────────────────────────────────────────────────────

class _PokeProto(asyncio.DatagramProtocol):
    def __init__(self):
        self.fut = asyncio.get_event_loop().create_future()

    def datagram_received(self, data, addr):
        # full frame, frametype 6 (IAX), subclass 3 (PONG) or 4 (ACK)
        if len(data) >= 12 and data[0] & 0x80 and data[10] == 6 and data[11] in (3, 4):
            if not self.fut.done():
                self.fut.set_result(time.monotonic())

    def error_received(self, exc):
        pass


def poke_frame(src_call: int) -> bytes:
    """IAX2 full frame: POKE (frametype 6, subclass 0x1e), destination call 0."""
    return struct.pack(">HHIBBBB", 0x8000 | (src_call & 0x7FFF), 0, 0, 0, 0, 6, 0x1E)


async def iax_poke(ip: str, timeout: float = 2.5, tries: int = 2) -> float | None:
    """Round-trip ms of an IAX2 POKE->PONG, or None if nothing answered."""
    loop = asyncio.get_event_loop()
    for _ in range(tries):
        try:
            transport, proto = await loop.create_datagram_endpoint(_PokeProto, remote_addr=(ip, IAX_PORT))
        except OSError:
            return None
        try:
            t0 = time.monotonic()
            transport.sendto(poke_frame(random.randint(1, 0x7FFF)))
            t1 = await asyncio.wait_for(proto.fut, timeout)
            return round((t1 - t0) * 1000, 1)
        except asyncio.TimeoutError:
            continue
        finally:
            transport.close()
    return None


def is_local_ip(ip: str) -> bool:
    """True when this machine owns the address (it can bind to it)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind((ip, 0))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _mesh_ip(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip) in ipaddress.ip_network("10.0.0.0/8")
    except ValueError:
        return False


def km(lat1, lon1, lat2, lon2) -> float:
    p = math.pi / 180
    a = (math.sin((lat2 - lat1) * p / 2) ** 2
         + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin((lon2 - lon1) * p / 2) ** 2)
    return 12742 * math.asin(math.sqrt(a))


# ── the mesh host list ───────────────────────────────────────────────────────

def _lan(ip: str) -> tuple:
    """An AREDN node's LAN is at most a /27 (1, 5, 13 or 29 hosts), and node
    mesh addresses are scattered across 10/8, so two hosts share a LAN only
    when they share a /27 - a /24 often holds another node's mesh address."""
    try:
        a, b, c, d = (int(x) for x in (ip or "").split("."))
    except ValueError:
        return ()
    return a, b, c, d // 32


def node_of(hosts: list[dict], i: int | None) -> dict | None:
    """The AREDN node host i sits behind. The sysinfo host list puts each
    node first, then the hosts on its LAN: walk back over the hosts on the
    same LAN, and the entry before them is the node. (Checked against the
    node's own LAN address before its location is used - see build_map.)"""
    if i is None:
        return None
    ip = hosts[i].get("ip") or ""
    j = i - 1
    while j >= 0 and _lan(hosts[j].get("ip") or "") == _lan(ip):
        j -= 1
    return hosts[j] if j >= 0 else None


def host_index(hosts: list[dict], ip: str) -> int | None:
    """Index of the host with this IP, else of a host on the same LAN."""
    first_lan = None
    for i, h in enumerate(hosts):
        hip = h.get("ip") or ""
        if hip == ip:
            return i
        if first_lan is None and _lan(hip) == _lan(ip):
            first_lan = i
    return first_lan


def callsign_of(*names: str) -> str:
    for n in names:
        head = re.split(r"[-_. ]", (n or "").strip())[0].upper()
        if _CALL_RE.match(head):
            return head
    return ""


def numbers_in(text: str) -> list[str]:
    """MeshPhone numbers / ranges in a service name: '770-00xx', '937-1000'."""
    out = []
    for a, b in _NUM_RE.findall(text or ""):
        n = f"{a}-{b.lower()}"
        if n not in out:
            out.append(n)
    return out


def _svc_host(s: dict) -> str:
    m = re.match(r"https?://([^/:]+)", str(s.get("link") or ""))
    return (m.group(1) if m else "").lower().removesuffix(".local.mesh")


def mesh_candidates(hosts: list[dict], services: list[dict]) -> dict[str, list[str]]:
    """ip -> host names, for hosts that look like a PBX by name or by a
    phone-system service linking to them."""
    out: dict[str, list[str]] = {}
    name_ip = {}
    for h in hosts:
        name, ip = str(h.get("name") or ""), str(h.get("ip") or "")
        if not _IP_RE.match(ip):
            continue
        name_ip.setdefault(name.lower(), (name, ip))
        if _PBX_HOST_RE.search(name) and not _HANDSET_RE.search(name):
            out.setdefault(ip, [])
            if name not in out[ip]:
                out[ip].append(name)
    for s in services:
        sname = str(s.get("name") or "")
        if not _PBX_SVC_RE.search(sname) or _HANDSET_RE.search(sname):
            continue
        hit = name_ip.get(_svc_host(s))
        if hit:
            out.setdefault(hit[1], [])
            if hit[0] not in out[hit[1]]:
                out[hit[1]].append(hit[0])
    return dict(list(out.items())[:MAX_CANDIDATES])


def office_candidates(office: dict, hosts: list[dict]) -> list[str]:
    """Addresses to try for a directory office: its listed IP, then mesh hosts
    of the admin's callsign that look like a PBX."""
    out = []
    if _IP_RE.match(office.get("ip") or ""):
        out.append(office["ip"])
    call = (office.get("admin_callsign") or "").upper()
    hn = (office.get("hostname") or "").split(".")[0].upper()
    for h in hosts:
        name, ip = str(h.get("name") or ""), str(h.get("ip") or "")
        if not _IP_RE.match(ip) or ip in out:
            continue
        up = name.upper()
        # a directory hostname like "freepbx" or "raspbx" is someone else's too:
        # only match one that carries a callsign
        if hn and up == hn and (call in hn or re.search(r"\d", hn)):
            out.append(ip)
        elif call and (up.startswith(call + "-") or up == call) and _PBX_HOST_RE.search(name) \
                and not _HANDSET_RE.search(name):
            out.append(ip)
    return out[:CALLSIGN_CANDIDATES]


def advertised_phone_numbers(services: list[dict]) -> dict[str, str]:
    """7-digit number -> service name, for phones their owners advertise on the
    mesh ("KD0HOY 816-0007 [phone]"). AREDN lists a service only while the node
    advertising it is on the mesh."""
    out = {}
    for s in services:
        name = str(s.get("name") or "")
        if "phone" not in name.lower():
            continue
        for n in numbers_in(name):
            d = n.replace("-", "")
            if re.fullmatch(r"\d{7}", d):
                out.setdefault(d, name[:80])
    return out


def own_numbers(endpoints: list[dict], office_code: str) -> dict[str, bool]:
    """MeshPhone number -> registered, for this PBX's extensions: office code
    + the extension's last two digits (ROCKVILLE 20771 + ext 101 -> 2077101)."""
    out = {}
    if not re.fullmatch(r"\d{5}", office_code or ""):
        return out
    for e in endpoints:
        ext = str(e.get("extension") or "")
        if re.fullmatch(r"\d{3}", ext):
            out[office_code + ext[-2:]] = bool(e.get("online"))
    return out


# ── people: handsets on the mesh, their office's PBX ─────────────────────────

SIP_PORT = 5060
MAX_SIP_PROBES = 400
_PHONE_HOST_RE = re.compile(_HANDSET_RE.pattern + r"|phone|\bsip\b", re.I)
_NOT_SIP_RE = re.compile(r"iphone|smartphone|cellphone|headphone|microphone", re.I)


def handset_hosts(hosts: list[dict]) -> dict[str, list[dict]]:
    """callsign -> [{"name", "ip"}] for phones on the mesh named for their owner
    ("N0CALL-phone", "N0CALL-SIP-T26P", "N0CALL-GRANDSTREAM"). PBXs are left out."""
    out: dict[str, list[dict]] = {}
    for h in hosts:
        name, ip = str(h.get("name") or ""), str(h.get("ip") or "")
        if (not _IP_RE.match(ip) or not _PHONE_HOST_RE.search(name) or _NOT_SIP_RE.search(name)
                or (_PHONE_SYS_RE.search(name) and not _HANDSET_RE.search(name))):
            continue
        call = callsign_of(name)
        if call and all(x["ip"] != ip for x in out.get(call, [])):
            out.setdefault(call, []).append({"name": name[:63], "ip": ip})
    return out


def advertised_phone_ips(hosts: list[dict], services: list[dict]) -> dict[str, str]:
    """7-digit number -> the phone's address, for advertised phones whose service
    links to a mesh host ("N0CALL 555-0007 [phone]" -> http://n0call-phone/)."""
    name_ip = {str(h.get("name") or "").lower(): str(h.get("ip") or "") for h in hosts}
    out = {}
    for s in services:
        if "phone" not in str(s.get("name") or "").lower():
            continue
        host = _svc_host(s)
        ip = host if _IP_RE.match(host) else name_ip.get(host, "")
        if not _IP_RE.match(ip):
            continue
        for n in numbers_in(str(s.get("name") or "")):
            d = n.replace("-", "")
            if re.fullmatch(r"\d{7}", d):
                out.setdefault(d, ip)
    return out


def _dial_re(pattern: str) -> re.Pattern | None:
    """An Asterisk dialplan pattern ("_41322XX", "_937[01]XXX") as a regex."""
    p = (pattern or "").strip()
    if not p.startswith("_"):
        return re.compile(re.escape(p)) if re.fullmatch(r"\d+", p) else None
    out, i = "", 1
    while i < len(p):
        c = p[i]
        if c in "Xx":
            out += r"\d"
        elif c in "Nn":
            out += "[2-9]"
        elif c in "Zz":
            out += "[1-9]"
        elif c == ".":
            out += r"\d+"
        elif c == "!":
            out += r"\d*"
        elif c == "[":
            j = p.find("]", i)
            body = p[i + 1:j] if j > i else ""
            if j < 0 or not re.fullmatch(r"[\d-]+", body):
                return None
            out += f"[{body}]"
            i = j
        elif c.isdigit():
            out += c
        else:
            return None
        i += 1
    return re.compile(out)


def office_for_number(number: str, offices: list[dict]) -> dict | None:
    """The office whose code or dialplan covers a 7-digit MeshPhone number."""
    if not re.fullmatch(r"\d{7}", number or ""):
        return None
    for o in offices:
        if number[:5] in (o.get("office_codes") or []):
            return o
    for o in offices:
        for pat in o.get("dialplans") or []:
            rx = _dial_re(pat)
            if rx and rx.fullmatch(number):
                return o
    return None


def sip_options_msg(ip: str, lip: str, lport: int) -> bytes:
    tag = "%08x" % random.getrandbits(32)
    return (f"OPTIONS sip:{ip} SIP/2.0\r\n"
            f"Via: SIP/2.0/UDP {lip}:{lport};branch=z9hG4bK{tag};rport\r\n"
            "Max-Forwards: 70\r\n"
            f"From: <sip:ping@{lip}>;tag={tag}\r\n"
            f"To: <sip:{ip}>\r\n"
            f"Call-ID: {tag}{random.getrandbits(32):08x}@{lip}\r\n"
            "CSeq: 1 OPTIONS\r\n"
            "User-Agent: SignalMatrix\r\n"
            "Content-Length: 0\r\n\r\n").encode()


class _SipProto(asyncio.DatagramProtocol):
    def __init__(self):
        self.fut = asyncio.get_event_loop().create_future()

    def datagram_received(self, data, addr):
        if data.startswith(b"SIP/2.0 ") and not self.fut.done():
            self.fut.set_result(time.monotonic())

    def error_received(self, exc):
        pass


async def sip_ping(ip: str, timeout: float = 2.0, tries: int = 2) -> float | None:
    """Round-trip ms of a SIP OPTIONS to the phone, or None. Any SIP reply
    (200, 401, 404 ...) means the phone is powered and on the mesh."""
    loop = asyncio.get_event_loop()
    for _ in range(tries):
        try:
            transport, proto = await loop.create_datagram_endpoint(_SipProto, remote_addr=(ip, SIP_PORT))
        except OSError:
            return None
        try:
            lip, lport = transport.get_extra_info("sockname")[:2]
            t0 = time.monotonic()
            transport.sendto(sip_options_msg(ip, lip, lport))
            t1 = await asyncio.wait_for(proto.fut, timeout)
            return round((t1 - t0) * 1000, 1)
        except asyncio.TimeoutError:
            continue
        finally:
            transport.close()
    return None


def number_range(numbers: list[str]) -> str:
    """The office's block from its people's 7-digit numbers: '9371000',
    '9371012' -> '937-10xx' (the most common first five digits, when shared)."""
    heads: dict[str, int] = {}
    for n in numbers:
        if re.fullmatch(r"\d{7}", n or ""):
            heads[n[:5]] = heads.get(n[:5], 0) + 1
    if not heads:
        return ""
    head, count = max(heads.items(), key=lambda kv: kv[1])
    return f"{head[:3]}-{head[3:]}xx" if count >= 2 else ""


def services_for(node_ip: str, pbx_ip: str, names: list[str], services: list[dict]) -> list[str]:
    """Services the PBX's node advertises, or that link to the PBX."""
    lnames = {n.lower() for n in names}
    out = []
    for s in services:
        host = _svc_host(s)
        if (node_ip and s.get("ip") == node_ip) or s.get("ip") == pbx_ip or host in lnames or host == pbx_ip:
            n = str(s.get("name") or "").strip()[:80]
            if n and n not in out:
                out.append(n)
    return out


def parse_traceroute(text: str, dest: str) -> str:
    """The hop just before dest in `traceroute -n` output: the AREDN node the
    destination sits behind (its mesh address). "" if dest wasn't reached."""
    hops = []
    for line in (text or "").splitlines():
        m = re.match(r"\s*\d+\s+(\d+\.\d+\.\d+\.\d+)", line)
        if m:
            hops.append(m.group(1))
    if len(hops) >= 2 and hops[-1] == dest:
        return hops[-2]
    return ""


async def trace_node(ip: str, max_hops: int = 20) -> str:
    """The AREDN node in front of a host, by traceroute (UDP, no root needed)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "traceroute", "-n", "-q", "1", "-w", "1", "-m", str(max_hops), ip,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), 45)
    except (OSError, asyncio.TimeoutError):
        return ""
    return parse_traceroute(out.decode("utf-8", "replace"), ip)


# ── Asterisk peers ───────────────────────────────────────────────────────────

_PEER_LINE = re.compile(r"^(\S+?)(?:/\S*)?\s+(\d+\.\d+\.\d+\.\d+|\(null\))\s.*?\s(OK \((\d+) ms\)|UNREACHABLE|UNKNOWN|LAGGED[^\n]*|Unmonitored)\s*",
                        re.M)


def parse_iax_peers(text: str) -> list[dict]:
    """Rows of Asterisk's `iax2 show peers`."""
    out = []
    for m in _PEER_LINE.finditer(text or ""):
        name, host, status, ms = m.group(1), m.group(2), m.group(3), m.group(4)
        if name.lower() in ("name", "name/username"):
            continue
        out.append({"name": name, "host": "" if host == "(null)" else host,
                    "ok": status.startswith("OK"), "ms": int(ms) if ms else None,
                    "status": status.split(" (")[0]})
    return out


def match_peer(peer: dict, offices: list[dict], cands: dict[str, list[str]]) -> str | None:
    """Which directory office an Asterisk IAX peer is: by address, then by
    office name, then by admin callsign (peers are often named after it)."""
    host, name = peer.get("host") or "", (peer.get("name") or "").upper()
    if host:
        for o in offices:
            if host in cands.get(o["name"], []):
                return o["name"]
    for o in offices:
        if o["name"].upper() == name:
            return o["name"]
    for o in offices:
        cs = (o.get("admin_callsign") or "").upper()
        if cs and cs == name:
            return o["name"]
    return None


# ── trunk status PBXs report to the WT0F database ────────────────────────────
# Published only inside its map page, one popup per trunk and direction:
#   "HURRICANE - 97&nbsp;ms - ROCKVILLE"  "DELAND - DOWN - OAKLAND"  "X - UNKNOWN - Y"
_REPORT_RE = re.compile(r">\s*([A-Z0-9][A-Z0-9_.-]*) - (DOWN|UNKNOWN|(\d+)(?:&nbsp;|\s)ms) - ([A-Z0-9][A-Z0-9_.-]*)\s*</div>")


def parse_trunk_reports(html: str) -> list[dict]:
    """[{"by", "to", "ms", "down"}]; UNKNOWN (never reported) left out."""
    out, seen = [], set()
    for m in _REPORT_RE.finditer(html or ""):
        by, status, ms, to = m.group(1), m.group(2), m.group(3), m.group(4)
        if status == "UNKNOWN" or by == to or (by, to) in seen:
            continue
        seen.add((by, to))
        out.append({"by": by, "to": to, "ms": int(ms) if ms else None, "down": status == "DOWN"})
    return out


# ── trunk status a PBX publishes itself ──────────────────────────────────────
# Some PBXs advertise a page with their own trunks ("PBX Trunk Report", "IAX2
# Trunk Status"). Three forms are read: JSON (a list of trunks with host, status
# and latency), Asterisk's own `iax2 show peers` text, and a text table with one
# trunk per line (office code ... host STATUS ... N ms). Live from that PBX, so
# it replaces the directory's report for the same trunk and direction.
_TRUNK_PAGE_NAME_RE = re.compile(r"trunk\s*(report|status)|iax2?\s*(peers|status)", re.I)
_TRUNK_PAGE_PATH_RE = re.compile(r"iax2?_(peers|status)|trunks?(\.json|\.txt|_status)", re.I)
_TABLE_ROW = re.compile(r"^[ \t]*(\d{5})[ \t]+.*?[ \t]([A-Za-z0-9][\w.-]*)[ \t]+(OK|UNREACHABLE|LAGGED|DOWN|UNKNOWN)\b"
                        r"(?:[^\n]*?(\d+)[ \t]*ms)?", re.M)
MAX_TRUNK_PAGES = 30
MAX_TRUNK_PAGE_BYTES = 200_000


def trunk_pages(services: list[dict], hosts: list[dict]) -> list[dict]:
    """The trunk-status pages advertised on the mesh: [{"url", "host", "ip", "name", "link"}]."""
    from urllib.parse import urlsplit
    names = {str(h.get("name") or "").lower(): str(h.get("ip") or "") for h in hosts}
    out, seen = [], set()
    for s in services:
        link, name = str(s.get("link") or ""), str(s.get("name") or "")
        if not link.lower().startswith("http"):
            continue
        try:
            u = urlsplit(link)
            port = u.port
        except ValueError:
            continue
        if not (_TRUNK_PAGE_NAME_RE.search(name) or _TRUNK_PAGE_PATH_RE.search(u.path)):
            continue
        host = (u.hostname or "").lower().removesuffix(".local.mesh")
        ip = host if _IP_RE.match(host) else names.get(host, "")
        if not ip or not _mesh_ip(ip):
            continue
        q = u.query
        if u.path.endswith("iax2_status_ami.php") and "json=1" not in q:
            q = (q + "&" if q else "") + "json=1"          # that page's data, not its script
        url = f"http://{ip}{f':{port}' if port and port != 80 else ''}{u.path or '/'}{'?' + q if q else ''}"
        if url not in seen:
            seen.add(url)
            out.append({"url": url, "host": host, "ip": ip, "name": name[:80], "link": link[:300]})
    return out[:MAX_TRUNK_PAGES]


def parse_published_trunks(text: str) -> dict:
    """{"code": the page's own office code or "", "peers": [{"name", "host",
    "code", "ok", "ms"}]}; trunks Asterisk doesn't monitor are left out."""
    import html as _html
    text = text or ""
    peers: list[dict] = []
    s = text.lstrip()
    if s[:1] in ("{", "["):
        try:
            j = json.loads(s)
        except ValueError:
            j = None
        rows = j if isinstance(j, list) else next(
            (j[k] for k in ("data", "trunks", "peers") if isinstance(j, dict) and isinstance(j.get(k), list)), [])
        for r in rows:
            if not isinstance(r, dict):
                continue
            st = str(r.get("status") or "").upper()
            if not st or st.startswith(("UNKNOWN", "UNMONITORED")):
                continue
            ms = r.get("latency_ms", r.get("ms"))
            ok = st.startswith(("OK", "LAGGED", "UP"))
            peers.append({"name": str(r.get("trunk_name") or r.get("name") or "")[:63],
                          "host": str(r.get("host") or "")[:63], "code": str(r.get("office_code") or "")[:10],
                          "ok": ok, "ms": int(ms) if ok and isinstance(ms, (int, float)) else None})
        return {"code": str(j.get("office_code") or "")[:10] if isinstance(j, dict) else "", "peers": peers}
    plain = _html.unescape(re.sub(r"<[^>]+>", "", text))
    for p in parse_iax_peers(plain):
        if p["status"].startswith(("UNKNOWN", "Unmonitored")):
            continue
        ok = p["ok"] or p["status"].startswith("LAGGED")
        peers.append({"name": p["name"], "host": p["host"], "code": "", "ok": ok, "ms": p["ms"] if ok else None})
    if not peers:
        for m in _TABLE_ROW.finditer(plain):
            code, host, st, ms = m.groups()
            if st == "UNKNOWN":
                continue
            ok = st in ("OK", "LAGGED")
            peers.append({"name": "", "host": host, "code": code, "ok": ok, "ms": int(ms) if ok and ms else None})
    m = re.search(r"\boffice\s+(\d{5})\b", plain, re.I)
    return {"code": m.group(1) if m else "", "peers": peers}


def published_candidates(offices: list[dict], published: list[dict], hosts: list[dict]) -> dict[str, list[str]]:
    """More addresses to try for directory offices, from published trunk pages:
    the page's own PBX for the office whose code the page names, and a trunk's
    host for the office with that code (by address, or by the directory's own
    host name for it). Fixes an office whose listed address is stale."""
    by_code = {str(c): o for o in offices for c in o.get("office_codes") or []}
    ip_of = {str(h.get("name") or "").lower(): str(h.get("ip") or "") for h in hosts}
    out: dict[str, list[str]] = {}

    def add(o, ip):
        if o and ip and _mesh_ip(ip) and ip not in out.setdefault(o["name"], []):
            out[o["name"]].append(ip)
    for pg in published or []:
        add(by_code.get(str(pg.get("code") or "")), pg.get("ip", ""))
        for p in pg.get("peers", []):
            o, h = by_code.get(str(p.get("code") or "")), str(p.get("host") or "").lower()
            if not o or not h:
                continue
            if _IP_RE.match(h):
                add(o, h)
            elif h.removesuffix(".local.mesh") == (o.get("hostname") or "").split(".")[0].lower():
                add(o, ip_of.get(h.removesuffix(".local.mesh"), ""))
    return out


# ── check-in to the MeshPhone directory ──────────────────────────────────────
# The same form POST the directory's own generator script sends each hour, so
# the directory shows this PBX Active at its address with its trunks' latency.
DEFAULT_CHECKIN_URL = "http://wt0f-meshphone.local.mesh/api/checkin"
CHECKIN_S = 3600


def checkin_trunks(peers: list[dict], listed: list[str], office_of) -> list[dict]:
    """[{"pbx_name", "latency"}] for this PBX's mesh trunks to the PBXs the
    directory lists as its trunks (as the directory's script reports them):
    latency in ms, or "Unreachable". office_of(peer) -> directory PBX name."""
    out, seen = [], set()
    for p in peers:
        if not _mesh_ip(p.get("host") or "") or p.get("status", "").startswith(("UNKNOWN", "Unmonitored")):
            continue
        name = office_of(p)
        if not name or name not in listed or name in seen:
            continue
        if p.get("ok") and p.get("ms") is not None:
            out.append({"pbx_name": name, "latency": str(p["ms"])})
        elif p.get("status", "").startswith("UNREACHABLE"):
            out.append({"pbx_name": name, "latency": "Unreachable"})
        else:
            continue
        seen.add(name)
    return out


def checkin_form(data: dict) -> dict[str, str]:
    """The check-in as form fields, trunks in PHP's nested form
    (trunks[0][pbx_name]=...), which is what the directory reads."""
    out = {k: str(v) for k, v in data.items() if k != "trunks" and v not in (None, "")}
    for i, t in enumerate(data.get("trunks") or []):
        out[f"trunks[{i}][pbx_name]"] = str(t["pbx_name"])
        out[f"trunks[{i}][latency]"] = str(t["latency"])
    return out


def pbx_type() -> str:
    """What the directory calls this PBX (its script's own test)."""
    if os.path.exists("/etc/asterisk/rpt.conf"):
        return "Allstar"
    if os.path.exists("/etc/amportal.conf"):
        return "FreePBX"
    return "Asterisk"


def mesh_address() -> str:
    """This machine's mesh (10.x) address, "" if none."""
    try:
        import psutil
        for addrs in psutil.net_if_addrs().values():
            for a in addrs:
                if a.family == socket.AF_INET and _mesh_ip(a.address):
                    return a.address
    except Exception:
        pass
    return ""


# ── the map ──────────────────────────────────────────────────────────────────

def build_map(offices: list[dict], hosts: list[dict], services: list[dict],
              cands: dict[str, list[str]], mesh_cands: dict[str, list[str]],
              answers: dict[str, float], nodes: dict[str, dict], own_ips: set[str],
              own_office: str = "", own_peers: list[dict] | None = None,
              people: list[dict] | None = None, reports: list[dict] | None = None,
              generated_at: float | None = None, node_for: dict[str, str] | None = None,
              own_registered: dict[str, bool] | None = None,
              sip_answers: dict[str, float] | None = None,
              published: list[dict] | None = None) -> dict:
    """offices: directory offices (the guide); cands: office -> addresses to try;
    mesh_cands: ip -> names of other PBX-looking mesh hosts; answers: ip -> POKE
    ms (answered only); nodes: node ip -> {"name","lat","lon","grid","place"};
    own_ips: this machine's addresses; people: white pages; reports: what PBXs
    report about their own trunks; node_for: PBX ip -> its node's address
    found by traceroute (used when the host list's grouping is unverified);
    own_registered: this PBX's MeshPhone numbers -> whether that phone is registered;
    sip_answers: phone ip -> SIP OPTIONS ms (answered only); published: trunk
    pages PBXs publish themselves (trunk_pages + parse_published_trunks)."""
    node_for = node_for or {}
    own_registered = own_registered or {}
    sip_answers = sip_answers or {}
    own_peers, people, reports = own_peers or [], people or [], reports or []
    answers = dict(answers)
    names_by_ip: dict[str, list[str]] = {}
    for h in hosts:
        names_by_ip.setdefault(h.get("ip"), []).append(h.get("name", ""))

    # Our trunks that Asterisk qualifies OK prove those PBXs are up, at the trunk's address.
    peer_office = {}
    for peer in own_peers:
        if not _mesh_ip(peer.get("host") or ""):
            continue                       # an internet trunk (e.g. HOIP), not a mesh PBX
        o = match_peer(peer, offices, cands)
        if o:
            peer_office[peer["name"]] = o
            if peer["host"] not in cands.get(o, []):
                cands = {**cands, o: cands.get(o, []) + [peer["host"]]}
        if peer["ok"] and peer["host"] not in answers:
            answers[peer["host"]] = peer["ms"]

    def where(ip: str) -> dict:
        """The PBX's parent node and what it says about its location."""
        node = node_of(hosts, host_index(hosts, ip)) if ip else None
        node_ip = (node or {}).get("ip", "")
        n = nodes.get(node_ip, {})
        if n.get("lan") and _lan(n["lan"]) != _lan(ip):
            n = {}                         # the host list's grouping was wrong: that's not its node
            node, node_ip = None, ""
        if ip in node_for and (not n or n.get("lat") is None):
            node_ip = node_for[ip]         # the last hop before the PBX, by traceroute
            n = nodes.get(node_ip, {})
            node = {"ip": node_ip, "name": n.get("name") or next(
                (h.get("name") for h in hosts if h.get("ip") == node_ip), "")}
        return {"node": n.get("name") or (node or {}).get("name", ""), "node_ip": node_ip,
                "node_lat": n.get("lat"), "node_lon": n.get("lon"), "grid": n.get("grid", ""),
                "place": n.get("place", "")}

    def entry(name, ip, state, ms, o=None, extra_names=()):
        w = where(ip)
        hn = list(dict.fromkeys(names_by_ip.get(ip, []) + [x for x in extra_names if x]))
        svc = services_for(w["node_ip"], ip, hn, services) if ip else []
        o = o or {}
        e = {
            "name": name, "in_directory": bool(o), "location": o.get("location", ""),
            "office_codes": o.get("office_codes", []), "dialplans": o.get("dialplans", []),
            "admin_callsign": o.get("admin_callsign") or callsign_of(*hn, w["node"]),
            "admin_name": o.get("admin_name", ""), "admin_email": o.get("admin_email", ""),
            "admin_phone": o.get("admin_phone", ""), "pbx_type": o.get("pbx_type", ""),
            "listed_status": o.get("pbx_status", ""), "checkin_at": o.get("checkin_at", ""),
            "listed_ip": o.get("ip", ""), "trunks_listed": o.get("trunks", []),
            "state": state, "rtt_ms": ms, "answering_ip": ip if state in ("up", "self") else "",
            "ip_moved": bool(state == "up" and ip and o.get("ip") and ip != o.get("ip")),
            "hostnames": hn[:4], "services": svc[:8],
            "numbers": list(dict.fromkeys(n for s in svc for n in numbers_in(s))),
            **w, "lat": None, "lon": None, "loc_source": "", "loc_off_km": None,
            "tried": cands.get(name, []) if o else ([ip] if ip else []),
        }
        if w["node_lat"] is not None:
            e["lat"], e["lon"], e["loc_source"] = w["node_lat"], w["node_lon"], "node"
            if o.get("lat") is not None:
                d = km(w["node_lat"], w["node_lon"], o["lat"], o["lon"])
                if d > LOCATION_OFF_KM:
                    e["loc_off_km"] = round(d)
        elif o.get("lat") is not None:
            e["lat"], e["lon"], e["loc_source"] = o["lat"], o["lon"], "directory"
        if not e["place"]:
            e["place"] = o.get("location", "")
        return e

    out: list[dict] = []
    claimed: set[str] = set()
    for o in offices:
        n = o["name"]
        tried = cands.get(n, [])
        if n == own_office or any(ip in own_ips for ip in tried):
            ip = next((ip for ip in tried if ip in own_ips), next(iter(own_ips), ""))
            e = entry(n, ip, "self", None, o)
        else:
            hit = next((ip for ip in tried if ip in answers), "")
            if hit:
                e = entry(n, hit, "up", answers[hit], o)
            else:
                # not answering: still locate it by its listed address's node, if any
                e = entry(n, tried[0] if tried else "", "no_answer" if tried else "no_address", None, o)
                e["answering_ip"] = ""
        claimed.update(tried)
        out.append(e)
    # PBXs on the mesh that the directory doesn't have
    office_of_call = {}
    for p in people:
        if p.get("pbx"):
            office_of_call.setdefault(p.get("callsign", "").upper(), []).append(p["pbx"])
    known = {e["name"].upper() for e in out}
    for ip, hn in mesh_cands.items():
        if ip in claimed or (ip not in answers and ip not in own_ips):
            continue                       # only PBXs that answer are added from the mesh
        if any(_ALLSTAR_ONLY_RE.search(n) for n in hn) and not any(_PHONE_SYS_RE.search(n) for n in hn):
            continue                       # an AllStar node runs Asterisk too, but isn't a MeshPhone PBX
        call = callsign_of(*hn, where(ip)["node"])
        guess = office_of_call.get(call, [])
        name = max(set(guess), key=guess.count) if guess else (hn[0] if hn else ip)
        if name.upper() in known:
            name = f"{name} ({hn[0] if hn else ip})"
        known.add(name.upper())
        out.append(entry(name, ip, "self" if ip in own_ips else "up", answers.get(ip), None, hn))
        claimed.add(ip)
    # A directory office that doesn't answer and has no host on the mesh at all
    # (its address and every callsign host are gone) isn't shown: it doesn't exist.
    on_mesh = {h.get("ip") for h in hosts}
    dropped = [e["name"] for e in out
               if e["state"] in ("no_answer", "no_address") and not any(ip in on_mesh for ip in e["tried"])]
    out = [e for e in out if e["name"] not in set(dropped)]

    # several offices answering on one PBX: hosted offices (e.g. one PBX serving
    # several towns). The PBX's location then isn't the office's service area.
    on_ip: dict[str, list[str]] = {}
    for e in out:
        if e["answering_ip"]:
            on_ip.setdefault(e["answering_ip"], []).append(e["name"])
    for e in out:
        e["shares_pbx_with"] = [n for n in on_ip.get(e["answering_ip"], []) if n != e["name"]]
        e["hosted"] = bool(e["shares_pbx_with"] and e["loc_off_km"])
    by_name = {e["name"]: e for e in out}
    gone = set(dropped)

    # trunks PBXs publish themselves: live from that PBX, so they replace its directory report
    def office_of(ip: str = "", host: str = "", code: str = "", name: str = "") -> str | None:
        host = host.lower().removesuffix(".local.mesh")
        for e in out:
            if code and code in e["office_codes"]:
                return e["name"]
        for e in out:
            if ip and (ip == e["answering_ip"] or ip in e["tried"]):
                return e["name"]
        for h in (host, name.lower()):
            for e in out:
                if h and h in (x.lower() for x in e["hostnames"]):
                    return e["name"]
        for e in out:
            if name and name.upper() in (e["name"].upper(), (e["admin_callsign"] or "").upper()):
                return e["name"]
        return None
    pub: dict[tuple[str, str], dict] = {}
    for pg in published or []:
        by = office_of(ip=pg.get("ip", ""), host=pg.get("host", ""), code=pg.get("code", ""))
        if not by:
            continue
        for p in pg.get("peers", []):
            h = str(p.get("host") or "")
            is_ip = bool(_IP_RE.match(h))
            if is_ip and not _mesh_ip(h):
                continue                       # an internet trunk (e.g. HOIP)
            to = office_of(ip=h if is_ip else "", host="" if is_ip else h, code=p.get("code", ""),
                           name=p.get("name", ""))
            if to and to != by and to not in gone:
                pub[(by, to)] = {"by": by, "to": to, "ms": p.get("ms") if p.get("ok") else None,
                                 "down": not p.get("ok"), "at": pg.get("at", ""), "source": "published"}
    if pub:
        reports = [r for r in reports if (r["by"], r["to"]) not in pub] + list(pub.values())

    # trunks: directory lists + reports + our own
    pairs: dict[tuple[str, str], dict] = {}
    for o in offices:
        for t in o.get("trunks", []):
            if t != o["name"]:
                key = tuple(sorted((o["name"], t)))
                p = pairs.setdefault(key, {"a": key[0], "b": key[1], "listed_by": []})
                if o["name"] not in p["listed_by"]:
                    p["listed_by"].append(o["name"])
    own_links = {}
    self_name = next((e["name"] for e in out if e["state"] == "self"), own_office)
    for peer in own_peers:
        other = peer_office.get(peer["name"])
        if not other:
            other = next((e["name"] for e in out if peer.get("host") and peer["host"] == e["answering_ip"]), None)
        if other and self_name and other != self_name:
            key = tuple(sorted((self_name, other)))
            pairs.setdefault(key, {"a": key[0], "b": key[1], "listed_by": []})
            own_links[key] = peer
    checkin = {o["name"]: o.get("checkin_at", "") for o in offices}
    by_pair: dict[tuple, list] = {}
    for r in reports:
        key = tuple(sorted((r["by"], r["to"])))
        pairs.setdefault(key, {"a": key[0], "b": key[1], "listed_by": []})
        by_pair.setdefault(key, []).append({**r, "at": r.get("at") or checkin.get(r["by"], "")})
    links = []
    for key, p in sorted(pairs.items()):
        if p["a"] in gone or p["b"] in gone:
            continue                       # a trunk to an office that no longer exists
        a, b = by_name.get(p["a"]), by_name.get(p["b"])
        rep = by_pair.get(key, [])
        lk = {**p, "mutual": len(p["listed_by"]) == 2, "rtt_ms": None, "peer": "", "reports": rep}
        ups = [r for r in rep if not r["down"] and r["ms"] is not None]
        if a is None or b is None:
            lk["state"] = "unlisted_end"
        elif key in own_links:
            peer = own_links[key]
            lk["peer"], lk["rtt_ms"] = peer["name"], peer["ms"]
            lk["state"] = "verified" if peer["ok"] else "down"
        elif rep:
            lk["state"] = "reported_mixed" if ups and len(ups) < len(rep) else "reported_up" if ups else "reported_down"
            if ups:
                lk["rtt_ms"] = round(sum(r["ms"] for r in ups) / len(ups))
        else:
            sa, sb = a["state"], b["state"]
            lk["state"] = ("both_up" if {sa, sb} <= {"up", "self"}
                           else "end_down" if "no_answer" in (sa, sb) else "unknown")
        links.append(lk)

    # The only white pages on the mesh is an old copy, so offices set up since are
    # missing: list each directory office's admin where the white pages lack them.
    people = [dict(x, source=x.get("source") or "white pages") for x in people]
    have = {x.get("number") for x in people}
    for o in offices:
        n = o.get("admin_phone") or ""
        if n and n not in have and o.get("admin_callsign") and o["name"] not in gone:
            people.append({"callsign": o["admin_callsign"], "name": o.get("admin_name", ""), "number": n,
                           "pbx": o["name"], "source": "directory (office admin)"})
            have.add(n)
    # Status, best evidence first: our own registrations; the person's phone
    # answering a SIP ping on the mesh; a phone advertised as a mesh service;
    # else whether the PBX they're listed on answers (other PBXs don't publish
    # who is registered). The office is found by name, else by number.
    advertised = advertised_phone_numbers(services)
    adv_ip = advertised_phone_ips(hosts, services)
    handsets = handset_hosts(hosts)
    office_entry = {e["name"].upper(): e for e in out}
    listed = [o for o in offices if o["name"].upper() in office_entry]
    for x in people:
        num, call = x.get("number", ""), (x.get("callsign") or "").upper()
        e = office_entry.get((x.get("pbx") or "").upper())
        if e is None:
            o = office_for_number(num, listed)
            e = office_entry.get(o["name"].upper()) if o else None
            if e is not None:
                x["office"] = e["name"]
        phones = ([{"name": advertised.get(num, ""), "ip": adv_ip[num]}] if num in adv_ip else []) + handsets.get(call, [])
        up = next((p for p in phones if p["ip"] in sip_answers), None)
        x["status_note"] = ""
        if num in own_registered and (x.get("pbx") or "").upper() == (self_name or "").upper():
            x["status"] = "online" if own_registered[num] else "offline"
        elif up:
            x["status"] = "phone_up"
            x["status_note"] = f"{up['name']} ({up['ip']}) answered a SIP ping in {sip_answers[up['ip']]:.0f} ms"
        elif num in advertised:
            x["status"], x["status_note"] = "on_mesh", advertised[num]
        elif e is not None and e["state"] in ("up", "self", "no_answer"):
            x["status"] = "pbx_down" if e["state"] == "no_answer" else "pbx_up"
            x["status_note"] = (f"{e['name']} PBX " + ("isn't answering" if x["status"] == "pbx_down" else "answers")
                                + "; it doesn't publish which phones are registered")
        else:
            x["status"] = ""
        if phones and not up and x["status"] not in ("online", "offline"):
            quiet = ", ".join(p["name"] or p["ip"] for p in phones[:2])
            x["status_note"] = "; ".join(s for s in (x["status_note"], f"{quiet} on the mesh didn't answer a SIP ping") if s)

    # each office's people (white pages) and the number block they share
    at: dict[str, list[dict]] = {}
    for x in people:
        at.setdefault((x.get("pbx") or "").upper(), []).append(x)
    for e in out:
        mine = at.get(e["name"].upper(), [])
        e["people_at"] = [{k: x.get(k, "") for k in ("callsign", "name", "number")} for x in mine[:40]]
        e["people_count"] = len(mine)
        e["number_range"] = number_range([x.get("number", "") for x in mine])

    # people: located by their office's PBX (by name, else the office covering the number)
    out_people = []
    for x in people:
        e = office_entry.get((x.get("pbx") or "").upper()) or office_entry.get((x.get("office") or "").upper())
        out_people.append({**{k: x.get(k, "") for k in ("callsign", "name", "number", "pbx", "office", "source",
                                                        "status", "status_note")},
                           "location": (e or {}).get("place", ""), "grid": (e or {}).get("grid", "")})

    def count(items, k):
        d: dict[str, int] = {}
        for i in items:
            d[i[k]] = d.get(i[k], 0) + 1
        return d
    return {
        "generated_at": generated_at or time.time(),
        "measured_from": self_name,
        "offices": out, "links": links, "people": out_people,
        "summary": {"offices": len(out), "office_states": count(out, "state"),
                    "links": len(links), "link_states": count(links, "state"),
                    "moved": sum(1 for e in out if e["ip_moved"]),
                    "mesh_only": sum(1 for e in out if not e["in_directory"]),
                    "node_located": sum(1 for e in out if e["loc_source"] == "node"),
                    "location_off": sum(1 for e in out if e["loc_off_km"] and not e["hosted"]),
                    "hosted": sum(1 for e in out if e["hosted"]),
                    "dropped": sorted(dropped),
                    "trunk_pages": len(published or []), "published_reports": len(pub),
                    "people": len(out_people),
                    "people_online": sum(1 for x in out_people if x["status"] == "online"),
                    "people_on_mesh": sum(1 for x in out_people if x["status"] == "on_mesh"),
                    "people_status": count(out_people, "status")},
    }


# ── service ──────────────────────────────────────────────────────────────────

class MeshPhoneMap:
    def __init__(self) -> None:
        self.data: dict | None = None
        self.last_error: str | None = None
        self.enabled = True
        self._config: dict = {}
        self._router = None
        self._trunks_cache: tuple[float, dict] | None = None
        self._checkin_task: asyncio.Task | None = None
        self.last_checkin: dict | None = None
        self._task: asyncio.Task | None = None
        self.cache_path: str | None = None
        self.places_path: str | None = None
        self.refresh_s = REFRESH_S
        self._places: dict[str, str] = {}
        self.people_created = ""

    def configure(self, config: dict, router=None) -> None:
        self._config = config or {}
        self._router = router
        mp = ((config.get("radio_directory") or {}).get("meshphone") or {})
        self.enabled = bool(mp.get("enabled", True)) and bool(mp.get("map_enabled", True))
        try:
            self.refresh_s = max(3600, int(float(mp.get("map_refresh_hours", REFRESH_S / 3600)) * 3600))
        except (TypeError, ValueError):
            self.refresh_s = REFRESH_S
        db_path = (config.get("database") or {}).get("path", "ech.db")
        base = os.path.dirname(os.path.abspath(db_path)) if db_path != ":memory:" else os.getcwd()
        self.cache_path = os.path.join(base, "meshphone_map.json")
        self.places_path = os.path.join(base, "meshphone_places.json")
        try:
            with open(self.cache_path, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict) and "people" in d:      # older maps lack fields: rebuild them
                self.data = d
        except (OSError, ValueError):
            pass
        try:
            with open(self.places_path, encoding="utf-8") as f:
                self._places = json.load(f)
        except (OSError, ValueError):
            pass

    def _cfg(self) -> dict:
        rd = self._config.get("radio_directory") or {}
        mp = rd.get("meshphone") or {}
        disc = rd.get("aredn_discovery") or {}
        return {"own_office": str(mp.get("own_office") or "").upper(),
                "hosts_url": str(mp.get("hosts_url") or disc.get("sysinfo_url") or ""),
                "reports_url": str(mp.get("trunk_reports_url") or DEFAULT_REPORTS_URL),
                "dns_server": str(mp.get("dns_server") or "auto"),
                "geocode": bool(mp.get("geocode", True)),
                "checkin": bool(mp.get("checkin", False)),
                "checkin_url": str(mp.get("checkin_url") or DEFAULT_CHECKIN_URL),
                "checkin_hostname": str(mp.get("checkin_hostname") or ""),
                "checkin_ip": str(mp.get("checkin_ip") or "")}

    async def start(self, config: dict, router=None) -> None:
        self.configure(config, router)
        if self.enabled and self._task is None:
            self._task = asyncio.create_task(self._loop())
        if self._cfg()["checkin"] and self._checkin_task is None and self._router:
            self._checkin_task = asyncio.create_task(self._checkin_loop())

    async def _checkin_loop(self) -> None:
        await asyncio.sleep(FIRST_DELAY_S + 30)
        while True:
            try:
                await self.checkin()
            except Exception as exc:
                self.last_checkin = {"at": time.time(), "ok": False, "error": f"{exc.__class__.__name__}: {exc}"}
                log.info("MeshPhone check-in failed: %s", exc)
            await asyncio.sleep(CHECKIN_S)

    async def checkin_data(self) -> dict | None:
        """What the check-in sends; None without an own_office or a mesh address."""
        from ech import __version__
        from ech.core.directory_status import status as live
        cfg = self._cfg()
        office = cfg["own_office"]
        ip = cfg["checkin_ip"] or mesh_address()
        if not office or not ip:
            return None
        offices = list(live.mp_offices)
        own = next((o for o in offices if o.get("name") == office), {})
        listed = [t for t in own.get("trunks") or [] if t != office]
        seen = {e.get("answering_ip"): e["name"] for e in (self.data or {}).get("offices", [])
                if e.get("answering_ip") and e.get("in_directory")}
        cands = {o["name"]: office_candidates(o, []) for o in offices}

        def office_of(p):
            return seen.get(p.get("host")) or match_peer(p, offices, cands)
        hostname = cfg["checkin_hostname"] or (own.get("hostname") or "").split(".")[0] or socket.gethostname()
        return {"pbx_name": office, "hostname": hostname, "ip_address": ip, "pbx_type": pbx_type(),
                "gen_script_name": "SignalMatrix", "gen_script_version": __version__,
                "trunks": checkin_trunks(await self._own_peers(), listed, office_of)}

    async def checkin(self) -> dict:
        """One check-in to the MeshPhone directory."""
        import httpx
        from ech.core import meshphone_directory as mpd
        data = await self.checkin_data()
        if not data:
            self.last_checkin = {"at": time.time(), "ok": False, "error": "no own_office or mesh address"}
            return self.last_checkin
        cfg = self._cfg()
        url, hdrs = await asyncio.to_thread(mpd.resolve_url, cfg["checkin_url"], cfg["dns_server"])
        async with httpx.AsyncClient(headers={"User-Agent": "SignalMatrix-ECH/1 (MeshPhone check-in)"}) as client:
            r = await client.post(url, data=checkin_form(data), headers=hdrs, timeout=20)
        self.last_checkin = {"at": time.time(), "ok": r.status_code < 400, "status": r.status_code,
                             "trunks": [t["pbx_name"] for t in data["trunks"]]}
        log.info("MeshPhone check-in as %s (%s, %s): HTTP %s, %d trunk(s) reported",
                 data["pbx_name"], data["hostname"], data["ip_address"], r.status_code, len(data["trunks"]))
        return self.last_checkin

    async def _loop(self) -> None:
        age = time.time() - float((self.data or {}).get("generated_at") or 0)
        await asyncio.sleep(max(FIRST_DELAY_S, self.refresh_s - age))   # a restart doesn't re-measure early
        while True:
            wait = self.refresh_s
            try:
                await self.refresh()
            except Exception as exc:
                self.last_error = f"{exc.__class__.__name__}: {exc}"
                log.warning("MeshPhone map refresh failed (retrying in 10 min): %s", self.last_error)
                wait = RETRY_S                 # e.g. the AREDN node was briefly unreachable
            await asyncio.sleep(wait)

    async def _sysinfo(self, client) -> tuple[list[dict], list[dict]]:
        url = self._cfg()["hosts_url"]
        if not url:
            raise RuntimeError("set radio_directory.meshphone.hosts_url (or aredn_discovery.sysinfo_url) "
                               "to a local AREDN node's sysinfo URL")
        for q in ("hosts=1", "services=1"):
            if q not in url:
                url += ("&" if "?" in url else "?") + q
        for attempt in range(3):
            try:
                r = await client.get(url, timeout=40)
                r.raise_for_status()
                j = r.json()
                break
            except Exception:
                if attempt == 2:
                    raise
                await asyncio.sleep(10)
        return ([h for h in j.get("hosts") or [] if isinstance(h, dict)],
                [s for s in j.get("services") or [] if isinstance(s, dict)])

    async def _node_info(self, client, ip: str) -> dict:
        try:
            j = (await client.get(f"http://{ip}/cgi-bin/sysinfo.json", timeout=8)).json()
        except Exception:
            return {}
        name = str(j.get("node") or "")[:63]
        lan = next((i.get("ip") for i in j.get("interfaces") or []
                    if isinstance(i, dict) and i.get("name") == "br-lan" and i.get("ip")), "")
        try:
            lat, lon = float(j.get("lat")), float(j.get("lon"))
        except (TypeError, ValueError):
            return {"name": name, "lan": lan}
        if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
            return {"name": name, "lan": lan}
        return {"name": name, "lan": lan, "lat": round(lat, 4), "lon": round(lon, 4),
                "grid": str(j.get("gridsquare") or j.get("grid_square") or "")[:8]}

    async def _place(self, client, lat: float, lon: float) -> str:
        key = f"{lat:.2f},{lon:.2f}"
        if key in self._places:
            return self._places[key]
        if not self._cfg()["geocode"]:
            return ""
        try:
            await asyncio.sleep(1.1)        # Nominatim: at most one request a second
            r = await client.get("https://nominatim.openstreetmap.org/reverse",
                                 params={"lat": lat, "lon": lon, "format": "jsonv2", "zoom": 10},
                                 headers={"User-Agent": "SignalMatrix-ECH/1 (MeshPhone map)"}, timeout=15)
            a = (r.json() or {}).get("address") or {}
        except Exception as exc:
            log.debug("MeshPhone map: reverse geocode failed: %s", exc)
            return ""
        town = a.get("city") or a.get("town") or a.get("village") or a.get("hamlet") or a.get("county") or ""
        cc = (a.get("country_code") or "").upper()
        region = a.get("state") or a.get("region") or ""
        place = ", ".join(x for x in (town, region if cc in ("US", "CA", "AU") else (region or cc)) if x)
        if cc and cc not in ("US", "CA", "AU") and cc not in place:
            place = f"{place} ({cc})" if place else cc
        self._places[key] = place
        return place

    async def _whitepages(self, client, ips: list[str]) -> list[dict]:
        from ech.core.meshphone_directory import parse_whitepages
        sem = asyncio.Semaphore(8)

        async def one(ip):
            async with sem:
                try:
                    r = await client.get(f"http://{ip}/whitepages.xml", timeout=6)
                    if r.status_code == 200 and "<WhitePages" in r.text[:500]:
                        return parse_whitepages(r.text)
                except Exception:
                    pass
            return None
        best = ("", [])
        for res in await asyncio.gather(*(one(ip) for ip in ips)):
            if res and len(res[0]) >= 10 and res[1] > best[0]:
                best = (res[1], res[0])
        if best[1]:
            self.people_created = best[0]
            return best[1]
        from ech.core.directory_status import status as live      # fall back to the hourly copy
        self.people_created = live.mp_people_created
        return list(live.mp_people)

    async def _published(self, client, hosts: list[dict], services: list[dict]) -> list[dict]:
        """Trunk pages PBXs advertise on the mesh, read (one request each)."""
        pages = trunk_pages(services, hosts)
        sem = asyncio.Semaphore(6)

        async def one(pg):
            async with sem:
                try:
                    hdr = {} if _IP_RE.match(pg["host"]) else {"Host": pg["host"] + ".local.mesh"}
                    r = await client.get(pg["url"], headers=hdr, timeout=10)
                    if r.status_code != 200 or len(r.content) > MAX_TRUNK_PAGE_BYTES:
                        return None
                    got = parse_published_trunks(r.text)
                except Exception:
                    return None
            if not got["peers"]:
                return None
            return {**pg, **got, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        res = [x for x in await asyncio.gather(*(one(p) for p in pages)) if x]
        log.info("MeshPhone map: %d of %d published trunk pages read", len(res), len(pages))
        return res

    async def _reports(self, client) -> list[dict]:
        from ech.core import meshphone_directory as mpd
        cfg = self._cfg()
        url, hdrs = await asyncio.to_thread(mpd.resolve_url, cfg["reports_url"], cfg["dns_server"])
        r = await client.get(url, headers=hdrs, timeout=40)
        r.raise_for_status()
        return parse_trunk_reports(r.text)

    def _own_endpoints(self) -> list[dict]:
        for a in getattr(self._router, "_adapters", {}).values() if self._router else []:
            if hasattr(a, "list_endpoint_status"):
                try:
                    return a.list_endpoint_status()
                except Exception:
                    pass
        return []

    async def trunk_status(self) -> dict:
        """This PBX's own mesh trunks, live from Asterisk (cached a minute), in
        the form other copies of the map read from a published trunk page.
        Internet trunks are left out: only mesh addresses and their status."""
        now = time.time()
        if self._trunks_cache and now - self._trunks_cache[0] < 60:
            return self._trunks_cache[1]
        from ech.core.directory_status import status as live
        office = self._cfg()["own_office"]
        o = next((x for x in live.mp_offices if x.get("name") == office), {})
        trunks = [{"name": p["name"][:63], "host": p["host"], "status": p["status"][:20],
                   "latency_ms": p["ms"] if p["ok"] else None}
                  for p in await self._own_peers() if _mesh_ip(p.get("host") or "")]
        d = {"office": office, "office_code": (o.get("office_codes") or [""])[0], "at": round(now),
             "trunks": trunks}
        self._trunks_cache = (now, d)
        return d

    async def _own_peers(self) -> list[dict]:
        if not self._router:
            return []
        for a in getattr(self._router, "_adapters", {}).values():
            if hasattr(a, "ami_command") and getattr(a, "_connected", False):
                try:
                    return parse_iax_peers(await a.ami_command("iax2 show peers"))
                except Exception as exc:
                    log.debug("MeshPhone map: iax2 show peers failed: %s", exc)
        return []

    async def refresh(self) -> dict | None:
        import httpx
        from ech.core.directory_status import status as live
        offices = list(live.mp_offices)
        cfg = self._cfg()
        async with httpx.AsyncClient(follow_redirects=True) as client:
            hosts, svcs = await self._sysinfo(client)
            published = await self._published(client, hosts, svcs)
            cands = {o["name"]: office_candidates(o, hosts) for o in offices}
            for name, ips in published_candidates(offices, published, hosts).items():
                cands[name] = cands.get(name, []) + [ip for ip in ips if ip not in cands.get(name, [])]
            mesh = mesh_candidates(hosts, svcs)
            probe_ips = set(mesh) | {ip for ips in cands.values() for ip in ips}
            own_ips = {ip for ip in probe_ips if is_local_ip(ip)}
            sem = asyncio.Semaphore(16)

            async def probe(ip: str):
                async with sem:
                    return ip, await iax_poke(ip)
            answers = {ip: ms for ip, ms in await asyncio.gather(*(probe(ip) for ip in probe_ips - own_ips))
                       if ms is not None}
            peers = await self._own_peers()
            # our qualified trunks are PBXs that answer, even when POKE got no reply
            peer_ok = {p["host"] for p in peers if p["ok"] and _mesh_ip(p.get("host") or "")}
            # locate every PBX we try: through the node it sits behind
            node_ips = set()
            for ip in probe_ips | {p["host"] for p in peers if _mesh_ip(p.get("host") or "")}:
                n = node_of(hosts, host_index(hosts, ip))
                if n and (ip in answers or ip in own_ips or ip in peer_ok
                          or any(ip == ips[0] for ips in cands.values() if ips)):
                    node_ips.add(n["ip"])
            sem2 = asyncio.Semaphore(8)

            async def ninfo(ip):
                async with sem2:
                    return ip, await self._node_info(client, ip)
            nodes = dict(await asyncio.gather(*(ninfo(ip) for ip in node_ips)))
            # PBXs whose node the host list doesn't give reliably: trace the route,
            # the hop before the PBX is its node (also for PBXs not in any node's list)
            located = set(answers) | own_ips | peer_ok
            unsure = []
            for ip in located:
                n = node_of(hosts, host_index(hosts, ip))
                info = nodes.get((n or {}).get("ip", ""), {})
                if not info or info.get("lat") is None or (info.get("lan") and _lan(info["lan"]) != _lan(ip)):
                    unsure.append(ip)
            sem3 = asyncio.Semaphore(6)

            async def tr(ip):
                async with sem3:
                    return ip, await trace_node(ip)
            node_for = {ip: hop for ip, hop in await asyncio.gather(*(tr(ip) for ip in unsure if ip not in own_ips))
                        if hop}
            extra = set(node_for.values()) - set(nodes)
            nodes.update(dict(await asyncio.gather(*(ninfo(ip) for ip in extra))))
            for n in nodes.values():
                if n.get("lat") is not None:
                    n["place"] = await self._place(client, n["lat"], n["lon"])
            people = await self._whitepages(client, sorted(set(answers) | own_ips))
            # people's phones on the mesh: one SIP OPTIONS each
            phone_ips = {p["ip"] for ps in handset_hosts(hosts).values() for p in ps}
            phone_ips |= set(advertised_phone_ips(hosts, svcs).values())
            phone_ips = sorted(phone_ips - own_ips)[:MAX_SIP_PROBES]

            async def ping(ip: str):
                async with sem:
                    return ip, await sip_ping(ip)
            sip_answers = {ip: ms for ip, ms in await asyncio.gather(*(ping(ip) for ip in phone_ips))
                           if ms is not None}
            log.info("MeshPhone map: %d of %d phones on the mesh answered a SIP ping",
                     len(sip_answers), len(phone_ips))
            try:
                reports = await self._reports(client)
            except Exception as exc:
                log.info("MeshPhone map: trunk reports unavailable (%s)", exc)
                reports = []
        own = next((o for o in offices if o["name"] == cfg["own_office"]), {})
        registered = own_numbers(self._own_endpoints(), (own.get("office_codes") or [""])[0])
        data = build_map(offices, hosts, svcs, cands, mesh, answers, nodes, own_ips,
                         cfg["own_office"], peers, people, reports, node_for=node_for,
                         own_registered=registered, sip_answers=sip_answers, published=published)
        data["people_created"] = self.people_created
        self.data, self.last_error = data, None
        for path, obj in ((self.cache_path, data), (self.places_path, self._places)):
            if path:
                try:
                    with open(path + ".tmp", "w", encoding="utf-8") as f:
                        json.dump(obj, f)
                    os.replace(path + ".tmp", path)
                except OSError as exc:
                    log.debug("MeshPhone map: cache write failed: %s", exc)
        s = data["summary"]
        log.info("MeshPhone map: %d offices (%s), %d mesh-only, %d node-located, %d location off, %d links (%s)",
                 s["offices"], s["office_states"], s["mesh_only"], s["node_located"], s["location_off"],
                 s["links"], s["link_states"])
        return data


service = MeshPhoneMap()
