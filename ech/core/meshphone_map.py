"""
ech/core/meshphone_map.py
-------------------------
A MeshPhone network map built from measurements, not just from what each
office's admin once typed into the WT0F MeshPhone database.

The WT0F map (wt0f-meshphone.local.mesh/network_map.html) colours most links
grey ("Unknown") and many PBXs "Unknown" because those fields come from PBX
check-ins, and only about a third of the offices ever check in. Its IP
addresses are also self-reported and often stale (an office listed at an
address that no longer answers, while its PBX answers elsewhere).

What this module adds, measured from this station every few hours:

  * PBX reachability - an IAX2 POKE to each office's address. Any Asterisk
    answers POKE with PONG before authentication, so a PONG proves the PBX is
    up and reachable over the mesh, with its round-trip time. Besides the
    directory address, the mesh-wide host list (AREDN sysinfo hosts) is
    searched for hosts of the office admin's callsign that look like a PBX
    ("k1abc-pbx", "...-phone", "...-allstar"), so moved PBXs are found too.
  * Our own trunks - straight from Asterisk ("iax2 show peers"): these links
    are verified end to end, with Asterisk's own qualify latency.
  * Other links - a trunk between two other PBXs can't be seen from here.
    Each is classified from the evidence there is: listed by both offices or
    by one only, and whether both ends answer. "Both ends up" means the trunk
    can work, not that it is configured.

Nothing is sent but POKEs (and one host-list fetch): a few hundred small UDP
packets per refresh.

Config (radio_directory.meshphone in config.yaml):
  map_enabled    bool  default true
  map_refresh_hours  float  how often to re-measure (default 6, minimum 1)
  own_office     str   this PBX's office name in the directory (e.g. MYTOWN)
  hosts_url      str   AREDN sysinfo with the mesh host list
                       (default: radio_directory.aredn_discovery.sysinfo_url,
                       else the supernode below)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import struct
import time

log = logging.getLogger(__name__)

DEFAULT_HOSTS_URL = "http://10.160.184.198/a/sysinfo?hosts=1"
REFRESH_S = 6 * 3600          # PBXs rarely move; map_refresh_hours overrides
FIRST_DELAY_S = 120
IAX_PORT = 4569
_IP_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")
# host names that look like a phone system (not a phone handset)
_PBX_HOST_RE = re.compile(r"pbx|asterisk|allstar|hamvoip|meshphone|voip|iax", re.I)
_HANDSET_RE = re.compile(r"yealink|cisco|polycom|grandstream|gxp|t4\d|t5\d|spa\d|ata\b|sip-t", re.I)
MAX_CANDIDATES = 6


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


# ── inputs ───────────────────────────────────────────────────────────────────

def candidate_ips(office: dict, hosts: list[dict]) -> list[str]:
    """Addresses to try for an office: the directory's, then mesh hosts of
    the admin's callsign that look like a PBX."""
    out = []
    if _IP_RE.match(office.get("ip") or ""):
        out.append(office["ip"])
    call = (office.get("admin_callsign") or "").upper()
    hn = (office.get("hostname") or "").split(".")[0].upper()
    for h in hosts:
        name = str(h.get("name") or "")
        ip = str(h.get("ip") or "")
        if not _IP_RE.match(ip) or ip in out:
            continue
        up = name.upper()
        if hn and up == hn:
            out.append(ip)
        elif call and (up.startswith(call + "-") or up == call) and _PBX_HOST_RE.search(name) \
                and not _HANDSET_RE.search(name):
            out.append(ip)
    return out[:MAX_CANDIDATES]


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
    """Which office an Asterisk IAX peer is: by address, then by office name,
    then by the admin callsign (peers are often named after the callsign)."""
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


# ── the map ──────────────────────────────────────────────────────────────────

def build_map(offices: list[dict], cands: dict[str, list[str]], answers: dict[str, dict],
              own_office: str = "", own_peers: list[dict] | None = None,
              generated_at: float | None = None) -> dict:
    """answers: office name -> {"ip": answering ip, "ms": rtt} for offices that
    answered a POKE. Returns {"offices": [...], "links": [...], "summary": {...}}."""
    own_peers = own_peers or []
    names = {o["name"] for o in offices}
    # A trunk of ours that Asterisk qualifies OK proves that PBX is up, at the
    # trunk's address, even when the directory and host list don't know it.
    answers = dict(answers)
    for peer in own_peers:
        other = match_peer(peer, offices, cands)
        if other and other != own_office and peer["ok"] and other not in answers and peer.get("host"):
            answers[other] = {"ip": peer["host"], "ms": peer["ms"]}
    out_offices = []
    for o in offices:
        n = o["name"]
        a = answers.get(n)
        if n == own_office:
            state, ms, ip = "self", None, ""
        elif a:
            state, ms, ip = "up", a.get("ms"), a.get("ip", "")
        elif cands.get(n):
            state, ms, ip = "no_answer", None, ""
        else:
            state, ms, ip = "no_address", None, ""
        out_offices.append({
            "name": n, "location": o.get("location", ""), "lat": o.get("lat"), "lon": o.get("lon"),
            "office_codes": o.get("office_codes", []), "dialplans": o.get("dialplans", []),
            "admin_callsign": o.get("admin_callsign", ""), "admin_name": o.get("admin_name", ""),
            "admin_email": o.get("admin_email", ""),
            "admin_phone": o.get("admin_phone", ""),
            "pbx_type": o.get("pbx_type", ""), "listed_status": o.get("pbx_status", ""),
            "checkin_at": o.get("checkin_at", ""), "listed_ip": o.get("ip", ""),
            "trunks_listed": o.get("trunks", []),
            "state": state, "rtt_ms": ms, "answering_ip": ip,
            "ip_moved": bool(ip and o.get("ip") and ip != o.get("ip")),
            "tried": cands.get(n, []),
        })
    by_name = {o["name"]: o for o in out_offices}

    # links listed in the directory, one per pair
    pairs: dict[tuple[str, str], dict] = {}
    for o in offices:
        for t in o.get("trunks", []):
            if t == o["name"]:
                continue
            key = tuple(sorted((o["name"], t)))
            p = pairs.setdefault(key, {"a": key[0], "b": key[1], "listed_by": []})
            if o["name"] not in p["listed_by"]:
                p["listed_by"].append(o["name"])
    # our own trunks, from Asterisk
    own_links = {}
    for peer in own_peers:
        other = match_peer(peer, offices, cands)
        if not other or not own_office or other == own_office:
            continue
        key = tuple(sorted((own_office, other)))
        pairs.setdefault(key, {"a": key[0], "b": key[1], "listed_by": []})
        own_links[key] = peer

    links = []
    for key, p in sorted(pairs.items()):
        a, b = by_name.get(p["a"]), by_name.get(p["b"])
        lk = {**p, "mutual": len(p["listed_by"]) == 2, "rtt_ms": None, "peer": ""}
        if a is None or b is None:
            lk["state"] = "unlisted_end"          # a trunk to a name with no directory entry
        elif key in own_links:
            peer = own_links[key]
            lk["peer"] = peer["name"]
            lk["state"] = "verified" if peer["ok"] else "down"
            lk["rtt_ms"] = peer["ms"]
        else:
            sa, sb = a["state"], b["state"]
            up = {"up", "self"}
            if sa in up and sb in up:
                lk["state"] = "both_up"
            elif "no_answer" in (sa, sb):
                lk["state"] = "end_down"
            else:
                lk["state"] = "unknown"
        links.append(lk)

    states = {}
    for o in out_offices:
        states[o["state"]] = states.get(o["state"], 0) + 1
    lstates = {}
    for lk in links:
        lstates[lk["state"]] = lstates.get(lk["state"], 0) + 1
    return {
        "generated_at": generated_at or time.time(),
        "measured_from": own_office,
        "offices": out_offices,
        "links": links,
        "summary": {"offices": len(out_offices), "office_states": states,
                    "links": len(links), "link_states": lstates,
                    "moved": sum(1 for o in out_offices if o["ip_moved"]),
                    "unlisted_trunk_names": sorted({x for p in pairs.values() for x in (p["a"], p["b"])} - names)},
    }


# ── service ──────────────────────────────────────────────────────────────────

class MeshPhoneMap:
    def __init__(self) -> None:
        self.data: dict | None = None
        self.last_error: str | None = None
        self.enabled = True
        self._config: dict = {}
        self._router = None
        self._task: asyncio.Task | None = None
        self.cache_path: str | None = None
        self.refresh_s = REFRESH_S

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
        try:
            with open(self.cache_path, encoding="utf-8") as f:
                self.data = json.load(f)
        except (OSError, ValueError):
            pass

    def _cfg(self) -> dict:
        rd = self._config.get("radio_directory") or {}
        mp = rd.get("meshphone") or {}
        disc = rd.get("aredn_discovery") or {}
        return {"own_office": str(mp.get("own_office") or "").upper(),
                "hosts_url": str(mp.get("hosts_url") or disc.get("sysinfo_url") or DEFAULT_HOSTS_URL)}

    async def start(self, config: dict, router=None) -> None:
        self.configure(config, router)
        if self.enabled and self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        age = time.time() - float((self.data or {}).get("generated_at") or 0)
        await asyncio.sleep(max(FIRST_DELAY_S, self.refresh_s - age))   # a restart doesn't re-measure early
        while True:
            try:
                await self.refresh()
            except Exception as exc:
                self.last_error = f"{exc.__class__.__name__}: {exc}"
                log.warning("MeshPhone map refresh failed: %s", self.last_error)
            await asyncio.sleep(self.refresh_s)

    async def _hosts(self) -> list[dict]:
        import httpx
        url = self._cfg()["hosts_url"]
        if "hosts=1" not in url:
            url += ("&" if "?" in url else "?") + "hosts=1"
        async with httpx.AsyncClient(timeout=40, follow_redirects=True) as c:
            r = await c.get(url)
            r.raise_for_status()
            hosts = r.json().get("hosts") or []
        return [h for h in hosts if isinstance(h, dict)]

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
        from ech.core.directory_status import status as live
        offices = list(live.mp_offices)
        if not offices:
            self.last_error = "no MeshPhone office list yet"
            return self.data
        cfg = self._cfg()
        try:
            hosts = await self._hosts()
        except Exception as exc:
            log.info("MeshPhone map: mesh host list unavailable (%s) - directory addresses only", exc)
            hosts = []
        cands = {o["name"]: candidate_ips(o, hosts) for o in offices}
        sem = asyncio.Semaphore(16)

        async def probe(name: str, ips: list[str]):
            for ip in ips:
                async with sem:
                    ms = await iax_poke(ip)
                if ms is not None:
                    return name, {"ip": ip, "ms": ms}
            return name, None

        results = await asyncio.gather(*(probe(n, ips) for n, ips in cands.items()
                                        if ips and n != cfg["own_office"]))
        answers = {n: a for n, a in results if a}
        data = build_map(offices, cands, answers, cfg["own_office"], await self._own_peers())
        self.data, self.last_error = data, None
        if self.cache_path:
            tmp = self.cache_path + ".tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f)
                os.replace(tmp, self.cache_path)
            except OSError as exc:
                log.debug("MeshPhone map: cache write failed: %s", exc)
        s = data["summary"]
        log.info("MeshPhone map: %d offices (%s), %d links (%s), %d moved", s["offices"], s["office_states"],
                 s["links"], s["link_states"], s["moved"])
        return data


service = MeshPhoneMap()
