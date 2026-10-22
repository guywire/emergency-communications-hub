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
    interval_min: 15              # traceroute cadence
    speedtest_interval_min: 15    # speed test cadence (0 = traceroute only)
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


def path_signature(hops: list[dict]) -> str:
    ips = [h["ip"] for h in hops if h["ip"]]
    return hashlib.sha1(" ".join(ips).encode()).hexdigest()[:12] if ips else ""


class NetProbeService:
    def __init__(self, config: dict, db=None):
        cfg = config.get("network_probe") or {}
        self.enabled = bool(cfg.get("enabled", False))
        self.interval_min = max(float(cfg.get("interval_min", 15)), 1.0)
        self.speedtest_interval_min = float(cfg.get("speedtest_interval_min", self.interval_min))
        self.speedtest_bin = cfg.get("speedtest_bin") or "/opt/ech/bin/speedtest"
        self.traceroute_bin = cfg.get("traceroute_bin") or shutil.which("traceroute") or "/usr/sbin/traceroute"
        self.max_hops = int(cfg.get("max_hops", 24))
        self.targets = [dict(t) for t in (cfg.get("targets") or DEFAULT_TARGETS)]
        self._db = db
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._last_speed = 0.0
        self._last_run: str | None = None
        self._running = False
        self._hosts: dict[str, str] = {}   # label -> server host for traceroute
        self._home = cfg.get("home_dir") or os.path.join(
            os.path.dirname(os.path.abspath(getattr(db, "_path", "ech.db"))), ".ookla")

    # ── lifecycle ─────────────────────────────────────────────────────────
    async def start(self) -> None:
        if not self.enabled:
            return
        self._task = asyncio.create_task(self._loop(), name="net-probe")
        log.info("NetProbe: every %.0f min (speed test every %s) to %s",
                 self.interval_min,
                 f"{self.speedtest_interval_min:.0f} min" if self.speedtest_interval_min > 0 else "never",
                 ", ".join(t["label"] for t in self.targets))

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def _loop(self) -> None:
        await asyncio.sleep(90)  # let adapters settle before saturating the link
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                return
            except Exception as exc:
                log.warning("NetProbe: run failed: %s", exc)
            await asyncio.sleep(self.interval_min * 60)

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
        rc, out, err = await self._run(
            [self.speedtest_bin, "--accept-license", "--accept-gdpr",
             "-s", str(t["server_id"]), "-f", "json"], timeout=120)
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
            row.update(
                hops_json=json.dumps(hops),
                hop_count=len(hops),
                final_rtt_ms=replied[-1]["rtt"] if replied else None,
                via_canada=1 if canada else 0,
                path_sig=path_signature(hops),
            )
            prev = await self._db.last_net_probe(t["label"], "traceroute") if self._db else None
            changed = bool(prev and prev.get("path_sig") and prev["path_sig"] != row["path_sig"])
            cities = []
            for h in hops:
                if h["city"] and (not cities or cities[-1] != h["city"]):
                    cities.append(h["city"])
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
