"""AllStar node directory for the desk-phone AllStar panel.

Source: AllStarLink's public node list (the "astdb" Allmon uses),
https://allmondb.allstarlink.org/ -> one node per line, "node|callsign|description|location"
(about 42,000 lines / 1.4 MB, checked 2026-11-21). Downloaded at most once a
day and cached on disk next to the database, so the phone keeps working from
the last good copy when the source is down.

A phone can't page through 42,000 nodes, so the panel offers: nodes in our own
state (taken from our node's own entry, e.g. "63018|KN0O||Rockport Maine"), a
callsign search, and a node-number lookup.
"""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_SOURCE = "https://allmondb.allstarlink.org/"
REFRESH_S = 24 * 3600
_NODE_RE = re.compile(r"^\d{4,7}$")

US_STATES = {
    "alabama": "al", "alaska": "ak", "arizona": "az", "arkansas": "ar", "california": "ca",
    "colorado": "co", "connecticut": "ct", "delaware": "de", "florida": "fl", "georgia": "ga",
    "hawaii": "hi", "idaho": "id", "illinois": "il", "indiana": "in", "iowa": "ia", "kansas": "ks",
    "kentucky": "ky", "louisiana": "la", "maine": "me", "maryland": "md", "massachusetts": "ma",
    "michigan": "mi", "minnesota": "mn", "mississippi": "ms", "missouri": "mo", "montana": "mt",
    "nebraska": "ne", "nevada": "nv", "new hampshire": "nh", "new jersey": "nj", "new mexico": "nm",
    "new york": "ny", "north carolina": "nc", "north dakota": "nd", "ohio": "oh", "oklahoma": "ok",
    "oregon": "or", "pennsylvania": "pa", "rhode island": "ri", "south carolina": "sc",
    "south dakota": "sd", "tennessee": "tn", "texas": "tx", "utah": "ut", "vermont": "vt",
    "virginia": "va", "washington": "wa", "west virginia": "wv", "wisconsin": "wi", "wyoming": "wy",
}
_ABBR = {v: k for k, v in US_STATES.items()}


def parse_astdb(text: str) -> list[dict]:
    out = []
    for line in (text or "").splitlines():
        parts = line.strip().split("|")
        if len(parts) < 2 or not _NODE_RE.match(parts[0].strip()):
            continue
        parts += [""] * (4 - len(parts))
        out.append({"node": parts[0].strip(), "call": parts[1].strip().upper()[:12],
                    "desc": parts[2].strip()[:60], "loc": parts[3].strip()[:60]})
    return out


def state_of(location: str) -> str:
    """US state (full lower-case name) named in a free-text location, or ''.
    "Rockport Maine" / "Hope, Maine" / "Rockland,Me" / "Bangor ME." -> "maine"."""
    loc = (location or "").lower().strip()
    for name in sorted(US_STATES, key=len, reverse=True):      # "west virginia" before "virginia"
        if re.search(r"(^|[^a-z])" + name + r"([^a-z]|$)", loc):
            return name
    m = re.search(r"(?:^|[\s,])([a-z]{2})\.?\s*(?:\d{5})?\s*$", loc)
    return _ABBR.get(m.group(1), "") if m else ""


class AllStarDirectory:
    def __init__(self, cache_path: str | Path, source: str = ""):
        self.cache_path = Path(cache_path)
        self.source = source or DEFAULT_SOURCE
        self.nodes: list[dict] = []
        self.by_node: dict[str, dict] = {}
        self.fetched_at = 0.0
        self.error = ""
        try:
            self._set(parse_astdb(self.cache_path.read_text(encoding="utf-8", errors="replace")))
            self.fetched_at = self.cache_path.stat().st_mtime
        except OSError:
            pass

    def _set(self, rows: list[dict]) -> None:
        self.nodes = rows
        self.by_node = {r["node"]: r for r in rows}

    async def refresh(self, force: bool = False) -> None:
        if not force and time.time() - self.fetched_at < REFRESH_S:
            return
        import httpx
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True,
                                         headers={"User-Agent": "ECH AllStar directory"}) as c:
                r = await c.get(self.source)
            rows = parse_astdb(r.text) if r.status_code == 200 else []
            if len(rows) >= 100:
                self._set(rows)
                self.fetched_at, self.error = time.time(), ""
                try:
                    self.cache_path.write_text(r.text, encoding="utf-8")
                except OSError as e:
                    log.warning("AllStar directory: cache not written: %s", e)
                log.info("AllStar directory: %d nodes", len(rows))
                return
            self.error = f"HTTP {r.status_code}, {len(rows)} nodes"
        except Exception as e:
            self.error = e.__class__.__name__
        self.fetched_at = max(self.fetched_at, time.time() - REFRESH_S + 3600)    # retry in an hour
        log.info("AllStar directory refresh failed (%s); using %d cached nodes", self.error, len(self.nodes))

    def lookup(self, node: str) -> dict | None:
        return self.by_node.get(str(node or "").strip())

    def in_state(self, state: str) -> list[dict]:
        if not state:
            return []
        rows = [r for r in self.nodes if state_of(r["loc"]) == state]
        return sorted(rows, key=lambda r: (r["call"], int(r["node"])))

    def search_call(self, q: str, limit: int = 200) -> list[dict]:
        q = re.sub(r"[^A-Z0-9]", "", (q or "").upper())
        if len(q) < 2:
            return []
        rows = [r for r in self.nodes if r["call"].startswith(q)] or \
               [r for r in self.nodes if q in r["call"]]
        return sorted(rows, key=lambda r: (r["call"], int(r["node"])))[:limit]


def label(r: dict) -> str:
    """One phone menu line: node, callsign, then description or location."""
    extra = r.get("desc") or r.get("loc") or ""
    return f"{r['node']} {r['call']} {extra}".strip()
