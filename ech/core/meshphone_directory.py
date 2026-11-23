"""
ech/core/meshphone_directory.py
-------------------------------
AREDN MeshPhone directory data, fetched over the mesh by directory_status.py:

  * Offices: the WT0F "MeshPhone Database 2.0" export
    (http://wt0f-meshphone.local.mesh/api/network_json) - one JSON document
    per PBX with its office codes, the number patterns it answers, trunks
    and check-in status. This is the live routing guide that MeshPhone's own
    meshphone_gen.php reads (gitlab.com/aredn-apps/meshphone-generators).

  * People ("white pages"): N2MH's WhitePages XML (<WhitePages><listing>...).
    Its original host (n2mh-meshphone2) is gone, but PBXs that ran the
    aredn-apps/whitepages viewer still serve their last hourly copy at
    /whitepages.xml (WT0F-freepbx, ka4ema-pbx-cha; both dated 2025-10-13 when
    checked 2026-11-12). All configured copies are tried and the one with the
    newest <created> date wins.

`.local.mesh` names don't resolve through the ECH box's normal DNS (the
VLAN's DHCP DNS is deliberately not installed), so hosts under .local.mesh
are looked up directly against the mesh node's DNS server: either the
configured `dns_server`, or (`auto`) the domain-name-servers of the DHCP
lease whose domain is local.mesh.

Only what the directory needs is kept: office name, location (and its map
coordinates), codes, number patterns, trunks, status, IP/hostname/PBX type,
admin callsign, name + MeshPhone number; for people the
callsign, name, MeshPhone number and home PBX (callsigns and names are public
on the AREDN mesh, as is the admin email the office published for MeshPhone
contact). People's notes and MeshMail addresses are dropped. This is personal contact data published for
MeshPhone users: it lives in the server-side cache file only, never in the
repo.

Config (config.yaml):
  radio_directory:
    meshphone:
      enabled: true
      offices_url: http://wt0f-meshphone.local.mesh/api/network_json
      whitepages_urls:
        - http://WT0F-freepbx.local.mesh/whitepages.xml
        - http://ka4ema-pbx-cha.local.mesh/whitepages.xml
      dns_server: auto          # or an IP, e.g. the AREDN node's address
"""

from __future__ import annotations

import glob
import logging
import random
import re
import socket
import struct
import xml.etree.ElementTree as ET
from urllib.parse import unquote_plus, urlsplit, urlunsplit

log = logging.getLogger(__name__)

DEFAULT_OFFICES_URL = "http://wt0f-meshphone.local.mesh/api/network_json"
DEFAULT_WHITEPAGES_URLS = [
    "http://WT0F-freepbx.local.mesh/whitepages.xml",
    "http://ka4ema-pbx-cha.local.mesh/whitepages.xml",
]
_LEASE_GLOB = "/var/lib/dhcp/dhclient*.leases"
_NUMBER_RE = re.compile(r"^\d{7}$")
DNS_PORT = 53


def settings(config: dict) -> dict:
    cfg = ((config.get("radio_directory") or {}).get("meshphone") or {})
    urls = cfg.get("whitepages_urls")
    return {
        "enabled": bool(cfg.get("enabled", True)),
        "offices_url": str(cfg.get("offices_url") or DEFAULT_OFFICES_URL),
        "whitepages_urls": [str(u) for u in urls] if isinstance(urls, list) else list(DEFAULT_WHITEPAGES_URLS),
        "dns_server": str(cfg.get("dns_server") or "auto"),
    }


# ── mesh DNS ────────────────────────────────────────────────────────────────

def mesh_dns_from_leases(pattern: str = _LEASE_GLOB) -> str | None:
    """DNS server of the most recent dhclient lease for the local.mesh domain."""
    found = None
    for path in sorted(glob.glob(pattern)):
        try:
            text = open(path, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        for block in re.findall(r"lease\s*\{(.*?)\}", text, re.S):
            if not re.search(r'option domain-name "local\.mesh\.?"', block):
                continue
            m = re.search(r"option domain-name-servers ([0-9.]+)", block)
            if m:
                found = m.group(1)   # later leases in the file are newer
    return found


def _dns_query(name: str, server: str, timeout: float = 3.0) -> str | None:
    """Minimal DNS A lookup (stdlib only): one UDP question, first A answer."""
    qid = random.randint(0, 0xFFFF)
    q = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0)
    for label in name.rstrip(".").split("."):
        b = label.encode("idna")
        q += bytes([len(b)]) + b
    q += b"\x00" + struct.pack(">HH", 1, 1)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.settimeout(timeout)
        s.sendto(q, (server, DNS_PORT))
        data, _ = s.recvfrom(4096)
    except OSError:
        return None
    finally:
        s.close()
    if len(data) < 12:
        return None
    rid, flags, qd, an = struct.unpack(">HHHH", data[:8])
    if rid != qid or flags & 0x000F or an == 0:
        return None
    pos = 12

    def skip_name(p: int) -> int:
        while p < len(data):
            n = data[p]
            if n == 0:
                return p + 1
            if n & 0xC0 == 0xC0:
                return p + 2
            p += n + 1
        return p

    for _ in range(qd):
        pos = skip_name(pos) + 4
    for _ in range(an):
        pos = skip_name(pos)
        if pos + 10 > len(data):
            return None
        rtype, rclass, _ttl, rdlen = struct.unpack(">HHIH", data[pos:pos + 10])
        pos += 10
        if rtype == 1 and rclass == 1 and rdlen == 4:
            return socket.inet_ntoa(data[pos:pos + 4])
        pos += rdlen
    return None


def resolve_url(url: str, dns_server: str) -> tuple[str, dict]:
    """(url to fetch, extra headers). A .local.mesh host is swapped for its
    mesh IP, with the original name sent as the Host header."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if not host.lower().endswith(".local.mesh"):
        return url, {}
    server = mesh_dns_from_leases() if dns_server == "auto" else dns_server
    if not server:
        raise RuntimeError(f"can't resolve {host}: no mesh DNS server (no local.mesh DHCP lease; set dns_server)")
    ip = _dns_query(host, server)
    if not ip:
        raise RuntimeError(f"{host} not found in mesh DNS ({server})")
    netloc = ip + (f":{parts.port}" if parts.port else "")
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment)), {"Host": host}


# ── parsers ─────────────────────────────────────────────────────────────────

def _s(v, limit: int = 80) -> str:
    return str(v if v is not None else "").strip()[:limit]


def _digits(v) -> str:
    return re.sub(r"\D", "", str(v or ""))


_EMAIL_RE = re.compile(r"^[^@\s<>\"']+@[^@\s<>\"']+\.[A-Za-z0-9.-]+$")
_IP_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


def _coord(v, limit: float) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return round(f, 4) if -limit <= f <= limit and f != 0 else None


def parse_offices(data) -> list[dict]:
    if not isinstance(data, list):
        raise ValueError("office list is not a JSON array")
    out = []
    for o in data:
        if not isinstance(o, dict) or not o.get("name"):
            continue
        codes, patterns = [], []
        for c in o.get("office_codes") or []:
            if isinstance(c, dict):
                if c.get("office_code"):
                    codes.append(_s(c["office_code"], 8))
                patterns += [_s(p, 20) for p in (c.get("dialplans") or []) if p]
            elif c:
                codes.append(_s(c, 8))
        admin_phone = _digits(o.get("admin_phone"))
        out.append({
            "name": _s(o.get("name"), 40),
            "location": _s(o.get("location")),
            "office_codes": codes,
            "dialplans": patterns[:20],
            "trunks": [_s(t.get("pbx_name"), 40) for t in (o.get("trunks") or [])
                       if isinstance(t, dict) and t.get("pbx_name")],
            "pbx_status": _s(o.get("pbx_status"), 20),
            "checkin_at": _s(o.get("checkin_at"), 40),
            "admin_callsign": _s(o.get("admin_callsign"), 12).upper(),
            "admin_name": _s(o.get("admin_name"), 60),   # public on the mesh, like the callsign
            "admin_email": _s(o.get("admin_email"), 80) if _EMAIL_RE.match(_s(o.get("admin_email"), 80)) else "",
            "admin_phone": admin_phone if _NUMBER_RE.match(admin_phone) else "",
            # for the MeshPhone map (meshphone_map.py): where it is and how to reach it
            "lat": _coord(o.get("latitude"), 90),
            "lon": _coord(o.get("longitude"), 180),
            "ip": _s(o.get("ip_address"), 15) if _IP_RE.match(_s(o.get("ip_address"), 15)) else "",
            "hostname": _s(o.get("hostname"), 63),
            "pbx_type": _s(o.get("pbx_type"), 20),
            "network": _s(o.get("network"), 20),
            "updated_at": _s(o.get("updated_at"), 40),
        })
    out.sort(key=lambda o: o["name"])
    return out


def parse_whitepages(text: str) -> tuple[list[dict], str]:
    """(people, created) from a WhitePages XML document. Values in the file
    are URL-encoded ("Mark+Herson"), so they're decoded."""
    if re.search(r"<!(DOCTYPE|ENTITY)", text[:4096], re.I):
        raise ValueError("white pages XML has a DTD — refusing to parse")
    root = ET.fromstring(text.encode("utf-8") if isinstance(text, str) else text)
    if root.tag != "WhitePages":
        raise ValueError(f"not a WhitePages document (root <{root.tag}>)")
    created = unquote_plus(root.findtext("administration/created") or "").strip()
    people = []
    for li in root.findall("listing"):
        call = unquote_plus(li.findtext("callsign") or "").strip().upper()
        number = _digits(unquote_plus(li.findtext("meshphone") or ""))
        if not call or not _NUMBER_RE.match(number):
            continue
        people.append({
            "callsign": call[:12],
            "name": unquote_plus(li.findtext("opname") or "").strip()[:60],
            "number": number,
            "pbx": unquote_plus(li.findtext("pbx") or "").strip()[:40],
        })
    people.sort(key=lambda p: (p["callsign"], p["number"]))
    return people, created


def dial_string(number: str) -> str:
    """What ECH dials for a 7-digit MeshPhone number: 78 + number, which the
    PBX dialplan sends straight into the MeshPhone context."""
    return "78" + number
