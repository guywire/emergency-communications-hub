"""
ech/core/meshcore_hub_compare.py
----------------------------------
O96: compare group/channel text messages seen by a `meshcore-hub`
(github.com/ipnet-mesh/meshcore-hub) instance's other observers against
what ECH itself received directly, to surface reception gaps.

Scope/honesty note: this was built from meshcore-hub's project description
only (self-hosted, REST API, `/api/v1/messages` and `/api/v1/dashboard/stats`
endpoints, Swagger/OpenAPI docs) — no live instance was available to verify
the exact response JSON schema against. `_extract_message()` below is
deliberately defensive: it tries several plausible field-name variants per
value and returns None (skipping the record, logged once per poll) rather
than guessing wrong and silently mis-reporting gaps. Before trusting this
operationally, verify field names against a real instance's
`/api/v1/messages` response (or its OpenAPI/Swagger schema at `/docs` or
`/openapi.json`) and adjust `_FIELD_CANDIDATES` if they differ.

Two ways to actually get data into a meshcore-hub instance for this to
compare against:
  1. Point ECH's existing MQTT bridge (meshcore_bridge.py, O4/O71) at the
     hub's MQTT broker as one more observer — no new code needed, just a
     `letsmesh-compare`-style `mqtt` adapter config block pointed at the
     hub's broker address instead of LetsMesh's.
  2. This module's read-only REST polling, for comparing against gaps.

Config (config.yaml):
  meshcore_hub:
    enabled: true
    base_url: "http://meshcore-hub.local:8000"   # no trailing slash
    bearer_token: ""                              # optional, if the hub requires auth
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import httpx

log = logging.getLogger(__name__)

# Each inner list is tried in order; first key present in the record wins.
# Deliberately broad — the real schema is unconfirmed (see module docstring).
_FIELD_CANDIDATES = {
    "timestamp": ["timestamp", "time", "received_at", "created_at", "ts"],
    "body":      ["text", "body", "payload", "message", "content"],
    "channel":   ["channel", "channel_idx", "channel_index", "channel_name"],
    "observer":  ["observer", "node", "gateway", "station", "source"],
}


def _first_present(record: dict, keys: list[str]):
    for k in keys:
        if k in record and record[k] not in (None, ""):
            return record[k]
    return None


def _extract_message(record: dict) -> dict | None:
    """Pull a normalized {timestamp, body, channel, observer} out of one
    record from the hub's /api/v1/messages response, trying several
    plausible field names. Returns None (record skipped) if no recognizable
    timestamp or body field is found at all."""
    ts_raw = _first_present(record, _FIELD_CANDIDATES["timestamp"])
    body = _first_present(record, _FIELD_CANDIDATES["body"])
    if ts_raw is None or body is None:
        return None
    try:
        if isinstance(ts_raw, (int, float)):
            ts = datetime.fromtimestamp(float(ts_raw), tz=timezone.utc)
        else:
            s = str(ts_raw).replace("Z", "+00:00")
            ts = datetime.fromisoformat(s)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None
    return {
        "timestamp": ts,
        "body": str(body),
        "channel": _first_present(record, _FIELD_CANDIDATES["channel"]),
        "observer": _first_present(record, _FIELD_CANDIDATES["observer"]),
    }


class MeshCoreHubCompare:
    def __init__(self, config: dict, db=None):
        cfg = config.get("meshcore_hub", {}) or {}
        self.enabled = bool(cfg.get("enabled", False))
        self._base_url = str(cfg.get("base_url", "")).rstrip("/")
        self._bearer_token = str(cfg.get("bearer_token", "") or "").strip()
        self._db = db
        self._client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        if not self.enabled:
            return
        if not self._base_url:
            log.warning("MeshCoreHubCompare: enabled but meshcore_hub.base_url not set")
            return
        headers = {"Authorization": f"Bearer {self._bearer_token}"} if self._bearer_token else {}
        self._client = httpx.AsyncClient(base_url=self._base_url, headers=headers, timeout=15.0)
        log.info("MeshCoreHubCompare: started, base_url=%s", self._base_url)

    async def stop(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def check_connectivity(self) -> dict:
        """GET /api/v1/dashboard/stats — the lightest-weight confirmed-to-
        exist endpoint, used purely as a reachability/auth probe."""
        if not self._client:
            return {"status": "error", "detail": "not started (enabled=false or no base_url)"}
        try:
            resp = await self._client.get("/api/v1/dashboard/stats")
            resp.raise_for_status()
            return {"status": "ok", "stats": resp.json()}
        except httpx.HTTPError as exc:
            return {"status": "error", "detail": str(exc)}

    async def fetch_hub_messages(self, hours: int = 24) -> list[dict]:
        """GET /api/v1/messages for the last `hours` hours. Returns
        normalized records (see _extract_message); malformed/unrecognized
        records are silently skipped (counted and logged once)."""
        if not self._client:
            return []
        try:
            resp = await self._client.get("/api/v1/messages", params={"hours": hours})
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as exc:
            log.warning("MeshCoreHubCompare: fetch_hub_messages failed: %s", exc)
            return []
        records = data if isinstance(data, list) else (data.get("messages") or data.get("items") or [])
        out, skipped = [], 0
        for r in records:
            if not isinstance(r, dict):
                skipped += 1
                continue
            parsed = _extract_message(r)
            if parsed is None:
                skipped += 1
                continue
            out.append(parsed)
        if skipped:
            log.warning("MeshCoreHubCompare: skipped %d/%d records with no recognizable "
                        "timestamp/body field — hub's response schema may not match "
                        "_FIELD_CANDIDATES; see module docstring", skipped, len(records))
        return out

    async def find_gaps(self, hours: int = 24) -> list[dict]:
        """Hub-observed messages with no close match (same body substring
        within a 5-minute window) in ECH's own message log — candidates for
        "other observers heard this, we didn't"."""
        if not self._db:
            return []
        hub_msgs = await self.fetch_hub_messages(hours=hours)
        if not hub_msgs:
            return []
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        own = await self._db.get_messages(limit=5000, since=since)
        own_bodies = [(m.get("body") or "", m.get("timestamp") or "") for m in own]

        gaps = []
        for hm in hub_msgs:
            body = hm["body"].strip()
            if not body:
                continue
            found = False
            for ob_body, ob_ts in own_bodies:
                if body and body in ob_body:
                    try:
                        ob_dt = datetime.fromisoformat(str(ob_ts).replace("Z", "+00:00"))
                        if ob_dt.tzinfo is None:
                            ob_dt = ob_dt.replace(tzinfo=timezone.utc)
                        if abs((ob_dt - hm["timestamp"]).total_seconds()) <= 300:
                            found = True
                            break
                    except (ValueError, TypeError):
                        continue
            if not found:
                gaps.append(hm)
        return gaps
