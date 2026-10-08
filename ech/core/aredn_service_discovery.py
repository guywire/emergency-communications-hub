"""
ech/core/aredn_service_discovery.py
--------------------------------------
O101/queue: discover other PBX/phone services already advertised across an
AREDN mesh, via a local AREDN node's `sysinfo.json` endpoint, instead of
requiring the operator to hand-type every entry into radio_directory.py's
config-extension block.

Confirmed real (2026-10-08) against the operator's own live AREDN
supernode — this is NOT guessed from docs:
  GET http://<local-node>/cgi-bin/sysinfo.json?services=1&services_local=1
  -> 307 redirect to /a/sysinfo?... on current (20260921) AREDN firmware;
     follow it (httpx does this by default).
  Response JSON relevant keys:
    "services"       — mesh-WIDE service list via OLSR propagation, not
                        just the directly-connected node. On the live
                        supernode checked, this had 1579 entries from
                        across the whole connected mesh.
    "services_local" — services advertised by THIS node specifically
                        (empty on the node checked — nothing locally
                        configured beyond what already propagates in).
  Each entry: {"name": str, "ip": str, "link": str, "protocol": str}
    `link` is often empty (plain free-text "name" only, e.g. "Dial by IP:
    10.27.133.5 [phone]") — there is NO structured phone-number/SIP field,
    just whatever text the remote node's admin typed into AREDN's "Local
    Services" setup page. This is real, operator-entered data across many
    different conventions (an AllStar node number, a SIP URI, a "dial by
    IP" star-code instruction, a bare 10-digit number, a HOIP/AmateurWire/
    NZSIP extension reference, etc.) — there is no way to reliably derive
    a single dialable "destination" field from free text this
    heterogeneous, so this module does NOT attempt to guess one beyond the
    one case that IS structured (`sip:` URIs). Everything else surfaces as
    reference information only (name + link + source IP), left for the
    operator to read and act on manually — exactly the same honesty stance
    radio_directory.py already takes for its own built-in entries.

IMPORTANT connectivity caveat, left for the operator's awareness rather
than silently assumed away: reaching a local node's own sysinfo.json over
the regular LAN (true on this project's live box) does NOT by itself mean
every discovered mesh host (10.x.x.x) is reachable for an actual call —
real OLSR mesh routing into the 10.x.x.x space is a separate thing from
one dual-homed supernode answering HTTP on the LAN. If the operator's
Asterisk box has no mesh route, discovered entries will surface correctly
here but calls to them will simply fail/timeout, same as any other
unreachable destination.

Config keys (radio_directory.aredn_discovery in config.yaml):
  sysinfo_url   str   full URL to a LOCAL AREDN node's sysinfo.json, e.g.
                       "http://192.168.x.x/cgi-bin/sysinfo.json" (REQUIRED
                       to enable — no default, since which node/IP exists
                       on an operator's own LAN/mesh is entirely
                       deployment-specific; nobody else's network will
                       have the same address)
"""

from __future__ import annotations

import re

import httpx

_PBX_KEYWORDS = (
    "phone", "sip", "allstar", "asl node", "pbx", "voip", "asterisk",
    "hoip", "hams over ip", "amateurwire", "amateur wire", "nzsip",
    "dial by ip", "dial ", "meshphone", "mesh phone",
)

# Real AREDN "Local Services" sip: links are "sip://host[:port]/" — NO
# user@ part in practice (confirmed against live mesh data, e.g.
# "sip://KJ5DFD-home:5060/"), but a user@ part is accepted too since the
# URI scheme technically allows one and some node admin might include it.
_SIP_URI_RE = re.compile(r"^sip://(?:([^@/\s]+)@)?([^/\s:]+)(?::(\d+))?", re.IGNORECASE)


async def fetch_services(sysinfo_url: str, timeout: float = 10.0) -> dict:
    """GET a local AREDN node's sysinfo.json with services included.
    Raises httpx.HTTPError on network/HTTP failure — caller decides how to
    surface that (this module makes no assumption about retry/caching)."""
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        resp = await client.get(sysinfo_url, params={"services": "1", "services_local": "1"})
        resp.raise_for_status()
        return resp.json()


def looks_pbx_like(entry: dict) -> bool:
    """Heuristic only — real AREDN service names are free text typed by
    many different node operators with no fixed convention. False
    negatives (a phone service worded unusually) are expected and fine;
    this is a helpful filter, not a guarantee."""
    haystack = f"{entry.get('name', '')} {entry.get('link', '')}".lower()
    return any(kw in haystack for kw in _PBX_KEYWORDS)


def _extract_sip_destination(link: str) -> str | None:
    """Only `sip:user@host[:port]` is structured enough to treat as an
    actual dialable destination — everything else (bare node numbers in
    free text, 'dial by IP' star-code instructions, etc.) is surfaced as
    reference info only, not auto-converted, since guessing wrong here
    means silently misdialing a stranger's station.

    Returned in "SIP/user@host[:port]" channel-dial format, not the
    "sip:" URI form — both aredn_ami.py's and asterisk_adapter.py's
    originate() already special-case any destination starting with
    "SIP/"/"PJSIP/" as a direct Dial() channel string (vs. a dialplan
    Exten), so this needs no adapter changes to actually be dialable."""
    m = _SIP_URI_RE.match((link or "").strip())
    if not m:
        return None
    user, host, port = m.group(1), m.group(2), m.group(3)
    target = f"{user}@{host}" if user else host
    return f"SIP/{target}:{port}" if port else f"SIP/{target}"


def to_directory_entries(sysinfo: dict) -> list[dict]:
    """Convert a sysinfo.json response into radio_directory-style dicts,
    filtered to PBX/phone-like entries. `destination` is only populated
    for entries with a structured sip: URI — see module docstring."""
    all_services = list(sysinfo.get("services") or []) + list(sysinfo.get("services_local") or [])
    out = []
    for e in all_services:
        if not looks_pbx_like(e):
            continue
        name = (e.get("name") or "").strip()
        if not name:
            continue
        link = e.get("link") or ""
        sip_dest = _extract_sip_destination(link)
        out.append({
            "name": name,
            "destination": sip_dest or "",
            "category": "aredn_pbx",
            "description": (f"Discovered via AREDN mesh service list — node {e.get('ip', '?')}"
                             + (f", link: {link}" if link and not sip_dest else "")),
            "source": "AREDN sysinfo.json live mesh-service discovery",
        })
    return out
