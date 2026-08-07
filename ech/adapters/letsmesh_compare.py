"""
ech/adapters/letsmesh_compare.py
----------------------------------
Passive comparison layer against LetsMesh.net's MeshCore packet-observer
network. NOT a message source — nothing from here reaches the main feed.

LetsMesh (analyzer.letsmesh.net) aggregates packets reported by many
"Observer" MeshCore nodes over MQTT (topic meshcore/{region}/{pubkey}/packets).
The analyzer's own web dashboard is a bot-protected single-page app with no
public JSON API — direct HTTP fetches to it (even API-shaped guesses) get
Cloudflare-blocked — but the underlying MQTT broker is the same one ECH
already knows how to authenticate to (see mqtt_adapter.py's pubkey_auth:
an Ed25519 JWT derived from a local MeshCore adapter's hardware key).

Subscribing to that feed lets ECH see path/routing data reported by OTHER
observers near it — repeaters and hop chains this station's own single
vantage point may never hear directly — and merge that into the local
MeshCore adapter's topology graph (the same graph traces/map routing use).

What this adapter does:
  1. Subscribes to a LetsMesh packets topic (e.g. meshcore/RKD/+/packets).
  2. For every packet with a "path" field (direct-route packets carry a
     relay-hash chain, e.g. "C2 -> E2"), feeds it into the target MeshCore
     adapter's _learn_topology() — the exact same call heard-traffic and
     trace results already use, so it plugs into existing route inference
     without any special-casing on the map/trace side.
  3. Tracks distinct transmitting-node hash prefixes (origin_id) seen on
     LetsMesh in a rolling window and reports how many the local topology
     graph doesn't know about yet — "N nodes active in this region that
     this station hasn't directly or indirectly heard."

That coverage number is deliberately a coarse per-origin comparison, NOT
exact packet-for-packet hash matching. MeshCore firmware hashes packet
content with SHA-256 over the payload type + payload for its own dedup,
but the precise byte layout isn't confirmed against live traffic here —
an exact-hash comparison risks silently reporting a wrong number, which
in an emergency-comms tool is worse than not reporting one. The per-origin
comparison doesn't depend on getting that byte-exact.

Config keys (in addition to everything mqtt_adapter.MQTTAdapter accepts —
host/port/tls/transport/pubkey_auth/private_key/token_ttl/topics):
  name              str     adapter name (default: letsmesh-compare)
  meshcore_adapter  str     local MeshCore adapter name to feed topology
                            into and compare coverage against (REQUIRED;
                            should match the pubkey_auth value)
  window_sec        int     rolling window for the coverage summary
                            (default: 3600 = 1 hour)
  log_interval_sec  int     how often to log a coverage summary
                            (default: 900 = 15 min)

Example config:
  - type: letsmesh_compare
    name: letsmesh-compare
    host: mqtt-us-v1.letsmesh.net
    port: 443
    tls: true
    transport: websockets
    pubkey_auth: meshcore-usb     # local MeshCore adapter whose hardware key signs the JWT
    meshcore_adapter: meshcore-usb
    topics: ["meshcore/RKD/+/packets"]   # scope to your region — see the LetsMesh map
"""

from __future__ import annotations

import json
import logging
import time

from ech.adapters.mqtt_adapter import MQTTAdapter

log = logging.getLogger(__name__)


class LetsMeshCompareAdapter(MQTTAdapter):
    """Subscribes to a LetsMesh MQTT packet-observer feed and merges observed
    path data into a local MeshCore adapter's topology graph, without adding
    anything to the main message feed."""

    def __init__(self, config: dict):
        super().__init__(config)
        self.name = config.get("name", "letsmesh-compare")
        self._target_name = config.get("meshcore_adapter", config.get("pubkey_auth", ""))
        self._window_sec = int(config.get("window_sec", 3600))
        self._log_interval_sec = int(config.get("log_interval_sec", 900))

        self._packets_seen = 0
        self._paths_merged = 0
        self._parse_errors = 0
        # origin hash-prefix -> last-seen monotonic time; a rolling window of
        # distinct transmitting nodes LetsMesh has reported hearing.
        self._seen_origins: dict[str, float] = {}
        self._last_summary = 0.0

    def _target_adapter(self):
        if not self._target_name or not self._get_sibling_adapter:
            return None
        return self._get_sibling_adapter(self._target_name)

    async def send(self, message) -> bool:
        """Never send — this adapter only subscribes to LetsMesh's observer feed
        for topology comparison. Publishing here would inject a bogus packet onto
        a shared third-party network. The UI hides this adapter from compose
        targets (see adapterMeta() in index.html); this is the server-side backstop."""
        log.warning("LetsMesh %s: send() called but this adapter never transmits — dropping", self.name)
        return False

    async def _handle_message(self, topic: str, payload: bytes) -> None:
        self._packets_seen += 1
        try:
            data = json.loads(payload.decode("utf-8", errors="replace"))
        except Exception:
            self._parse_errors += 1
            return
        if data.get("type") not in (None, "PACKET"):
            return

        origin_id = str(data.get("origin_id") or "").strip()
        if origin_id:
            self._seen_origins[origin_id[:2].lower()] = time.monotonic()

        path = data.get("path")
        if path:
            hashes = [h.strip().lower() for h in str(path).split("->") if h.strip()]
            if len(hashes) >= 2:
                target = self._target_adapter()
                if target is not None and hasattr(target, "_learn_topology"):
                    target._learn_topology(hashes)
                    self._paths_merged += 1

        now = time.monotonic()
        if now - self._last_summary >= self._log_interval_sec:
            self._last_summary = now
            self._log_coverage_summary()

    def _log_coverage_summary(self) -> None:
        cutoff = time.monotonic() - self._window_sec
        # Prune while filtering so this dict doesn't grow forever.
        self._seen_origins = {h: t for h, t in self._seen_origins.items() if t >= cutoff}
        recent_origins = set(self._seen_origins)

        target = self._target_adapter()
        unknown = recent_origins
        if target is not None:
            known = getattr(target, "_direct_hashes", set()) | set(getattr(target, "_rf_adjacency", {}))
            unknown = recent_origins - known
        log.info(
            "LetsMesh %s: %d packet(s) processed (%d path(s) merged into %s's topology), "
            "%d distinct node(s) active on LetsMesh in the last %ds, %d not yet in local topology",
            self.name, self._packets_seen, self._paths_merged,
            self._target_name or "?", len(recent_origins), self._window_sec, len(unknown),
        )

    def _health_detail(self) -> dict:
        detail = super()._health_detail()
        detail.update({
            "target_adapter": self._target_name,
            "packets_seen": self._packets_seen,
            "paths_merged": self._paths_merged,
            "parse_errors": self._parse_errors,
            "distinct_origins_seen": len(self._seen_origins),
            "mode": "letsmesh-compare (no messages added to feed)",
        })
        return detail
