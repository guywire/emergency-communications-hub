"""M17 reflector directory for the server pickers (Listen page, Settings, phone panel).

Two sources, merged and de-duplicated by host:
  * saved   - config.yaml `m17_reflectors: [{name, host, port}]`; every server
              an operator switches to is added here, and the adapters' current
              servers always count as saved.
  * directory - a hotspot-style host file (lines "M17-XXX  host  port"),
              downloaded at most once a day and cached on disk, so the picker
              keeps working from the last good copy when the source is down.
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_SOURCES = ["https://www.pistar.uk/downloads/M17Hosts.txt"]
REFRESH_S = 24 * 3600
_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$")


def valid_host(host: str) -> bool:
    return bool(_HOST_RE.match(host or "")) and ".." not in host


def parse_hosts(text: str) -> list[dict]:
    """'M17-XXX <host> [port]' per line; '#' comments; tabs or spaces."""
    out = []
    for line in (text or "").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2 or not valid_host(parts[1]):
            continue
        try:
            port = int(parts[2]) if len(parts) > 2 else 17000
        except ValueError:
            continue
        if 0 < port < 65536:
            out.append({"name": parts[0][:24], "host": parts[1].lower(), "port": port})
    return out


class M17Directory:
    def __init__(self, cache_path: str | Path, sources: list[str] | None = None):
        self.cache_path = Path(cache_path)
        self.sources = sources or DEFAULT_SOURCES
        self.entries: list[dict] = []
        self.fetched_at = 0.0
        self.error = ""
        self._load_cache()

    def _load_cache(self) -> None:
        try:
            d = json.loads(self.cache_path.read_text())
            self.entries, self.fetched_at = d.get("entries") or [], float(d.get("fetched_at") or 0)
        except (OSError, ValueError):
            pass

    async def refresh(self, force: bool = False) -> None:
        if not force and time.time() - self.fetched_at < REFRESH_S:
            return
        import httpx
        errors = []
        for url in self.sources:
            try:
                async with httpx.AsyncClient(timeout=20, follow_redirects=True,
                                             headers={"User-Agent": "Mozilla/5.0 (ECH M17 directory)"}) as c:
                    r = await c.get(url)
                rows = parse_hosts(r.text) if r.status_code == 200 else []
                if len(rows) >= 5:
                    self.entries, self.fetched_at, self.error = rows, time.time(), ""
                    try:
                        self.cache_path.write_text(json.dumps({"fetched_at": self.fetched_at, "source": url,
                                                               "entries": rows}))
                    except OSError as e:
                        log.warning("M17 directory: cache not written: %s", e)
                    log.info("M17 directory: %d reflectors from %s", len(rows), url)
                    return
                errors.append(f"{url}: HTTP {r.status_code}, {len(rows)} entries")
            except Exception as e:
                errors.append(f"{url}: {e.__class__.__name__}")
        self.error = "; ".join(errors)
        self.fetched_at = max(self.fetched_at, time.time() - REFRESH_S + 3600)   # retry in an hour
        log.info("M17 directory refresh failed (%s); using %d cached entries", self.error, len(self.entries))

    def merged(self, saved: list[dict]) -> list[dict]:
        """Saved first (in their order), then the directory sorted by name."""
        seen, out = set(), []
        for src, rows in (("saved", saved), ("directory", sorted(self.entries, key=lambda x: x["name"]))):
            for r in rows:
                host = str(r.get("host") or "").lower()
                if not valid_host(host) or host in seen:
                    continue
                seen.add(host)
                out.append({"name": str(r.get("name") or host)[:40], "host": host,
                            "port": int(r.get("port") or 17000), "source": src})
        return out


def saved_from_config(cfg: dict) -> list[dict]:
    """config m17_reflectors + every M17 adapter's current server."""
    rows = list((cfg or {}).get("m17_reflectors") or [])
    for a in (cfg or {}).get("adapters") or []:
        if (a.get("type") or "").startswith("m17") and a.get("reflector_host"):
            rows.append({"name": a["reflector_host"], "host": a["reflector_host"],
                         "port": a.get("reflector_port", 17000)})
    return rows
