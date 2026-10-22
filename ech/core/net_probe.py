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
    return {"enabled": bool(data.get("enabled", False)), "interval_min": interval,
            "speedtest_interval_min": speed, "targets": targets, "web_checks": web}


class NetProbeService:
    def __init__(self, config: dict, db=None):
        cfg = config.get("network_probe") or {}
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
        if not self.enabled or (self._task and not self._task.done()):
            return
        self._task = asyncio.create_task(self._loop(), name="net-probe")
        log.info("NetProbe: every %.0f min (speed test every %s) to %s",
                 self.interval_min,
                 f"{self.speedtest_interval_min:.0f} min" if self.speedtest_interval_min > 0 else "never",
                 ", ".join([t["label"] for t in self.targets] + [w["label"] for w in self.web_checks]))

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

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
        if self.enabled:
            await self.start()
            self._wake.set()
        else:
            await self.stop()
        log.info("NetProbe: settings updated — %s", "enabled" if self.enabled else "disabled")

    def config(self) -> dict:
        return {"enabled": self.enabled, "interval_min": self.interval_min,
                "speedtest_interval_min": self.speedtest_interval_min,
                "targets": [dict(t) for t in self.targets],
                "web_checks": [dict(w) for w in self.web_checks]}

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
            "targets": [{"label": t["label"], "expect_canada": self._expects_canada(t)}
                        for t in self.targets],
            "web_checks": [{"label": w["label"], "url": w["url"]} for w in self.web_checks],
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
                    if not self._host_for(t) and do_speed:
                        continue  # learn the host from the speed test, trace after
                    await self._trace(t)
                for w in self.web_checks:
                    await self._web_check(w)
                if do_speed:
                    self._last_speed = time.monotonic()
                    for t in self.targets:
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
            site = await self._google_site() if host in GOOGLE_DNS_IPS else ""
            site_c, site_cc = site_city(site)
            if site:
                row["server"] = f"Google DNS site {site.upper()}" + (f" ({site_c})" if site_c else "")
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
                      "bytes": m.get("size_download"), "url": w["url"]}
            colo = ""
            rc2, out2, _ = await self._run(
                [self.curl_bin, "-s", "--max-time", "10", "--",
                 f"{parts.scheme}://{parts.netloc}/cdn-cgi/trace"], timeout=15)
            cm = re.search(r"^colo=([A-Z]{3})$", out2, re.M) if rc2 == 0 else None
            if cm:
                colo = detail["colo"] = cm.group(1)
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
