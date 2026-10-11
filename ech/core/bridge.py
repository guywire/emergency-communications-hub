"""
ech/core/bridge.py
------------------
Adapter-to-adapter bridging (O117).

A bridge links ONE endpoint on each side — e.g. Meshtastic channel 2 ↔ APRS
messages addressed to EMCOMM — rather than piping an entire adapter's feed
into another. Each rule says which kinds of traffic cross, in which
direction, how fast, and how the text is shaped for the far side.

Rule (config.yaml `bridge_rules:` / POST /api/bridge-rules):

    - name: mesh-aprs-emcomm
      enabled: true
      a: {adapter: meshtastic-usb, channel: 2}          # mesh: channel index
      b: {adapter: aprs-internet, to: EMCOMM}           # APRS: addressee
      direction: both            # a_to_b | b_to_a | both
      types: [text]              # text (channel/group text); dm = direct msgs too
      rate_per_min: 6            # per direction; burst = same
      prefix: true               # "<sender>: " attribution on the far side
      max_len: null              # default per target (APRS 67, MeshCore 140, …)
      require_callsign: false    # only forward senders whose name has a callsign
      dry_run: false             # log "would forward" without sending

Endpoint keys:
    adapter   adapter name (required)
    channel   mesh channel index to listen on and send to (MeshCore/Meshtastic)
    to        outbound destination (APRS addressee/callsign, mesh node for DMs)
    listen    inbound filter for addressed networks (APRS addressee); defaults
              to `to`. An APRS endpoint must have one of them to RECEIVE —
              otherwise every APRS message in the filter radius would cross.

Legacy rules `{from_adapter, to_adapter}` load as a one-way, text-only,
rate-limited bridge (they used to forward everything, positions and all).

Loop protection: a message created by a bridge is never bridged again; text
we sent into an adapter is remembered for a while so the network echoing it
back (repeaters, digipeaters, APRS-IS) is dropped; identical sender+text
within the window is forwarded once even if heard over several paths.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from collections import deque
from dataclasses import dataclass, field

from ech.core.models import NormalizedMessage

log = logging.getLogger(__name__)

DIRECTIONS = ("a_to_b", "b_to_a", "both")
SUPPORTED_TYPES = ("text", "dm")
DEDUPE_WINDOW_S = 120.0

# Text limits for the far side when a rule doesn't set max_len. Matched on
# the adapter's class name so any adapter instance name works.
_DEFAULT_MAX_LEN = (
    ("APRS", 67),          # APRS message text field
    ("MeshCore", 140),
    ("Meshtastic", 200),
    ("M17", 200),
    ("DAPNET", 80),
)
_CALLSIGN_RE = re.compile(r"\b[A-Z]{1,2}\d[A-Z]{1,3}\b|\b\d[A-Z]\d[A-Z]{1,3}\b", re.I)


class BridgeConfigError(ValueError):
    pass


@dataclass
class Endpoint:
    adapter: str
    channel: int | None = None
    to: str | None = None
    listen: str | None = None

    @classmethod
    def parse(cls, d, side: str) -> "Endpoint":
        if not isinstance(d, dict) or not str(d.get("adapter", "")).strip():
            raise BridgeConfigError(f"endpoint {side}: 'adapter' is required")
        ch = d.get("channel")
        if ch not in (None, ""):
            try:
                ch = int(ch)
            except (TypeError, ValueError):
                raise BridgeConfigError(f"endpoint {side}: channel must be a channel index number")
        else:
            ch = None
        to = str(d["to"]).strip().upper() if d.get("to") not in (None, "") else None
        listen = str(d["listen"]).strip().upper() if d.get("listen") not in (None, "") else None
        return cls(adapter=str(d["adapter"]).strip(), channel=ch, to=to, listen=listen or to)

    def to_dict(self) -> dict:
        d = {"adapter": self.adapter}
        if self.channel is not None:
            d["channel"] = self.channel
        if self.to:
            d["to"] = self.to
        if self.listen and self.listen != self.to:
            d["listen"] = self.listen
        return d


@dataclass
class _Bucket:
    rate_per_min: float
    tokens: float = 0.0
    last: float = field(default_factory=time.monotonic)

    def __post_init__(self):
        self.tokens = self.rate_per_min

    def take(self) -> bool:
        now = time.monotonic()
        self.tokens = min(self.rate_per_min,
                          self.tokens + (now - self.last) * self.rate_per_min / 60.0)
        self.last = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False


@dataclass
class BridgeRule:
    name: str
    a: Endpoint
    b: Endpoint
    direction: str = "both"
    types: tuple = ("text",)
    rate_per_min: float = 6.0
    prefix: bool = True
    max_len: int | None = None
    require_callsign: bool = False
    dry_run: bool = False
    enabled: bool = True
    counters: dict = field(default_factory=lambda: {
        "forwarded": 0, "dropped_rate": 0, "dropped_loop": 0,
        "dropped_duplicate": 0, "dropped_no_callsign": 0, "would_forward": 0})

    @classmethod
    def parse(cls, d: dict, idx: int = 0) -> "BridgeRule":
        if not isinstance(d, dict):
            raise BridgeConfigError(f"rule {idx}: must be a mapping")
        if "from_adapter" in d and "a" not in d:      # legacy {from_adapter, to_adapter}
            d = {"name": d.get("name") or f"{d.get('from_adapter')}->{d.get('to_adapter')}",
                 "a": {"adapter": d.get("from_adapter")}, "b": {"adapter": d.get("to_adapter")},
                 "direction": "a_to_b", "types": ["text"]}
        a = Endpoint.parse(d.get("a"), "a")
        b = Endpoint.parse(d.get("b"), "b")
        if a.adapter == b.adapter and a.channel == b.channel and a.listen == b.listen:
            raise BridgeConfigError(f"rule {idx}: both ends are the same endpoint")
        direction = str(d.get("direction", "both"))
        if direction not in DIRECTIONS:
            raise BridgeConfigError(f"rule {idx}: direction must be one of {DIRECTIONS}")
        types = tuple(d.get("types") or ["text"])
        bad = [t for t in types if t not in SUPPORTED_TYPES]
        if bad:
            raise BridgeConfigError(
                f"rule {idx}: unsupported type(s) {bad} — supported: {list(SUPPORTED_TYPES)} "
                "(position/telemetry bridging is not implemented yet)")
        try:
            rate = float(d.get("rate_per_min", 6))
        except (TypeError, ValueError):
            raise BridgeConfigError(f"rule {idx}: rate_per_min must be a number")
        if not (0 < rate <= 120):
            raise BridgeConfigError(f"rule {idx}: rate_per_min must be between 0 and 120")
        max_len = d.get("max_len")
        if max_len not in (None, ""):
            try:
                max_len = int(max_len)
            except (TypeError, ValueError):
                raise BridgeConfigError(f"rule {idx}: max_len must be a number")
            if max_len < 10:
                raise BridgeConfigError(f"rule {idx}: max_len must be at least 10")
        else:
            max_len = None
        return cls(name=str(d.get("name") or f"bridge-{idx + 1}"), a=a, b=b,
                   direction=direction, types=types, rate_per_min=rate,
                   prefix=bool(d.get("prefix", True)), max_len=max_len,
                   require_callsign=bool(d.get("require_callsign", False)),
                   dry_run=bool(d.get("dry_run", False)),
                   enabled=bool(d.get("enabled", True)))

    def to_dict(self) -> dict:
        return {"name": self.name, "enabled": self.enabled,
                "a": self.a.to_dict(), "b": self.b.to_dict(),
                "direction": self.direction, "types": list(self.types),
                "rate_per_min": self.rate_per_min, "prefix": self.prefix,
                "max_len": self.max_len, "require_callsign": self.require_callsign,
                "dry_run": self.dry_run}

    def legs(self):
        """(source, target, label) pairs this rule forwards along."""
        if self.direction in ("a_to_b", "both"):
            yield self.a, self.b, "a_to_b"
        if self.direction in ("b_to_a", "both"):
            yield self.b, self.a, "b_to_a"


def _is_aprs(adapter) -> bool:
    return "aprs" in type(adapter).__name__.lower()


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


class BridgeEngine:
    """Applies BridgeRules to inbound messages. Owned by the Router."""

    def __init__(self):
        self.rules: list[BridgeRule] = []
        self._buckets: dict[tuple, _Bucket] = {}
        self._sent: dict[str, deque] = {}          # target adapter → (t, normalized text)
        self._seen: deque = deque()                # (t, key) for multi-path de-dupe
        self.log: deque = deque(maxlen=200)        # recent decisions for the UI
        self._source = None                        # rules list object last compiled

    # ── configuration ────────────────────────────────────────────────────

    @staticmethod
    def validate(rules: list) -> list[BridgeRule]:
        return [BridgeRule.parse(r, i) for i, r in enumerate(rules or [])]

    def load(self, rules: list) -> None:
        self.rules = self.validate(rules)
        self._buckets.clear()
        self._source = rules

    def ensure_loaded(self, rules: list) -> None:
        """Recompile when the router's rule list object was replaced (API/config)."""
        if rules is not self._source:
            try:
                self.load(rules)
            except BridgeConfigError as exc:
                log.error("Bridge: invalid bridge_rules, bridging disabled: %s", exc)
                self.rules = []
                self._source = rules

    def status(self) -> dict:
        return {"rules": [{**r.to_dict(), "counters": dict(r.counters)} for r in self.rules],
                "log": list(self.log)[-50:]}

    # ── matching ─────────────────────────────────────────────────────────

    @staticmethod
    def _matches(ep: Endpoint, msg: NormalizedMessage, adapter, types) -> str | None:
        """Text to forward if `msg` belongs to endpoint `ep`, else None."""
        if msg.source_adapter != ep.adapter:
            return None
        raw = msg.raw or {}
        if _is_aprs(adapter):
            # APRS: only real message packets, only to the configured addressee
            if raw.get("format") != "message" or not ep.listen:
                return None
            if str(raw.get("addressee", "")).upper() != ep.listen:
                return None
            return raw.get("message_text") or None
        if msg.msg_type != "text":
            return None
        if msg.to_id:                                   # direct message
            if "dm" not in types:
                return None
        elif "text" not in types:
            return None
        if ep.channel is not None:
            ch = raw.get("channel_idx", raw.get("channel"))
            try:
                if ch is None or int(ch) != ep.channel:
                    return None
            except (TypeError, ValueError):
                return None
        return msg.body or None

    def _max_len(self, rule: BridgeRule, adapter) -> int:
        if rule.max_len:
            return rule.max_len
        cls = type(adapter).__name__
        for key, n in _DEFAULT_MAX_LEN:
            if key.lower() in cls.lower():
                return n
        return 200

    @staticmethod
    def _shape(text: str, sender: str, prefix: bool, max_len: int) -> str:
        out = f"{sender}: {text}" if prefix and sender else text
        out = re.sub(r"\s+", " ", out).strip()
        if len(out) > max_len:
            out = out[:max_len - 1].rstrip() + "…"
        return out

    def _echo(self, adapter_name: str, text: str, now: float) -> bool:
        q = self._sent.get(adapter_name)
        if not q:
            return False
        while q and now - q[0][0] > DEDUPE_WINDOW_S:
            q.popleft()
        n = _norm(text)
        # The network may hand our text back with its own decoration
        # ("MSG KN0O: …", a node-name prefix) — match by containment, but
        # only for texts long enough that containment isn't a coincidence
        # (a genuine "ok" must not be eaten because we bridged "X: ok thanks").
        for _, s in q:
            if not s:
                continue
            if s == n:
                return True
            short, long_ = (s, n) if len(s) <= len(n) else (n, s)
            if len(short) >= 8 and short in long_:
                return True
        return False

    def _remember_sent(self, adapter_name: str, text: str, now: float) -> None:
        self._sent.setdefault(adapter_name, deque(maxlen=200)).append((now, _norm(text)))

    def _dup(self, key: str, now: float) -> bool:
        while self._seen and now - self._seen[0][0] > DEDUPE_WINDOW_S:
            self._seen.popleft()
        if any(k == key for _, k in self._seen):
            return True
        self._seen.append((now, key))
        return False

    def _record(self, rule, leg, outcome, msg, text="") -> None:
        self.log.append({"t": time.time(), "rule": rule.name, "leg": leg, "outcome": outcome,
                         "from": msg.from_display or msg.from_id, "text": (text or msg.body)[:120]})

    # ── main entry ───────────────────────────────────────────────────────

    async def apply(self, msg: NormalizedMessage, adapters: dict) -> None:
        if (msg.raw or {}).get("_bridged_by"):
            return                                       # never re-bridge our own output
        now = time.monotonic()
        src_adapter = adapters.get(msg.source_adapter)
        if src_adapter is None:
            return
        for rule in self.rules:
            if not rule.enabled:
                continue
            for src, dst, leg in rule.legs():
                text = self._matches(src, msg, src_adapter, rule.types)
                if text is None:
                    continue
                if self._echo(msg.source_adapter, text, now):
                    rule.counters["dropped_loop"] += 1
                    self._record(rule, leg, "dropped: echo of bridged text", msg, text)
                    continue
                sender = msg.from_display or msg.from_id
                if rule.require_callsign and not _CALLSIGN_RE.search(f"{sender} {msg.from_id}"):
                    rule.counters["dropped_no_callsign"] += 1
                    self._record(rule, leg, "dropped: sender has no callsign", msg, text)
                    continue
                key = hashlib.sha1(f"{rule.name}|{leg}|{msg.from_id}|{_norm(text)}".encode()).hexdigest()
                if self._dup(key, now):
                    rule.counters["dropped_duplicate"] += 1
                    continue
                target = adapters.get(dst.adapter)
                if target is None:
                    self._record(rule, leg, f"dropped: adapter {dst.adapter!r} not found", msg, text)
                    continue
                if _is_aprs(target) and not dst.to:
                    self._record(rule, leg, "dropped: APRS side needs a 'to' addressee", msg, text)
                    continue
                bucket = self._buckets.setdefault((rule.name, leg), _Bucket(rule.rate_per_min))
                if not bucket.take():
                    rule.counters["dropped_rate"] += 1
                    self._record(rule, leg, "dropped: rate limit", msg, text)
                    continue
                out = self._shape(text, sender, rule.prefix, self._max_len(rule, target))
                if rule.dry_run:
                    rule.counters["would_forward"] += 1
                    self._record(rule, leg, f"dry run → {dst.adapter}", msg, out)
                    continue
                if not getattr(target, "_connected", False) or getattr(target, "_paused", False):
                    self._record(rule, leg, f"dropped: {dst.adapter} not connected", msg, out)
                    continue
                raw = {"_bridged_by": rule.name, "_bridged_from": msg.source_adapter}
                if dst.channel is not None:
                    raw["channel_idx"] = dst.channel
                fwd = NormalizedMessage(
                    source_adapter=msg.source_adapter, source_channel=msg.source_channel,
                    from_id=msg.from_id, from_display=msg.from_display, body=out,
                    to_id=dst.to, priority=msg.priority, raw=raw)
                self._remember_sent(dst.adapter, out, now)
                try:
                    ok = await target.send(fwd)
                except Exception as exc:
                    self._record(rule, leg, f"send error: {exc}", msg, out)
                    log.warning("Bridge %s: send to %s failed: %s", rule.name, dst.adapter, exc)
                    continue
                rule.counters["forwarded"] += 1
                self._record(rule, leg, f"forwarded → {dst.adapter}" + ("" if ok is not False else " (send reported failure)"), msg, out)
                log.info("Bridge %s [%s]: %s → %s: %s", rule.name, leg, msg.source_adapter, dst.adapter, out[:80])
