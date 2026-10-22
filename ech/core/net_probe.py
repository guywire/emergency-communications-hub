"""
ech/core/net_probe.py
---------------------
Network path probe (O168): periodic speed tests and traceroutes to fixed
targets, to show whether the ISP is backhauling "local" traffic through
Canada instead of handing it off in-region.

Two signals, because a speed test alone can't prove routing:
  * Ookla speed test (official CLI, `speedtest -s <id> -f json`) — throughput
    and latency to a specific server. If the Boston server's ping is no lower
    than the Montreal one, the Boston traffic is almost certainly going the
    long way round.
  * traceroute to the same server — the actual hop list. Carrier router names
    carry location codes (Cogent `ymq`/`yyz` = Montreal/Toronto, Beanfield
    `YUL`, `.ca` domains), so a Canadian hop on the Boston path is direct
    evidence. Each path is hashed (path_sig) so route changes are visible.

The speed test is the expensive half: at gigabit speeds one test moves
~1.5 GB, so it has its own interval (speedtest_interval_min) separate from
the cheap traceroute (interval_min).

Config (config.yaml):
  network_probe:
    enabled: false
    interval_min: 30              # traceroute cadence
    speedtest_interval_min: 30    # speed test cadence (0 = traceroute only)
    speedtest_bin: /opt/ech/bin/speedtest
    targets:
      - {label: Boston,   server_id: 1774}    # Comcast, Boston MA
      - {label: Montreal, server_id: 16045}   # Beanfield, Montreal QC
    # A target whose path crosses a Canadian hop is flagged (logged as a
    # warning) unless expect_canada is set — on by default for "Montreal".
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import time
from datetime import datetime, timezone

log = logging.getLogger(__name__)

DEFAULT_TARGETS = [
    {"label": "Boston", "server_id": 1774},
    {"label": "Montreal", "server_id": 16045},
]

# Location codes seen in carrier router hostnames. Matched against hostname
# tokens (split on . and -, trailing digits stripped: "ccr21.ymq01" -> "ymq").
_CANADA_CODES = {
    "ymq": "Montreal", "yul": "Montreal", "mtl": "Montreal", "mtrl": "Montreal",
    "yyz": "Toronto", "tor": "Toronto", "toro": "Toronto", "yow": "Ottawa",
    "ott": "Ottawa", "yqb": "Quebec City", "qbc": "Quebec City",
    "yhz": "Halifax", "hfx": "Halifax", "yvr": "Vancouver", "van": "Vancouver",
    "ywg": "Winnipeg", "yyc": "Calgary",
}
_US_CODES = {
    "bos": "Boston", "alb": "Albany", "nyc": "New York", "jfk": "New York",
    "lga": "New York", "ewr": "Newark", "phl": "Philadelphia", "iad": "Ashburn",
    "dca": "Washington", "ash": "Ashburn", "ord": "Chicago", "chi": "Chicago",
    "pwm": "Portland ME", "ptld": "Portland ME", "ptldme": "Portland ME",
    "burlvt": "Burlington VT", "manch": "Manchester NH", "prov": "Providence",
}
# Whole words that can appear inside longer tokens
_CANADA_WORDS = {"montreal": "Montreal", "toronto": "Toronto", "ottawa": "Ottawa",
                 "quebec": "Quebec City", "halifax": "Halifax"}
_US_WORDS = {"boston": "Boston", "albany": "Albany", "woburn": "Boston",
             "needham": "Boston", "newyork": "New York", "portland": "Portland ME",
             "burlington": "Burlington VT", "ashburn": "Ashburn"}

# Telco CLLI codes (city4 + state/province2 + building2, then equipment
# digits) as used by Consolidated/Fidium: "burlvtma97w" = Burlington VT.
# Only tokens of that exact shape are tried, so words like "cogentco" can't
# be misread as a CLLI.
_CLLI_RE = re.compile(r"^([a-z]{4})([a-z]{2})[a-z0-9]{0,2}\d")
_CLLI_CITIES = {"burlvt": "Burlington VT", "ptldme": "Portland ME", "bngrme": "Bangor ME",
                "lwstme": "Lewiston ME", "bstnma": "Boston", "mnchnh": "Manchester NH",
                "cncdnh": "Concord NH", "albyny": "Albany", "nycmny": "New York"}
# Canadian CLLIs are only trusted for known cities: a province suffix is too
# easy to hit by accident ("netron1" would otherwise read as Ontario).
_CLLI_CANADA = {"mtrlpq": "Montreal", "mtrlqc": "Montreal", "toroon": "Toronto",
                "otwaon": "Ottawa", "qubcpq": "Quebec City", "shbrpq": "Sherbrooke",
                "hlfxns": "Halifax", "stjhnb": "Saint John NB"}
_STATES = {"me", "nh", "vt", "ma", "ri", "ct", "ny", "nj", "pa", "md", "va", "dc"}

_HOP_RE = re.compile(r"^\s*(\d+)\s+(.*)$")
_PROBE_RE = re.compile(r"(\S+) \(([\d.]+)\)\s+([\d.]+) ms")
_IP_ONLY_RE = re.compile(r"^([\d.]+)\s+([\d.]+) ms")


def hop_location(host: str) -> tuple[str, str]:
    """(city, country) guessed from a router hostname; ('', '') if unknown."""
    h = (host or "").lower()
    if not h or re.fullmatch(r"[\d.]+", h):
        return "", ""
    tokens = re.split(r"[.\-_]", h)
    for t in tokens:
        bare = t.rstrip("0123456789")
        if bare in _CANADA_CODES:
            return _CANADA_CODES[bare], "CA"
        if bare in _US_CODES:
            return _US_CODES[bare], "US"
    for word, city in _CANADA_WORDS.items():
        if word in h:
            return city, "CA"
    for word, city in _US_WORDS.items():
        if word in h:
            return city, "US"
    for t in tokens:
        m = _CLLI_RE.match(t)
        if not m:
            continue
        key = m.group(1) + m.group(2)
        if key in _CLLI_CANADA:
            return _CLLI_CANADA[key], "CA"
        if m.group(2) in _STATES:
            return _CLLI_CITIES.get(key, f"{m.group(1).upper()} {m.group(2).upper()}"), "US"
    if h.endswith(".ca"):
        return "", "CA"
    return "", ""


def parse_traceroute(text: str) -> list[dict]:
    """Parse `traceroute` output into [{n, host, ip, rtt, city, country}].
    A hop with no reply gets host/ip '' and rtt None."""
    hops = []
    for line in text.splitlines():
        m = _HOP_RE.match(line)
        if not m:
            continue
        n, rest = int(m.group(1)), m.group(2)
        host = ip = ""
        rtt = None
        pm = _PROBE_RE.search(rest)
        if pm:
            host, ip, rtt = pm.group(1), pm.group(2), float(pm.group(3))
        else:
            im = _IP_ONLY_RE.search(rest.replace("*", "").strip())
            if im:
                host = ip = im.group(1)
                rtt = float(im.group(2))
        city, country = hop_location(host if host != ip else "")
        hops.append({"n": n, "host": host, "ip": ip, "rtt": rtt,
                     "city": city, "country": country})
    # Trailing run of silent hops past the last reply is just the -m limit
    while hops and not hops[-1]["ip"]:
        hops.pop()
    return hops


def route_cities(hops: list[dict]) -> list[str]:
    out: list[str] = []
    for h in hops:
        if h["city"] and (not out or out[-1] != h["city"]):
            out.append(h["city"])
    return out


def path_signature(hops: list[dict], extra: str = "") -> str:
    """Route identity for change detection. Built from the cities the path
    passes through, not router IPs: load balancing (ECMP) picks a different
    sibling router (..52.78 vs ..52.42) from run to run, which isn't a route
    change. Paths with no named hops fall back to /16 prefixes."""
    parts = route_cities(hops) or sorted({".".join(h["ip"].split(".")[:2]) for h in hops if h["ip"]})
    if extra:
        parts = parts + [extra]
    return hashlib.sha1(" ".join(parts).encode()).hexdigest()[:12] if parts else ""


# ── Google Public DNS site (O170) ─────────────────────────────────────────
# o-o.myaddr.l.google.com TXT returns the egress IP of the Google resolver
# that served us; locations.publicdns.goog TXT lists every egress range with
# its site's airport code (yul = Montreal, iad = Ashburn, ...). Together
# they say which Google site answers 8.8.8.8 for this connection.
GOOGLE_DNS_IPS = {"8.8.8.8", "8.8.4.4"}
# Cloudflare's resolver reports its data centre at https://1.1.1.1/cdn-cgi/trace
# (colo=BOS), the same endpoint every Cloudflare-fronted site has.
CLOUDFLARE_DNS_IPS = {"1.1.1.1", "1.0.0.1"}


def parse_google_locations(txt: str) -> list[tuple]:
    import ipaddress
    tok = txt.replace('"', " ").split()
    nets = []
    for i in range(0, len(tok) - 1):
        if "/" in tok[i] and re.fullmatch(r"[a-z]{3}", tok[i + 1]):
            try:
                nets.append((ipaddress.ip_network(tok[i], strict=False), tok[i + 1]))
            except ValueError:
                pass
    return nets


def google_site_for(ips: list[str], nets: list[tuple]) -> str:
    """Most common site code among the resolver egress IPs ('' if unknown)."""
    import ipaddress
    from collections import Counter
    seen = Counter()
    for ip in ips:
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            continue
        for n, code in nets:
            if a.version == n.version and a in n:
                seen[code] += 1
                break
    return seen.most_common(1)[0][0] if seen else ""


def site_city(code: str) -> tuple[str, str]:
    """(city, country) for an airport-style site code (Google / Cloudflare)."""
    c = (code or "").lower()
    if c in _CANADA_CODES:
        return _CANADA_CODES[c], "CA"
    if c in _US_CODES:
        return _US_CODES[c], "US"
    return "", ""


# ── Mesh repeater pings (O174) ────────────────────────────────────────────
# Repeater-class roles: MeshCore contact type REP; Meshtastic device roles.
MESH_REPEATER_ROLES = {"REP", "ROUTER", "REPEATER", "ROUTER_LATE", "ROUTER_CLIENT"}


def _haversine_km(lat1, lon1, lat2, lon2) -> float:
    import math
    r = math.radians
    a = (math.sin(r(lat2 - lat1) / 2) ** 2
         + math.cos(r(lat1)) * math.cos(r(lat2)) * math.sin(r(lon2 - lon1) / 2) ** 2)
    return 6371.0 * 2 * math.asin(math.sqrt(a))


def pick_local_repeaters(nodes, base: tuple | None, count: int, max_age_hours: float,
                         exclude: set | None = None, now=None) -> list[tuple]:
    """The `count` most local repeaters heard within max_age_hours, as
    [(node, distance_km | None)]. Repeaters with a position rank by distance
    from the hub; any shortfall is filled by the strongest-SNR repeaters
    without one (still local: we hear them well)."""
    now = now or datetime.now(timezone.utc)
    exclude = exclude or set()
    fresh = []
    for n in nodes:
        if n.node_id in exclude or (n.meta or {}).get("node_type") not in MESH_REPEATER_ROLES:
            continue
        if not n.last_heard or (now - n.last_heard).total_seconds() > max_age_hours * 3600:
            continue
        fresh.append(n)
    placed, unplaced = [], []
    for n in fresh:
        if base and n.lat and n.lon:
            placed.append((n, round(_haversine_km(base[0], base[1], n.lat, n.lon), 1)))
        else:
            unplaced.append((n, None))
    placed.sort(key=lambda x: x[1])
    unplaced.sort(key=lambda x: -(x[0].snr if x[0].snr is not None else -99))
    return (placed + unplaced)[:count]


_LABEL_RE = re.compile(r"^[\w .\-]{1,24}$")
# Must not start with "-": the host is passed to traceroute as an argument
_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-:]{0,252}$")
_URL_RE = re.compile(r"^https?://[^\s\"'<>]{1,490}$")


def validate_config(data: dict) -> dict:
    """Normalise a Settings-page network_probe payload; raises ValueError with
    a user-facing message on bad input."""
    interval = float(data.get("interval_min", 30))
    if not 5 <= interval <= 1440:
        raise ValueError("traceroute interval must be 5–1440 minutes")
    speed = float(data.get("speedtest_interval_min", interval))
    if speed != 0 and not 15 <= speed <= 10080:
        raise ValueError("speed-test interval must be 0 (off) or 15–10080 minutes")
    targets = []
    for t in data.get("targets") or []:
        label = str(t.get("label", "")).strip()
        if not _LABEL_RE.match(label):
            raise ValueError(f"bad target name {label!r} (letters, digits, space . - only, max 24)")
        sid = t.get("server_id")
        if sid in (None, "", "auto"):
            sid = None
        else:
            try:
                sid = int(sid)
            except (TypeError, ValueError):
                raise ValueError(f"{label}: server ID must be a number (or blank for automatic)") from None
        row = {"label": label, "server_id": sid}
        host = str(t.get("trace_host") or "").strip()
        if host:
            if not _HOST_RE.match(host):
                raise ValueError(f"{label}: trace host must be a hostname or IP address")
            row["trace_host"] = host
        if t.get("speedtest") is False:
            if not host:
                raise ValueError(f"{label}: a traceroute-only target needs a trace host")
            row["speedtest"] = False
        if "expect_canada" in t:
            row["expect_canada"] = bool(t["expect_canada"])
        targets.append(row)
    if not 1 <= len(targets) <= 6:
        raise ValueError("configure 1–6 targets")
    web = []
    for w in data.get("web_checks") or []:
        label = str(w.get("label", "")).strip()
        url = str(w.get("url", "")).strip()
        if not _LABEL_RE.match(label):
            raise ValueError(f"bad web check name {label!r} (letters, digits, space . - only, max 24)")
        if not _URL_RE.match(url):
            raise ValueError(f"{label}: URL must start with http:// or https:// (max 500 characters)")
        web.append({"label": label, "url": url})
    if len(web) > 4:
        raise ValueError("configure at most 4 web page checks")
    labels = [t["label"].lower() for t in targets + web]
    if len(set(labels)) != len(labels):
        raise ValueError("target and web check names must be unique")
    m = data.get("mesh") or {}
    mesh_interval = float(m.get("interval_min", 120))
    if not 30 <= mesh_interval <= 1440:
        raise ValueError("mesh repeater ping interval must be 30–1440 minutes")
    mesh_count = int(m.get("count", 3))
    if not 1 <= mesh_count <= 5:
        raise ValueError("ping 1–5 repeaters per mesh")
    max_age = float(m.get("max_age_hours", 72))
    if not 1 <= max_age <= 720:
        raise ValueError("'heard within' must be 1–720 hours")
    mesh = {"enabled": bool(m.get("enabled", False)), "interval_min": mesh_interval,
            "count": mesh_count, "max_age_hours": max_age}
    return {"enabled": bool(data.get("enabled", False)), "interval_min": interval,
            "speedtest_interval_min": speed, "targets": targets, "web_checks": web,
            "mesh": mesh}


class NetProbeService:
    def __init__(self, config: dict, db=None, router=None):
        cfg = config.get("network_probe") or {}
        m = cfg.get("mesh") or {}
        self.mesh_enabled = bool(m.get("enabled", False))
        self.mesh_interval_min = max(float(m.get("interval_min", 120)), 5.0)
        self.mesh_count = int(m.get("count", 3))
        self.mesh_max_age_hours = float(m.get("max_age_hours", 72))
        self._router = router
        self._mesh_task: asyncio.Task | None = None
        self._mesh_lock = asyncio.Lock()
        self._mesh_last_run: str | None = None
        self.enabled = bool(cfg.get("enabled", False))
        self.interval_min = max(float(cfg.get("interval_min", 30)), 1.0)
        self.speedtest_interval_min = float(cfg.get("speedtest_interval_min", self.interval_min))
        self.speedtest_bin = cfg.get("speedtest_bin") or "/opt/ech/bin/speedtest"
        self.traceroute_bin = cfg.get("traceroute_bin") or shutil.which("traceroute") or "/usr/sbin/traceroute"
        self.max_hops = int(cfg.get("max_hops", 24))
        self.targets = [dict(t) for t in (cfg.get("targets") or DEFAULT_TARGETS)]
        self.web_checks = [dict(w) for w in (cfg.get("web_checks") or [])]
        self.curl_bin = cfg.get("curl_bin") or shutil.which("curl") or "/usr/bin/curl"
        self.dig_bin = cfg.get("dig_bin") or shutil.which("dig") or "/usr/bin/dig"
        self._google_nets: list[tuple] = []
        self._google_nets_at = 0.0
        self._db = db
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._lock = asyncio.Lock()
        self._last_speed = 0.0
        self._last_run: str | None = None
        self._running = False
        self._hosts: dict[str, str] = {}   # label -> server host for traceroute
        self._home = cfg.get("home_dir") or os.path.join(
            os.path.dirname(os.path.abspath(getattr(db, "_path", "ech.db"))), ".ookla")

    # ── lifecycle ─────────────────────────────────────────────────────────
    async def start(self) -> None:
        if self.mesh_enabled and not (self._mesh_task and not self._mesh_task.done()):
            self._mesh_task = asyncio.create_task(self._mesh_loop(), name="net-probe-mesh")
            log.info("NetProbe: pinging the %d most local MeshCore/Meshtastic repeaters every %.0f min",
                     self.mesh_count, self.mesh_interval_min)
        if not self.enabled or (self._task and not self._task.done()):
            return
        self._task = asyncio.create_task(self._loop(), name="net-probe")
        log.info("NetProbe: every %.0f min (speed test every %s) to %s",
                 self.interval_min,
                 f"{self.speedtest_interval_min:.0f} min" if self.speedtest_interval_min > 0 else "never",
                 ", ".join([t["label"] for t in self.targets] + [w["label"] for w in self.web_checks]))

    async def stop(self, mesh: bool = True) -> None:
        tasks = [("_task", self._task)] + ([("_mesh_task", self._mesh_task)] if mesh else [])
        for attr, task in tasks:
            if task:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
                setattr(self, attr, None)

    async def apply_config(self, cfg: dict) -> None:
        """Apply a validate_config() result live (Settings page): starts or
        stops the loop and re-arms the timer for a changed interval."""
        self.interval_min = cfg["interval_min"]
        self.speedtest_interval_min = cfg["speedtest_interval_min"]
        old_ids = {t["label"]: t.get("server_id") for t in self.targets}
        self.targets = [dict(t) for t in cfg["targets"]]
        self.web_checks = [dict(w) for w in cfg.get("web_checks", [])]
        # A new or re-pointed target has a different host to trace
        for t in self.targets:
            if old_ids.get(t["label"]) != t.get("server_id"):
                self._hosts.pop(t["label"], None)
        self.enabled = cfg["enabled"]
        m = cfg.get("mesh") or {}
        mesh_was = self.mesh_enabled
        self.mesh_enabled = bool(m.get("enabled", self.mesh_enabled))
        self.mesh_interval_min = m.get("interval_min", self.mesh_interval_min)
        self.mesh_count = m.get("count", self.mesh_count)
        self.mesh_max_age_hours = m.get("max_age_hours", self.mesh_max_age_hours)
        if mesh_was and not self.mesh_enabled and self._mesh_task:
            self._mesh_task.cancel()
            self._mesh_task = None
        if not self.enabled:
            await self.stop(mesh=False)
        await self.start()
        self._wake.set()
        log.info("NetProbe: settings updated — %s", "enabled" if self.enabled else "disabled")

    def config(self) -> dict:
        return {"enabled": self.enabled, "interval_min": self.interval_min,
                "speedtest_interval_min": self.speedtest_interval_min,
                "targets": [dict(t) for t in self.targets],
                "web_checks": [dict(w) for w in self.web_checks],
                "mesh": {"enabled": self.mesh_enabled, "interval_min": self.mesh_interval_min,
                         "count": self.mesh_count, "max_age_hours": self.mesh_max_age_hours}}

    async def _sleep(self, seconds: float) -> None:
        """Sleep, but wake early when apply_config() changes the schedule."""
        self._wake.clear()
        try:
            await asyncio.wait_for(self._wake.wait(), seconds)
        except asyncio.TimeoutError:
            pass

    async def _loop(self) -> None:
        await asyncio.sleep(90)  # let adapters settle before saturating the link
        last = 0.0
        while True:
            due = last + self.interval_min * 60 - time.monotonic()
            if due > 0:
                await self._sleep(due)
                continue  # re-check: the interval may have changed while asleep
            try:
                await self.run_once()
            except asyncio.CancelledError:
                return
            except Exception as exc:
                log.warning("NetProbe: run failed: %s", exc)
            last = time.monotonic()

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "running": self._running,
            "last_run": self._last_run,
            "interval_min": self.interval_min,
            "speedtest_interval_min": self.speedtest_interval_min,
            "speedtest_available": os.path.isfile(self.speedtest_bin),
            "traceroute_available": os.path.isfile(self.traceroute_bin),
            "targets": [{"label": t["label"], "expect_canada": self._expects_canada(t),
                         "speedtest": t.get("speedtest", True)} for t in self.targets],
            "web_checks": [{"label": w["label"], "url": w["url"]} for w in self.web_checks],
            "mesh": {"enabled": self.mesh_enabled, "interval_min": self.mesh_interval_min,
                     "count": self.mesh_count, "running": self._mesh_lock.locked(),
                     "last_run": self._mesh_last_run},
        }

    # ── one probe cycle ───────────────────────────────────────────────────
    async def run_once(self, force_speed: bool = False) -> None:
        if self._lock.locked():
            return
        async with self._lock:
            self._running = True
            try:
                do_speed = os.path.isfile(self.speedtest_bin) and (
                    force_speed or (self.speedtest_interval_min > 0 and
                                    time.monotonic() - self._last_speed >= self.speedtest_interval_min * 60 - 30))
                # Traceroutes first, while the link is idle, then speed tests
                # one at a time so they don't compete for bandwidth.
                for t in self.targets:
                    if not self._host_for(t) and self._db:
                        last = await self._db.last_net_probe(t["label"], "speedtest")
                        if last and last.get("server_host"):
                            self._hosts[t["label"]] = last["server_host"]
                    if not self._host_for(t) and do_speed and t.get("speedtest", True):
                        continue  # learn the host from the speed test, trace after
                    await self._trace(t)
                for w in self.web_checks:
                    await self._web_check(w)
                if do_speed:
                    self._last_speed = time.monotonic()
                    for t in self.targets:
                        if not t.get("speedtest", True):
                            continue  # traceroute-only target
                        traced = bool(self._host_for(t))
                        await self._speedtest(t)
                        if not traced and self._host_for(t):
                            await self._trace(t)
                self._last_run = datetime.now(timezone.utc).isoformat()
            finally:
                self._running = False

    @staticmethod
    def _expects_canada(t: dict) -> bool:
        """A Canadian target is supposed to route through Canada; only flag the others."""
        return bool(t.get("expect_canada", "montreal" in t["label"].lower()))

    def _host_for(self, t: dict) -> str:
        return t.get("trace_host") or self._hosts.get(t["label"], "")

    async def _run(self, argv: list[str], timeout: float) -> tuple[int, str, str]:
        env = dict(os.environ, HOME=self._home)
        os.makedirs(self._home, exist_ok=True)
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return -1, "", f"timed out after {timeout:.0f}s"
        return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")

    async def _speedtest(self, t: dict) -> None:
        now = datetime.now(timezone.utc).isoformat()
        row = {"ts": now, "target": t["label"], "kind": "speedtest"}
        argv = [self.speedtest_bin, "--accept-license", "--accept-gdpr", "-f", "json"]
        if t.get("server_id"):
            argv += ["-s", str(t["server_id"])]   # else Ookla picks the nearest server
        rc, out, err = await self._run(argv, timeout=120)
        res = None
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("{") and '"type":"result"' in line.replace(" ", ""):
                try:
                    res = json.loads(line)
                except ValueError:
                    pass
        if not res:
            row["error"] = (err.strip() or out.strip() or f"exit {rc}")[-300:]
            log.warning("NetProbe: speed test to %s failed: %s", t["label"], row["error"])
        else:
            srv = res.get("server") or {}
            iface = res.get("interface") or {}
            row.update(
                ping_ms=(res.get("ping") or {}).get("latency"),
                jitter_ms=(res.get("ping") or {}).get("jitter"),
                down_mbps=round((res.get("download") or {}).get("bandwidth", 0) * 8 / 1e6, 1),
                up_mbps=round((res.get("upload") or {}).get("bandwidth", 0) * 8 / 1e6, 1),
                loss_pct=res.get("packetLoss"),
                server=f"{srv.get('name', '')} — {srv.get('location', '')}",
                server_host=srv.get("host") or "",
                external_ip=iface.get("externalIp"),
                isp=res.get("isp"),
                result_url=(res.get("result") or {}).get("url"),
            )
            if row["server_host"]:
                self._hosts[t["label"]] = row["server_host"]
            log.info("NetProbe: %s speed test %.0f↓ / %.0f↑ Mbps, ping %.1f ms (%s)",
                     t["label"], row["down_mbps"], row["up_mbps"], row["ping_ms"] or 0, row["server"])
        if self._db:
            await self._db.insert_net_probe(row)

    async def _trace(self, t: dict) -> None:
        host = self._host_for(t)
        if not host or not os.path.isfile(self.traceroute_bin):
            return
        now = datetime.now(timezone.utc).isoformat()
        row = {"ts": now, "target": t["label"], "kind": "traceroute", "server_host": host}
        rc, out, err = await self._run(
            [self.traceroute_bin, "-q", "2", "-w", "2", "-m", str(self.max_hops), host], timeout=120)
        hops = parse_traceroute(out)
        if not hops:
            row["error"] = (err.strip() or f"exit {rc}")[-300:]
            log.warning("NetProbe: traceroute to %s failed: %s", t["label"], row["error"])
        else:
            replied = [h for h in hops if h["rtt"] is not None]
            canada = [h for h in hops if h["country"] == "CA"]
            if host in GOOGLE_DNS_IPS:
                site, owner = await self._google_site(), "Google DNS site"
            elif host in CLOUDFLARE_DNS_IPS:
                site, owner = await self._cloudflare_colo(f"https://{host}"), "Cloudflare"
            else:
                site, owner = "", ""
            site_c, site_cc = site_city(site)
            if site:
                row["server"] = f"{owner} {site.upper()}" + (f" ({site_c})" if site_c else "")
                if site_cc == "CA":
                    canada = canada or [{"city": site_c}]
            row.update(
                hops_json=json.dumps(hops),
                hop_count=len(hops),
                final_rtt_ms=replied[-1]["rtt"] if replied else None,
                via_canada=1 if canada else 0,
                path_sig=path_signature(hops, site),
            )
            prev = await self._db.last_net_probe(t["label"], "traceroute") if self._db else None
            changed = bool(prev and prev.get("path_sig") and prev["path_sig"] != row["path_sig"])
            cities = route_cities(hops)
            if site:
                cities.append(row["server"])
            msg = "NetProbe: %s traceroute %d hops, %.1f ms, route %s%s%s"
            args = (t["label"], len(hops), row["final_rtt_ms"] or 0,
                    " → ".join(cities) or "?",
                    " — VIA CANADA" if canada else "", " (route changed)" if changed else "")
            if canada and not self._expects_canada(t):
                log.warning(msg, *args)
            else:
                log.info(msg, *args)
        if self._db:
            await self._db.insert_net_probe(row)

    async def _google_site(self) -> str:
        """Airport code of the Google Public DNS site serving us ('' if dig
        is missing or the lookup fails). The location list is cached a day."""
        if not os.path.isfile(self.dig_bin):
            return ""
        if not self._google_nets or time.monotonic() - self._google_nets_at > 86400:
            rc, out, _ = await self._run(
                [self.dig_bin, "+short", "+time=3", "TXT", "locations.publicdns.goog", "@8.8.8.8"], timeout=15)
            nets = parse_google_locations(out) if rc == 0 else []
            if nets:
                self._google_nets, self._google_nets_at = nets, time.monotonic()
        if not self._google_nets:
            return ""
        ips = []
        for _ in range(3):  # several queries: one site fronts several resolvers
            rc, out, _ = await self._run(
                [self.dig_bin, "+short", "+time=3", "TXT", "o-o.myaddr.l.google.com", "@8.8.8.8"], timeout=15)
            ips += [tok for tok in out.replace('"', " ").split() if re.fullmatch(r"[0-9.]+|[0-9a-fA-F:]*:[0-9a-fA-F:]*", tok)]
        return google_site_for(ips, self._google_nets)

    async def _cloudflare_colo(self, base_url: str) -> str:
        """Cloudflare data-centre code (e.g. 'BOS') serving base_url, from its
        /cdn-cgi/trace endpoint; '' if not Cloudflare or unreachable."""
        if not os.path.isfile(self.curl_bin):
            return ""
        rc, out, _ = await self._run(
            [self.curl_bin, "-s", "--max-time", "10", "--", f"{base_url}/cdn-cgi/trace"], timeout=15)
        m = re.search(r"^colo=([A-Z]{3})$", out, re.M) if rc == 0 else None
        return m.group(1) if m else ""

    async def _web_check(self, w: dict) -> None:
        """Time one page fetch with curl's phase timers (DNS, connect, TLS,
        first byte, total). Sites behind Cloudflare also report which edge
        data centre served us (/cdn-cgi/trace colo=), the routing signal.
        Only the page document is fetched: a picture a browser loads after a
        bot check (e.g. Cloudflare "Just a moment...") can't be timed without
        driving a real browser through that check, which this deliberately
        doesn't do."""
        if not os.path.isfile(self.curl_bin):
            return
        from urllib.parse import urlsplit
        now = datetime.now(timezone.utc).isoformat()
        parts = urlsplit(w["url"])
        row = {"ts": now, "target": w["label"], "kind": "web", "server_host": parts.hostname or ""}
        rc, out, err = await self._run(
            [self.curl_bin, "-s", "-L", "--max-time", "30", "-o", os.devnull,
             "-A", "Mozilla/5.0 (X11; Linux x86_64) ECH-netprobe",
             "-w", "%{json}", "--", w["url"]], timeout=40)
        try:
            m = json.loads(out[out.index("{"):]) if "{" in out else {}
        except ValueError:
            m = {}
        if not m or not m.get("time_total"):
            row["error"] = (err.strip() or m.get("errormsg") or f"curl exit {rc}")[-300:]
            log.warning("NetProbe: %s page check failed: %s", w["label"], row["error"])
        else:
            def ms(k):
                return round(float(m.get(k) or 0) * 1000, 1)
            detail = {"dns": ms("time_namelookup"), "connect": ms("time_connect"),
                      "tls": ms("time_appconnect"), "ttfb": ms("time_starttransfer"),
                      "total": ms("time_total"), "code": m.get("http_code"),
                      "bytes": m.get("size_download"), "url": w["url"],
                      "content_type": (m.get("content_type") or "").split(";")[0]}
            colo = await self._cloudflare_colo(f"{parts.scheme}://{parts.netloc}")
            if colo:
                detail["colo"] = colo
            city, cc = site_city(colo)
            row.update(
                ping_ms=detail["ttfb"], final_rtt_ms=detail["total"],
                hops_json=json.dumps(detail), via_canada=1 if cc == "CA" else 0,
                server=(f"Cloudflare {colo}" + (f" ({city})" if city else "")) if colo else "",
            )
            msg = "NetProbe: %s page HTTP %s in %.0f ms (DNS %.0f, connect %.0f, TLS %.0f, first byte %.0f)%s%s"
            args = (w["label"], detail["code"], detail["total"], detail["dns"], detail["connect"],
                    detail["tls"], detail["ttfb"], f", served from {row['server']}" if colo else "",
                    " — VIA CANADA" if cc == "CA" else "")
            (log.warning if cc == "CA" and not self._expects_canada(w) else log.info)(msg, *args)
        if self._db:
            await self._db.insert_net_probe(row)

    # ── Mesh repeater pings (O174) ────────────────────────────────────────
    async def _mesh_loop(self) -> None:
        await asyncio.sleep(180)  # radios need a few minutes to load contacts
        last = 0.0
        while True:
            due = last + self.mesh_interval_min * 60 - time.monotonic()
            if due > 0:
                await self._sleep(due)
                continue
            try:
                await self.run_mesh_once()
            except asyncio.CancelledError:
                return
            except Exception as exc:
                log.warning("NetProbe: mesh ping run failed: %s", exc)
            last = time.monotonic()

    async def _base_location(self, adapter) -> tuple | None:
        lat, lon = getattr(adapter, "_base_lat", None), getattr(adapter, "_base_lon", None)
        if lat is None and self._db and hasattr(self._db, "get_kv"):
            try:
                lat = float(await self._db.get_kv("base_lat") or "nan")
                lon = float(await self._db.get_kv("base_lon") or "nan")
            except (TypeError, ValueError):
                return None
            if lat != lat or lon != lon:  # NaN: not set
                return None
        return (lat, lon) if lat is not None and lon is not None else None

    async def run_mesh_once(self) -> None:
        """Ping the most local repeaters on every connected MeshCore and
        Meshtastic radio, one probe at a time with a gap between them so the
        run never floods the channel."""
        if self._mesh_lock.locked() or not self._router:
            return
        async with self._mesh_lock:
            for name, adapter in list(getattr(self._router, "_adapters", {}).items()):
                if not getattr(adapter, "_connected", False):
                    continue
                if hasattr(adapter, "ping_and_wait"):
                    kind = "meshcore"
                elif hasattr(adapter, "traceroute_and_wait"):
                    kind = "meshtastic"
                else:
                    continue
                nodes = list(getattr(adapter, "_nodes", {}).values())
                own = {getattr(adapter, "_self_node_id", ""), getattr(adapter, "_my_node_id", "")} - {""}
                picks = pick_local_repeaters(nodes, await self._base_location(adapter),
                                             self.mesh_count, self.mesh_max_age_hours, own)
                if not picks:
                    log.info("NetProbe: %s has no repeaters heard in the last %.0f h to ping",
                             name, self.mesh_max_age_hours)
                for i, (node, dist) in enumerate(picks):
                    if i:
                        await asyncio.sleep(15)
                    await self._mesh_ping(name, kind, adapter, node, dist)
            self._mesh_last_run = datetime.now(timezone.utc).isoformat()

    async def _mesh_ping(self, name: str, kind: str, adapter, node, dist) -> None:
        now = datetime.now(timezone.utc).isoformat()
        label = node.display_name or node.node_id
        row = {"ts": now, "target": f"{name}: {label}"[:80], "kind": "mesh",
               "server": label, "server_host": node.node_id}
        t0 = time.monotonic()
        try:
            if kind == "meshcore":
                res = await adapter.ping_and_wait(node.node_id, timeout=30.0)
            else:
                res = await adapter.traceroute_and_wait(node.node_id, timeout=90.0)
        except Exception as exc:
            res = {"status": "error", "detail": str(exc)}
        rtt = res.get("rtt_ms") or round((time.monotonic() - t0) * 1000)
        detail = {"adapter": name, "mesh": kind, "node_id": node.node_id, "distance_km": dist,
                  "snr_heard": node.snr}
        if res.get("status") == "ok":
            detail.update(snrs=res.get("snrs") or [],
                          path=res.get("named") or res.get("route") or [])
            row.update(ping_ms=rtt, final_rtt_ms=rtt, hop_count=res.get("hops"))
            log.info("NetProbe: %s ping %s (%s km): reply in %.1f s, %s hop(s)", name, label,
                     dist if dist is not None else "?", rtt / 1000, res.get("hops"))
        else:
            row["error"] = (res.get("detail") or res.get("status") or "no reply")[:300]
            log.info("NetProbe: %s ping %s: no reply (%s)", name, label, row["error"])
        row["hops_json"] = json.dumps(detail)
        if self._db:
            await self._db.insert_net_probe(row)
