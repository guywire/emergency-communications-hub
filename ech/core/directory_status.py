"""
ech/core/directory_status.py
----------------------------
Live refresh for the PBX directory (radio_directory.py), so the Calls tab
stops offering AllStar nodes that are not on the air.

Two sources, both fetched in bulk (one request each, never per node):

  * HOIP wiki reference lists (hamsoverip.github.io/wiki/reference/):
    test-numbers, conference-bridge-list, rf-links-list, audio-feeds-list.
    Each page is one HTML table, first column = HOIP extension, second =
    name. Re-reading them drops entries HOIP has removed and picks up new
    ones. Only extension, name and description are kept; the pages' owner
    contact column (names, emails) is deliberately not stored.

  * AllStarLink registered-node list (snodes.allstarlink.org/gennodes.php,
    "extnodes" format, lines like `49050=radio@1.2.3.4:4569/49050,1.2.3.4`).
    It holds only nodes currently registered with AllStarLink: checked
    2026-11-12, node 57206 (last registration 2023 per the stats API) was
    absent while 49050 (registered minutes earlier) was present.
    AllStarLink's developer page (allstarlink.github.io/developers/api)
    asks for no more than one fetch per 5 minutes; MIN_INTERVAL_S enforces
    a much longer floor than that.

Status per entry ("asl_status"):
  up       - the entry's AllStar node is in the registered list
  down     - the entry is known to be an AllStar node (name says ALLSTAR,
             a built-in parrot, or `allstar_node:` set in config) and it is
             NOT registered (a HOIP RF link whose node is down won't answer)
  unknown  - no AllStar node behind this entry (conference bridges, audio
             feeds, test numbers, RF links without a node number), or no
             successful AllStarLink fetch yet

Which node is behind a HOIP RF link is read from the trailing 4-7 digit
number in its name ("N8EMA ALLSTAR 49050" -> 49050) - HOIP names them that
way. Built-in "test" parrots dial the AllStar node number directly, so
their destination is the node number.

Also refreshed here: the AREDN MeshPhone office list and white pages
(meshphone_directory.py).

Config (config.yaml):
  radio_directory:
    live_refresh:
      enabled: true          # default on
      interval_min: 60       # floor 15
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import time
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

HOIP_BASE = "https://hamsoverip.github.io/wiki/reference/"
ASL_NODES_URL = "https://snodes.allstarlink.org/gennodes.php"
USER_AGENT = "ECH-directory-refresh/1.0"
MIN_INTERVAL_S = 15 * 60
MANUAL_MIN_GAP_S = 5 * 60   # AllStarLink's stated cache time for the node list

# page -> function(ext) -> category; mirrors radio_directory.py's categories
_PAGES = {
    "test-numbers": lambda ext: "hoip_test",
    "conference-bridge-list": lambda ext: {"1": "hoip_conference_us", "2": "hoip_conference_eu",
                                           "3": "hoip_conference_ap"}.get(ext[:1]),
    "rf-links-list": lambda ext: {"1": "hoip_rf_link_us", "2": "hoip_rf_link_eu",
                                  "3": "hoip_rf_link_ap"}.get(ext[:1]),
    "audio-feeds-list": lambda ext: "hoip_audio_feed",
}
_DESCRIPTIONS = {
    "hoip_test": "HOIP network test/diagnostic number.",
    "hoip_conference_us": "HOIP conference bridge (US).",
    "hoip_conference_eu": "HOIP conference bridge (EU).",
    "hoip_conference_ap": "HOIP conference bridge (Asia-Pacific).",
    "hoip_rf_link_us": "HOIP-linked RF/AllStar node (US).",
    "hoip_rf_link_eu": "HOIP-linked RF/AllStar node (EU).",
    "hoip_rf_link_ap": "HOIP-linked RF/AllStar node (Asia-Pacific).",
    "hoip_audio_feed": "HOIP audio broadcast/dispatch-monitor feed.",
}
# Category order matches radio_directory.HOIP_ALL_ENTRIES so the UI groups
# come out in the same order whether the list is built-in or refreshed.
_CATEGORY_ORDER = list(_DESCRIPTIONS)

_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
_CELL_RE = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_EXT_RE = re.compile(r"^\d{3,7}$")
_TRAILING_NODE_RE = re.compile(r"(?<!\d)(\d{4,7})\s*$")
_ASL_LINE_RE = re.compile(r"^(\d{3,7})=")


def _cell_text(raw: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(_TAG_RE.sub(" ", raw))).strip()


def parse_hoip_page(page: str, text: str) -> list[dict]:
    """Rows of one HOIP reference page -> directory entries. Header and any
    row whose first cell is not an extension number are skipped."""
    categorize = _PAGES[page]
    out = []
    for row in _ROW_RE.findall(text):
        cells = [_cell_text(c) for c in _CELL_RE.findall(row)]
        if len(cells) < 2 or not _EXT_RE.match(cells[0]) or not cells[1]:
            continue
        cat = categorize(cells[0])
        if not cat:
            continue
        # Only the rf-links and conference pages put a description in column 3
        desc = cells[2] if page in ("rf-links-list", "conference-bridge-list") and len(cells) > 2 else ""
        out.append({
            "name": cells[1][:120],
            "destination": cells[0],
            "category": cat,
            "description": (desc[:200] or _DESCRIPTIONS[cat]),
            "source": f"hamsoverip.github.io/wiki/reference/{page}/ (live refresh)",
        })
    return out


def parse_asl_nodes(text: str) -> set[str]:
    return {m.group(1) for line in text.splitlines() if (m := _ASL_LINE_RE.match(line.strip()))}


def allstar_node_for(entry: dict) -> str | None:
    """The AllStar node number behind a directory entry, if there is one."""
    return _node_and_certainty(entry)[0]


def _node_and_certainty(entry: dict) -> tuple[str | None, bool]:
    """(node, certain). `certain` means the entry is known to be an AllStar
    node, so "not registered" may be reported as down. A trailing number in
    an RF-link name that doesn't say ALLSTAR is only a guess: checked
    2026-11-12, "M0UKB EXTENDED FREEDOM NETWORK 23525" has no AllStarLink
    record at all, so it must never be hidden as down."""
    cat = entry.get("category") or ""
    node = entry.get("allstar_node")   # operator-supplied config entries may say so
    if node and str(node).isdigit():
        return str(node), True
    if cat == "test":
        dest = str(entry.get("destination") or "")
        return (dest, True) if dest.isdigit() else (None, False)
    if cat.startswith("hoip_rf_link"):
        name = str(entry.get("name") or "")
        m = _TRAILING_NODE_RE.search(name)
        if m:
            return m.group(1), "ALLSTAR" in name.upper()
    return None, False


class DirectoryStatus:
    """Holds the last refresh in memory and in a small JSON cache file
    (survives restarts, so the Calls tab has status before the first fetch)."""

    def __init__(self) -> None:
        self.enabled = True
        self.interval_s = 3600
        self.cache_path: str | None = None
        self.hoip_entries: list[dict] = []
        self.hoip_refreshed_at: float | None = None
        self.asl_registered: set[str] = set()
        self.asl_refreshed_at: float | None = None
        # AREDN MeshPhone (meshphone_directory.py)
        self.mp_offices: list[dict] = []
        self.mp_offices_refreshed_at: float | None = None
        self.mp_people: list[dict] = []
        self.mp_people_created: str = ""
        self.mp_people_source: str = ""
        self.mp_people_refreshed_at: float | None = None
        self.last_error: str | None = None
        self.last_attempt: float | None = None
        self._config: dict = {}
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    # ── setup ──────────────────────────────────────────────────────────────
    def configure(self, config: dict) -> None:
        self._config = config or {}
        cfg = ((config.get("radio_directory") or {}).get("live_refresh") or {})
        self.enabled = bool(cfg.get("enabled", True))
        try:
            self.interval_s = max(MIN_INTERVAL_S, int(float(cfg.get("interval_min", 60)) * 60))
        except (TypeError, ValueError):
            self.interval_s = 3600
        db_path = (config.get("database") or {}).get("path", "ech.db")
        base = os.path.dirname(os.path.abspath(db_path)) if db_path != ":memory:" else os.getcwd()
        self.cache_path = os.path.join(base, "directory_status.json")
        self._load_cache()

    def _load_cache(self) -> None:
        if not self.cache_path or not os.path.exists(self.cache_path):
            return
        try:
            with open(self.cache_path, encoding="utf-8") as f:
                d = json.load(f)
            self.hoip_entries = [e for e in d.get("hoip_entries", []) if isinstance(e, dict)]
            self.hoip_refreshed_at = d.get("hoip_refreshed_at")
            self.asl_registered = set(d.get("asl_registered", []))
            self.asl_refreshed_at = d.get("asl_refreshed_at")
            mp = d.get("meshphone") or {}
            self.mp_offices = [o for o in mp.get("offices", []) if isinstance(o, dict)]
            self.mp_offices_refreshed_at = mp.get("offices_refreshed_at")
            self.mp_people = [p for p in mp.get("people", []) if isinstance(p, dict)]
            self.mp_people_created = mp.get("people_created") or ""
            self.mp_people_source = mp.get("people_source") or ""
            self.mp_people_refreshed_at = mp.get("people_refreshed_at")
        except Exception as exc:
            log.warning("DirectoryStatus: cache %s unreadable (%s) — starting empty", self.cache_path, exc)

    def _save_cache(self, nodes_of_interest: set[str]) -> None:
        if not self.cache_path:
            return
        # Only the node numbers the directory references are stored, not all
        # ~14k registered nodes.
        d = {
            "hoip_entries": self.hoip_entries,
            "hoip_refreshed_at": self.hoip_refreshed_at,
            "asl_registered": sorted(self.asl_registered & nodes_of_interest),
            "asl_refreshed_at": self.asl_refreshed_at,
            "meshphone": {
                "offices": self.mp_offices,
                "offices_refreshed_at": self.mp_offices_refreshed_at,
                "people": self.mp_people,
                "people_created": self.mp_people_created,
                "people_source": self.mp_people_source,
                "people_refreshed_at": self.mp_people_refreshed_at,
            },
        }
        tmp = self.cache_path + ".tmp"
        try:
            # 0600: the MeshPhone white pages are other operators' contact details
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(d, f)
            os.replace(tmp, self.cache_path)
        except Exception as exc:
            log.warning("DirectoryStatus: could not write %s: %s", self.cache_path, exc)

    async def start(self, config: dict) -> None:
        self.configure(config)
        if self.enabled and not self._task:
            self._task = asyncio.create_task(self._loop(), name="directory-status")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _loop(self) -> None:
        await asyncio.sleep(30)   # let startup settle
        while True:
            try:
                await self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception as exc:   # refresh() records its own errors; this is a last resort
                log.warning("DirectoryStatus: refresh crashed: %s", exc)
            await asyncio.sleep(self.interval_s)

    # ── refresh ────────────────────────────────────────────────────────────
    def can_refresh_now(self) -> tuple[bool, int]:
        if self.last_attempt is None:
            return True, 0
        wait = int(self.last_attempt + MANUAL_MIN_GAP_S - time.time())
        return wait <= 0, max(0, wait)

    async def refresh(self, nodes_of_interest: set[str] | None = None) -> dict:
        """Fetch HOIP lists + AllStarLink registered nodes. Each source keeps
        its previous data if its own fetch fails, so one outage doesn't wipe
        the other."""
        import httpx
        async with self._lock:
            self.last_attempt = time.time()
            errors = []
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True,
                                         headers={"User-Agent": USER_AGENT}) as client:
                fresh: list[dict] = []
                hoip_ok = True
                for page in _PAGES:
                    try:
                        r = await client.get(HOIP_BASE + page + "/")
                        r.raise_for_status()
                        rows = parse_hoip_page(page, r.text)
                        if not rows:
                            raise ValueError("no table rows found (page layout changed?)")
                        fresh.extend(rows)
                    except Exception as exc:
                        hoip_ok = False
                        errors.append(f"HOIP {page}: {exc}")
                if hoip_ok:
                    fresh.sort(key=lambda e: (_CATEGORY_ORDER.index(e["category"]), e["destination"]))
                    self.hoip_entries = fresh
                    self.hoip_refreshed_at = time.time()
                try:
                    r = await client.get(ASL_NODES_URL)
                    r.raise_for_status()
                    nodes = parse_asl_nodes(r.text)
                    # A truncated/garbled download would mark everything down;
                    # the real list holds ~14,500 nodes.
                    if len(nodes) < 1000:
                        raise ValueError(f"only {len(nodes)} nodes in list — not trusting it")
                    self.asl_registered = nodes
                    self.asl_refreshed_at = time.time()
                except Exception as exc:
                    errors.append(f"AllStarLink node list: {exc}")
                errors += await self._refresh_meshphone(client)
            self.last_error = "; ".join(errors) or None
            if errors:
                log.warning("DirectoryStatus: %s", self.last_error)
            interest = set(nodes_of_interest or ())
            interest |= {n for e in self.hoip_entries if (n := allstar_node_for(e))}
            from ech.core.radio_directory import BUILTIN_HAMVOIP_ENTRIES
            interest |= {n for e in BUILTIN_HAMVOIP_ENTRIES if (n := allstar_node_for(e))}
            cfg_entries = ((self._config.get("radio_directory") or {}).get("hamvoip_entries") or [])
            interest |= {n for e in cfg_entries if isinstance(e, dict) and (n := allstar_node_for(e))}
            self._save_cache(interest)
            return self.status()

    async def _refresh_meshphone(self, client) -> list[str]:
        """AREDN MeshPhone offices + white pages, over the mesh. Returns errors."""
        from ech.core import meshphone_directory as mp
        cfg = mp.settings(self._config)
        if not cfg["enabled"]:
            return []
        errors = []
        try:
            url, hdrs = await asyncio.to_thread(mp.resolve_url, cfg["offices_url"], cfg["dns_server"])
            r = await client.get(url, headers=hdrs)
            r.raise_for_status()
            offices = mp.parse_offices(r.json())
            if not offices:
                raise ValueError("no offices in the list")
            self.mp_offices = offices
            self.mp_offices_refreshed_at = time.time()
        except Exception as exc:
            errors.append(f"MeshPhone offices: {exc}")
        best = None   # (created, people, url)
        wp_errors = []
        for wp_url in cfg["whitepages_urls"]:
            try:
                url, hdrs = await asyncio.to_thread(mp.resolve_url, wp_url, cfg["dns_server"])
                r = await client.get(url, headers=hdrs)
                r.raise_for_status()
                people, created = mp.parse_whitepages(r.text)
                if len(people) < 10:
                    raise ValueError(f"only {len(people)} listings")
                if best is None or created > best[0]:
                    best = (created, people, wp_url)
            except Exception as exc:
                wp_errors.append(f"{urlsplit(wp_url).hostname}: {exc}")
        if best:
            self.mp_people_created, self.mp_people, self.mp_people_source = best[0], best[1], urlsplit(best[2]).hostname or ""
            self.mp_people_refreshed_at = time.time()
        elif wp_errors:
            errors.append("MeshPhone white pages: " + "; ".join(wp_errors))
        return errors

    # ── read side ──────────────────────────────────────────────────────────
    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "interval_min": self.interval_s // 60,
            "hoip_refreshed_at": self.hoip_refreshed_at,
            "hoip_entries": len(self.hoip_entries),
            "asl_refreshed_at": self.asl_refreshed_at,
            "meshphone_offices": len(self.mp_offices),
            "meshphone_offices_refreshed_at": self.mp_offices_refreshed_at,
            "meshphone_people": len(self.mp_people),
            "meshphone_people_created": self.mp_people_created,
            "meshphone_people_source": self.mp_people_source,
            "meshphone_people_refreshed_at": self.mp_people_refreshed_at,
            "last_error": self.last_error,
        }

    def meshphone(self) -> dict:
        """Offices + white pages for the Calls tab / phonebook. Each person and
        office admin gets `dial` (78 + number) for click-to-call."""
        from ech.core.meshphone_directory import dial_string
        people = [{**p, "dial": dial_string(p["number"])} for p in self.mp_people]
        offices = [{**o, "dial": dial_string(o["admin_phone"]) if o.get("admin_phone") else ""}
                   for o in self.mp_offices]
        return {"offices": offices, "people": people, "status": self.status()}

    def annotate(self, entry: dict) -> dict:
        e = dict(entry)
        node, certain = _node_and_certainty(e)
        if node:
            e["allstar_node"] = node
        if node and self.asl_refreshed_at and node in self.asl_registered:
            e["asl_status"] = "up"
        elif node and self.asl_refreshed_at and certain:
            e["asl_status"] = "down"
        else:
            e["asl_status"] = "unknown"
        return e


status = DirectoryStatus()
