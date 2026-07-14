"""
ech/core/mesh_bot.py
---------------------
General-purpose mesh channel bot.  Replaces weather_bot.py.

Commands (case-insensitive, detected anywhere in the message body; a trailing
word or two after the command is tolerated and ignored by commands that take
no argument, e.g. "overhead now" still runs overhead):
  ping              — round-trip echo with SNR / hop metadata
  weather [place]   — NWS conditions + forecast; place = zip, city name, or blank for base location
  wx [place]        — alias for weather
  overhead          — aircraft within radius via local dump1090 JSON
  planes / aircraft — aliases for overhead
  satpass [name]    — next satellite pass (ISS, NOAA 19, …)
  sat [name]        — alias for satpass
  solar / space     — NOAA space weather: SFI, SSN, K-index
  ships             — nearest AIS vessels from connected AIS-catcher adapter
  fcc <callsign>    — FCC ULS amateur license lookup
  trivia [category] — scored multiple-choice question; reply A/B/C/D. Auto-continues with a
                      new question after each answer (no need to retype "trivia" every round)
                      until "trivia stop". Category (e.g. "trivia science") sticks for the
                      rest of the round; "trivia any" clears it back to random. On a channel
                      the question is open to whoever answers first (channel senders have no
                      reliable identity to restrict it to); in a DM it's just for you.
  score             — your trivia correct/answered tally (DM only)
  leaderboard / lb  — top 5 trivia scores mesh-wide (DM only)
  mud / adventure   — game picker: TinyMUD-lite, Colossal Cave Adventure, or Derelict (DM only)
  skywarn <call> <report>  — log a spotter report (works on channels too); "skywarn" alone
                             starts a guided DM form; "skywarn last" re-shows what's on file;
                             "skywarn net" formats it as ham radio net check-in phrasing;
                             "skywarn inws"/"nws" formats it for manual entry into iNWS or
                             equivalent; "skywarn winlink" emails it via the Pat Winlink
                             adapter to mesh_bot.skywarn_winlink_to (must be configured)
  dad               — dad joke (icanhazdadjoke.com)
  help              — list available commands

New aliases are added by adding the trigger word to _CMD_WORDS (for matching)
and, if it isn't already the canonical name, to _CMD_ALIASES (for dispatch).

Unrecognized input:
  - DMs always get a short "unknown command" reply (a DM implies the sender
    is deliberately addressing the bot, so silence would look like a dead bot).
  - Channel messages are otherwise ignored unless they @-mention the bot's
    configured name (mesh_bot.mention_name); channel chatter that doesn't
    address the bot must not trigger a reply.

Interactive sessions (trivia answers, MUD moves, SKYWARN's guided form):
  - Polled MeshCore channel messages carry no reliable sender identity (see the
    meshcore.py channel-message parsing notes), so anything keyed per-sender
    (MUD sessions, SKYWARN's form, a DM trivia question) is gated to DMs. Trivia
    on a channel works around this by scoping the pending question to the
    CHANNEL instead of a sender — open to whoever answers first — rather than
    needing a sender identity at all; see _trivia_channel_pending.
  - A pending trivia question (DM or channel) only intercepts a bare
    "A"/"B"/"C"/"D" reply (see _TRIVIA_ANSWER_RE) — everything else still runs
    as a normal command, so an ignored question doesn't lock anyone out.
  - An active MUD session (mesh_bot._mud_sessions) intercepts ALL further
    text from that context as a game command until "quit"/"exit" — this is the
    conventional MUD UX (you're "in" the game), but it does mean other bot
    commands are unavailable mid-session. DM sessions are per-sender; channel
    sessions (keyed by (adapter, channel), like channel trivia) are ONE shared
    game the whole channel drives together, since polled channel messages
    carry no sender identity. Channel games can only be started on channels
    NOT in mention_required_channels, so a shared session can never swallow
    conversation on a general-purpose channel.
  - All of these bypass the global/per-user cooldown: they're already a
    direct reply to something the bot itself just asked, not a new broadcast
    that could flood external APIs, so the cooldown (which
    exists to stop channel-wide flooding of external APIs) doesn't apply.

Config block (config.yaml):
  mesh_bot:
    enabled: true
    channels: ["#weather", "#cmd"]   # ["*"] = every channel
    adapters: []                      # [] = every adapter
    reply_dm: true                    # true = DM sender; false = channel broadcast
    per_user_cooldown_sec: 30         # per-sender cooldown (all commands share it)
    global_cooldown_sec: 5            # minimum gap between ANY two bot replies
    max_reply_len: 200                # hard cap (one LoRa payload)
    lat: null                         # observer lat; falls back to weather_service.nws_lat / state base
    lon: null                         # observer lon; falls back to weather_service.nws_lon / state base
    dump1090_path: "/run/dump1090-fa/aircraft.json"
    overhead_radius_nm: 20            # nautical miles
    ships_radius_nm: 50               # nautical miles for nearby ship list
    tle_targets: ["ISS (ZARYA)", "NOAA 19", "NOAA 18"]
    solar_cache_sec: 900              # re-fetch solar data after this many seconds
    mention_name: "SM"                # @name that triggers an error reply on unrecognized channel messages
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import random
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any

import httpx

from ech.core.models import NormalizedMessage, Priority

log = logging.getLogger(__name__)

NWS_BASE       = "https://api.weather.gov"
NOMINATIM_URL  = "https://nominatim.openstreetmap.org/search"
HAMQSL_URL     = "https://www.hamqsl.com/solarxml.php"
METAR_URL      = "https://aviationweather.gov/api/data/metar"
CALLOOK_URL    = "https://callook.info/{callsign}/json"
TRIVIA_URL          = "https://opentdb.com/api.php"
TRIVIA_CATEGORY_URL = "https://opentdb.com/api_category.php"
DADJOKE_URL    = "https://icanhazdadjoke.com/"
# TLE sources fetched concurrently.  Small curated groups first (fast, few KB);
# full catalogs as fallback (slow — SatNOGS JSON is several MB).
# celestrak.org blocks some IP ranges, hence the multi-source approach.
TLE_SOURCES = [
    # CelesTrak: curated small groups — stations (ISS) and weather (NOAA) only
    ("text", "https://celestrak.org/SATCAT/groups/stations.txt"),
    ("text", "https://celestrak.org/SATCAT/groups/weather.txt"),
    # AMSAT: amateur satellites, plain 3-line TLE text
    ("text", "https://www.amsat.org/tle/current/nasabare.txt"),
    # SatNOGS: full catalog fallback — large JSON, only useful if above fail
    ("json", "https://db.satnogs.org/api/tle/?page_size=5000"),
]
_TLE_SOURCE_TIMEOUT = 12.0   # per-source HTTP timeout (seconds)
USER_AGENT = "(SignalMatrix, sm@emergency.local)"

_WIND_DIRS = ["N","NNE","NE","ENE","E","ESE","SE","SSE",
              "S","SSW","SW","WSW","W","WNW","NW","NNW"]
_CARD8 = ["N","NE","E","SE","S","SW","W","NW"]

# Every recognized trigger word. Longer alternatives that share a prefix with a
# shorter one (e.g. satpass/sat) can appear in any order — the trailing lookahead
# forces the regex to backtrack onto the full word, so "satpass" always matches
# as "satpass", never as "sat" + leftover "pass".
_CMD_WORDS = [
    "ping", "weather\\??", "wx", "overhead", "planes", "aircraft",
    "satpass", "sat", "solar", "space", "ships", "fcc", "trivia", "dad",
    "alerts", "metar", "sun", "nodes", "aprs", "anomalies", "tide", "tides",
    "grid", "id", "moon", "dxcc", "contest", "help", "path",
    "score", "leaderboard", "lb", "mud", "adventure", "skywarn",
]
_CMD_RE = re.compile(
    r'(?<![a-z0-9])'
    r'(' + '|'.join(_CMD_WORDS) + r')'
    r'(?:\s+([^\s].{0,40}))?'
    r'(?=\s|$|[^a-z0-9])',
    re.IGNORECASE,
)
# Alias word → canonical command name used in the _dispatch() elif chain.
# Only needed for words that differ from their canonical name; "weather?" is
# normalised to "weather" separately via .rstrip("?").
_CMD_ALIASES: dict[str, str] = {
    "wx": "weather",
    "planes": "overhead", "aircraft": "overhead",
    "sat": "satpass",
    "space": "solar",
    "tides": "tide",
    "lb": "leaderboard",
    "adventure": "mud",
}
_ICAO_RE = re.compile(r'\b([A-Z]{4})\b', re.IGNORECASE)
APRS_FI_URL = "https://api.aprs.fi/api/get"
_ZIP_RE      = re.compile(r'\b(\d{5})\b')
_CALL_RE     = re.compile(r'\b([AKNW][A-Z0-9]{1,2}\d[A-Z]{1,3})\b', re.IGNORECASE)

_HEX_NODE_RE = re.compile(r'^[0-9a-fA-F]{8,}')

# A bare "A"/"B"/"C"/"D" (optionally with trailing punctuation) is treated as a
# trivia answer only while that sender has a pending question — see handle().
_TRIVIA_ANSWER_RE = re.compile(r'^\s*([a-dA-D])[.!)]?\s*$')
_TRIVIA_TIMEOUT_SEC = 120.0
# MUD sessions (which can hold a live Adventure `Game()` engine instance) have
# no natural end if the sender just stops replying — unlike trivia's pending
# answer, nothing else ever looks at an abandoned session again, so a lazy
# check-on-next-message isn't enough; a periodic sweep (_sweep_mud_sessions)
# is needed to actually reclaim them.
_MUD_SESSION_TIMEOUT_SEC = 1800.0
_MUD_SWEEP_INTERVAL_SEC = 300.0
_TRIVIA_SCORES_KV_KEY = "mesh_bot_trivia_scores"

# ── SKYWARN spotter reports ────────────────────────────────────────────────────
# Guided form fields, asked one at a time (see _dispatch_skywarn). DM-only,
# same reason as trivia/MUD: multi-turn state keyed by from_id needs a reliable
# sender identity, which polled MeshCore channel messages don't carry.
# (key, prompt)
# Field set cross-checked against the standard Winlink SKYWARN/"Local WX
# Report" form templates (event/observation type, hail size, and wind/precip
# are all standard fields there) — event_type and hail_size were missing
# before and have been added to match.
_SKYWARN_FIELDS = [
    ("callsign", "Callsign?"),
    ("spotter_id", "Spotter ID? (or 'none')"),
    ("location", "Location (town/landmark)?"),
    ("event_type", "Event type? (e.g. tornado, hail, wind damage, flooding, snow, heavy rain)"),
    ("temp_f", "Temperature (F)?"),
    ("wind_mph", "Wind speed (mph)?"),
    ("wind_dir", "Wind direction (e.g. SE)?"),
    ("hail_size", "Hail size (inches)? (or 'none')"),
    ("precip", "Precipitation/snow depth? (or 'none')"),
    ("notes", "Other hazards/notes? (or 'none')"),
]

# ── TinyMUD-lite ──────────────────────────────────────────────────────────────
# A tiny single-player homage to TinyMUD's exploration/building focus (rather
# than combat-heavy dungeon crawls): a handful of rooms, a couple of items, one
# simple locked-door puzzle. Session state is per-sender and in-memory only —
# there's no shared/persistent world, deliberately, to keep replies short and
# avoid one player's dropped packet corrupting another's state.
_MUD_START = "cloud"
_MUD_ROOMS: dict[str, dict] = {
    "cloud": {
        "name": "The Cloud",
        "desc": "A soft white cloud beneath an endless sky. Mist stairs lead down.",
        "exits": {"down": "plaza"},
        "items": [],
    },
    "plaza": {
        "name": "Founders' Plaza",
        "desc": "Hand-built stone underfoot. A garden lies north, a workshop east, mist stairs up.",
        "exits": {"north": "garden", "east": "workshop", "up": "cloud"},
        "items": ["brass key"],
    },
    "garden": {
        "name": "The Garden",
        "desc": "Player-planted flowers ring a small pond. Something glints under a lily pad.",
        "exits": {"south": "plaza"},
        "items": ["silver coin"],
    },
    "workshop": {
        "name": "The Workshop",
        "desc": "Dusty tools line the walls. A locked hatch leads down.",
        "exits": {"west": "plaza", "down": "cellar"},
        "items": [],
        "locks": {"down": "brass key"},
    },
    "cellar": {
        "name": "The Cellar",
        "desc": "Cool and dark. A hatch above leads back up; a door out leads home.",
        "exits": {"up": "workshop", "out": "WIN"},
        "items": [],
    },
}
_MUD_DIRS = {"n": "north", "s": "south", "e": "east", "w": "west", "u": "up", "d": "down"}

# ── Colossal Cave Adventure (1977) via the `adventure` PyPI package ───────────
# The original Crowther & Woods game, faithfully ported to Python by Brandon
# Rhodes (MIT licensed) — a real, pre-built ~140-room game rather than one we
# authored, for players who find TinyMUD-lite too small. Imported lazily so a
# missing dependency degrades to an error message instead of crashing the bot.
try:
    from adventure import load_advent_dat as _adventure_load_advent_dat
    from adventure.game import Game as _AdventureGame
    _ADVENTURE_AVAILABLE = True
except ImportError:
    _ADVENTURE_AVAILABLE = False

# (number, internal key, menu label) — shown by the "mud" command's game picker.
# Labels are deliberately terse: the full picker line must fit ONE LoRa payload
# (max_reply_len, 160 live) — the earlier descriptive labels pushed it to ~178
# chars and the "(reply 1-3)" tail was being silently truncated off.
_MUD_GAME_CHOICES = [
    ("1", "tinymud", "TinyMUD-lite (quick)"),
    ("2", "adventure", "Colossal Cave Adventure (1977, huge)"),
    ("3", "derelict", "Derelict (space escape)"),
]

# ── Derelict — original, larger than TinyMUD-lite, space-station escape ──────
# No suitable existing space-themed package was found (verified by search —
# only unpackaged one-off hobby repos turned up, none pre-built/substantial
# enough to import), so this one is hand-authored, same as TinyMUD-lite but
# roughly twice the room count and with one real puzzle (find a keycard to
# reach engineering, then power the reactor with a fuel cell before the
# airlock will let you launch the escape pod).
_DERELICT_START = "cryo_bay"
_DERELICT_ROOMS: dict[str, dict] = {
    "cryo_bay": {
        "name": "Cryo Bay",
        "desc": "Frost-cracked pods line the walls; emergency lights strobe red. A corridor leads south.",
        "exits": {"south": "corridor"},
        "items": [],
    },
    "corridor": {
        "name": "Main Corridor",
        "desc": "The hull groans overhead. Doors lead east to the galley, west to med bay, south to the airlock.",
        "exits": {"north": "cryo_bay", "east": "galley", "west": "medbay", "south": "airlock"},
        "items": [],
    },
    "galley": {
        "name": "Galley",
        "desc": "Ration packs drift in zero-g from a burst locker.",
        "exits": {"west": "corridor"},
        "items": ["ration pack"],
    },
    "medbay": {
        "name": "Med Bay",
        "desc": "Shattered glass and a dead medbot. A keycard is clipped to its harness.",
        "exits": {"east": "corridor"},
        "items": ["keycard"],
    },
    "airlock": {
        "name": "Airlock",
        "desc": "The outer door control panel is dark. A hatch leads down to the cargo hold.",
        "exits": {"north": "corridor", "down": "cargo_hold"},
        "items": [],
    },
    "cargo_hold": {
        "name": "Cargo Hold",
        "desc": "Most crates are crushed. One intact case holds a fuel cell. A hatch east leads to engineering.",
        "exits": {"up": "airlock", "east": "engineering"},
        "items": ["fuel cell"],
    },
    "engineering": {
        "name": "Engineering",
        "desc": "Conduits spark overhead. A locked blast door blocks the reactor room to the north.",
        "exits": {"west": "cargo_hold", "north": "reactor_room"},
        "items": [],
        "locks": {"north": "keycard"},
    },
    "reactor_room": {
        "name": "Reactor Room",
        "desc": "The reactor sits cold and dark, an empty slot in its housing.",
        "exits": {"south": "engineering"},
        "items": [],
    },
}


def _parse_channel_idx(source_channel: str | None) -> int | None:
    """Return channel index from 'ch2:weather' or 'ch2', else None."""
    if not source_channel:
        return None
    m = re.match(r'^ch(\d+)', source_channel.lower())
    return int(m.group(1)) if m else None


def _is_mentioned(body: str, name: str) -> bool:
    """True if body contains "@name" as a whole word (case-insensitive), used to
    let channel traffic address the bot directly without it replying to every
    message. The trailing boundary check keeps "foo@smith.com" from matching
    mention_name="SM"."""
    if not name:
        return False
    return re.search(r'@' + re.escape(name) + r'(?![a-z0-9])', body, re.IGNORECASE) is not None


# ── Geometry helpers ──────────────────────────────────────────────────────────

def _haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in nautical miles."""
    r = 3440.065  # Earth radius in nautical miles
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlam = math.radians(lon2 - lon1)
    x = math.sin(dlam) * math.cos(phi2)
    y = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlam)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def _card8(bearing: float) -> str:
    return _CARD8[int((bearing + 22.5) / 45) % 8]


# ── TLE cache ─────────────────────────────────────────────────────────────────

class _TleCache:
    def __init__(self, ttl_sec: int = 86400):
        self._ttl = ttl_sec
        self._data: dict[str, tuple[str, str]] = {}   # name → (line1, line2)
        self._fetched_at: float = 0.0
        self.last_error: str = ""

    def get(self, name: str) -> tuple[str, str] | None:
        return self._data.get(name.upper())

    def expired(self) -> bool:
        return not self._data or time.monotonic() - self._fetched_at > self._ttl

    def load_text(self, text: str) -> int:
        lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
        loaded = 0
        i = 0
        while i + 2 < len(lines):
            name_ln = lines[i]
            l1 = lines[i + 1]
            l2 = lines[i + 2]
            if l1.startswith("1 ") and l2.startswith("2 "):
                self._data[name_ln.strip().upper()] = (l1, l2)
                loaded += 1
                i += 3
            else:
                i += 1
        self._fetched_at = time.monotonic()
        return loaded

    def load_json(self, data: list) -> int:
        """Load SatNOGS-style JSON: [{"tle0": "0 NAME", "tle1": "1 ...", "tle2": "2 ..."}, ...]"""
        loaded = 0
        for item in data:
            name = str(item.get("tle0", "")).lstrip("0 ").strip().upper()
            l1 = str(item.get("tle1", ""))
            l2 = str(item.get("tle2", ""))
            if name and l1.startswith("1 ") and l2.startswith("2 "):
                self._data[name] = (l1, l2)
                loaded += 1
        self._fetched_at = time.monotonic()
        return loaded


# ── Main bot class ────────────────────────────────────────────────────────────

class MeshBot:
    def __init__(self, config: dict, router=None, state=None, db=None):
        self._db = db
        self._config = config
        cfg = config.get("mesh_bot", config.get("weather_bot", {}))
        self.enabled               = bool(cfg.get("enabled", False))
        self._channels             = [c.lower() for c in cfg.get("channels", ["#weather", "#cmd"])]
        # Channels (subset of self._channels) where even a real command word
        # must not auto-fire without an explicit @mention — for general-purpose
        # channels where ordinary conversation can contain a trigger word (e.g.
        # "the weather's nice today" matching "weather"), as opposed to a
        # dedicated bot-test channel where that ambiguity doesn't exist.
        self._mention_required_channels = [c.lower() for c in cfg.get("mention_required_channels", [])]
        self._adapter_filter: list = cfg.get("adapters", [])
        self._reply_dm             = bool(cfg.get("reply_dm", True))
        self._user_cooldown        = int(cfg.get("per_user_cooldown_sec", 30))
        self._global_cooldown      = int(cfg.get("global_cooldown_sec", 5))
        self._max_len              = int(cfg.get("max_reply_len", 200))
        self._dump1090             = cfg.get("dump1090_path", "/run/dump1090-fa/aircraft.json")
        self._radius_nm            = float(cfg.get("overhead_radius_nm", 20))
        self._tle_targets          = [t.upper() for t in cfg.get("tle_targets", ["ISS (ZARYA)", "NOAA 19", "NOAA 18"])]
        self._solar_cache_sec      = int(cfg.get("solar_cache_sec", 900))
        self._tide_station         = str(cfg.get("tide_station", "")).strip()
        self._mention_name         = str(cfg.get("mention_name", "SM")).strip()

        # Observer coordinates — priority: mesh_bot.lat/lon → weather_service.nws_lat/lon → state.base_lat/lon
        wx_cfg = config.get("weather_service", {})
        self._lat: float | None = cfg.get("lat") or wx_cfg.get("nws_lat")
        self._lon: float | None = cfg.get("lon") or wx_cfg.get("nws_lon")
        self._state = state   # held so _resolve_coords() can pull live base position

        self._router = router
        self._user_ts:  dict[str, float] = {}   # from_id → last reply time
        self._global_ts: float = 0.0
        self._client: httpx.AsyncClient | None = None
        self._tle_cache = _TleCache()
        self._solar_cache: str = ""
        self._solar_ts: float = 0.0
        self._start_time: float = 0.0
        # from_id → pending trivia question (multiple-choice, awaiting an A-D reply)
        self._trivia_pending: dict[str, dict] = {}
        # (adapter, channel) → pending channel trivia question, open to whoever
        # answers first (channel senders have no reliable from_id to key on)
        self._trivia_channel_pending: dict[tuple, dict] = {}
        # context key (("dm", from_id) or the channel tuple) → round paused via
        # "trivia stop"; absent = keep auto-continuing after each answer
        self._trivia_paused: set = set()
        # context key → opentdb category id, sticky across an auto-continuing round
        self._trivia_category: dict = {}
        # context key currently awaiting a category pick (see _cmd_trivia)
        self._trivia_choosing: set = set()
        # "name" → opentdb category id, fetched once and cached (rarely changes)
        self._trivia_categories: dict[str, int] = {}
        # from_id → active MUD session; every session dict carries "last_active"
        # (time.monotonic()) so _sweep_mud_sessions can reclaim abandoned games
        # Keyed by from_id (DM games) or (adapter, channel) tuple (shared channel games)
        self._mud_sessions: dict[str | tuple, dict] = {}
        self._sweep_task: asyncio.Task | None = None
        # from_id → in-progress guided SKYWARN report {"step": int, "answers": {...}}
        self._skywarn_sessions: dict[str, dict] = {}

    def _resolve_coords(self) -> tuple[float | None, float | None]:
        """Return best-available (lat, lon): config override → weather service → station base."""
        if self._lat is not None and self._lon is not None:
            return self._lat, self._lon
        if self._state is not None:
            slat = getattr(self._state, "_base_lat", None)
            slon = getattr(self._state, "_base_lon", None)
            if slat is not None and slon is not None:
                return slat, slon
        return None, None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        if not self.enabled:
            log.info("MeshBot: disabled in config")
            return
        self._start_time = time.time()
        self._client = httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT, "Accept-Language": "en"},
            timeout=20.0,
            follow_redirects=True,
        )
        asyncio.ensure_future(self._prefetch_tles())
        self._sweep_task = asyncio.ensure_future(self._sweep_mud_sessions())
        log.info("MeshBot: started, channels=%s, mention_required_channels=%s, reply_dm=%s",
                 self._channels, self._mention_required_channels, self._reply_dm)

    async def stop(self) -> None:
        if self._sweep_task:
            self._sweep_task.cancel()
            self._sweep_task = None
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _sweep_mud_sessions(self) -> None:
        """Periodically reclaim MUD sessions abandoned mid-game. A lazy
        check-on-next-message isn't enough here — if the sender never sends
        anything more, nothing else would ever look at their session again."""
        try:
            while True:
                await asyncio.sleep(_MUD_SWEEP_INTERVAL_SEC)
                now = time.monotonic()
                expired = [
                    fid for fid, sess in self._mud_sessions.items()
                    if now - sess.get("last_active", now) > _MUD_SESSION_TIMEOUT_SEC
                ]
                for fid in expired:
                    del self._mud_sessions[fid]
                if expired:
                    log.info("MeshBot: expired %d idle mud session(s)", len(expired))
        except asyncio.CancelledError:
            pass

    # ── Inbound pipeline hook ─────────────────────────────────────────────────

    async def handle(self, msg: NormalizedMessage) -> None:
        if not self.enabled or not self._client:
            return
        if msg.from_id in ("local", "NWS", "wx-service", "weather-bot", "mesh-bot"):
            return
        if msg.source_adapter in ("wx-service",):
            return
        if self._adapter_filter and msg.source_adapter not in self._adapter_filter:
            return
        # Accept DMs always; accept channel messages only on configured channels.
        # MeshCore source_channel format is "ch2:testing" or "ch2"; DMs are "DM".
        ch = (msg.source_channel or "").lower()
        is_dm = ch == "dm"
        mention_required = False
        if not is_dm:
            if ":" in ch:
                ch_idx_part, ch_name_part = ch.split(":", 1)  # e.g. "ch2", "testing"
            else:
                ch_idx_part, ch_name_part = ch, ""            # e.g. "ch2", ""
            # Extract bare numeric index ("ch2" → "2")
            ch_num = ch_idx_part[2:] if ch_idx_part.startswith("ch") else ""

            def _matches(cfg_entry: str) -> bool:
                c = cfg_entry.lstrip("#").lower()
                if c == "*":                           return True   # wildcard
                if ch_name_part and c == ch_name_part: return True   # by name: "testing"
                if c == ch_idx_part:                   return True   # "ch2"
                if ch_num and c == ch_num:             return True   # "2"
                # "ch2:testing" format — as shown in the Device channels hint in Settings
                if ":" in c:
                    c_idx, c_name = c.split(":", 1)
                    if c_idx == ch_idx_part:           return True   # idx part matches
                    if c_name and c_name == ch_name_part: return True  # name part matches
                return False

            if self._channels:
                on_channel = any(_matches(c) for c in self._channels)
            else:
                on_channel = False

            if not on_channel:
                log.debug(
                    "MeshBot: channel msg ignored (source=%r not in configured channels %s)",
                    msg.source_channel, self._channels,
                )
                return

            # On some channels (e.g. general-purpose ones like Public/MaineNet,
            # as opposed to a dedicated bot-test channel), even a real command
            # word shouldn't auto-fire from ordinary conversation — "the
            # weather's nice today" matching the "weather" trigger is exactly
            # the false positive this guards against. Reuses _matches() against
            # a separate configured list; doesn't apply to answering a question
            # the bot itself already asked (trivia answer, category pick below),
            # only to starting something new from a bare command word.
            mention_required = any(_matches(c) for c in self._mention_required_channels)

            # A pending category prompt claims the next channel message as the
            # pick, same idea as the DM version below.
            ch_key = self._trivia_channel_key(msg)
            if ch_key in self._trivia_choosing:
                asyncio.ensure_future(self._dispatch_trivia_category_pick(msg, ch_key, msg.body))
                return
            # A bare A/B/C/D reply while a channel trivia question is pending is
            # the answer — open to whoever answers first, since channel senders
            # have no reliable identity to restrict it to. Bypasses the cooldown,
            # same reasoning as the DM answer path below.
            am = _TRIVIA_ANSWER_RE.match(msg.body)
            if am and ch_key in self._trivia_channel_pending:
                asyncio.ensure_future(
                    self._dispatch_trivia_answer(msg, am.group(1).upper(), channel_key=ch_key)
                )
                return
            # An active shared MUD session on this channel claims every further
            # channel message as a game command — same contract as the DM
            # version below, and deliberately ahead of the @mention gate so
            # moves don't need a mention. Only startable on channels NOT in
            # mention_required_channels (see _cmd_mud_start), so this can't
            # swallow conversation on a general channel. Checked after the
            # trivia interceptions so an in-flight trivia round still works.
            if ch_key in self._mud_sessions:
                asyncio.ensure_future(self._dispatch_mud(msg))
                return

        if is_dm:
            log.info("MeshBot: DM received from=%s body=%r", (msg.from_display or msg.from_id or "?")[:20], msg.body[:60])

            # An active MUD session claims every further DM from that sender as a
            # game command (until "quit"/"exit") — checked first so "look"/"north"
            # etc. aren't swallowed by the generic command regex below. Bypasses
            # the cooldown: it's already a private 1:1 exchange, not a broadcast.
            if msg.from_id in self._mud_sessions:
                asyncio.ensure_future(self._dispatch_mud(msg))
                return
            # Same idea for an in-progress guided SKYWARN report.
            if msg.from_id in self._skywarn_sessions:
                asyncio.ensure_future(self._dispatch_skywarn(msg))
                return
            # A pending "which trivia category?" prompt claims the next DM.
            if ("dm", msg.from_id) in self._trivia_choosing:
                asyncio.ensure_future(self._dispatch_trivia_category_pick(msg, ("dm", msg.from_id), msg.body))
                return
            # A bare A/B/C/D reply while a trivia question is pending is the answer.
            # Anything else falls through to the normal command path so an ignored
            # question doesn't block the sender from using other commands.
            am = _TRIVIA_ANSWER_RE.match(msg.body)
            if am and msg.from_id in self._trivia_pending:
                asyncio.ensure_future(self._dispatch_trivia_answer(msg, am.group(1).upper()))
                return

        if mention_required and not _is_mentioned(msg.body, self._mention_name):
            # This channel requires an explicit @mention for anything to fire —
            # skip command matching entirely rather than letting an ordinary
            # sentence that happens to contain a trigger word dispatch a reply.
            return

        m = _CMD_RE.search(msg.body)
        if m:
            word = m.group(1).lower().rstrip("?")   # normalise "weather?" → "weather"
            cmd  = _CMD_ALIASES.get(word, word)
            args = (m.group(2) or "").strip()
        else:
            # No known command word found. DMs are a deliberate 1:1 request to the
            # bot, so always answer — silence there looks like a dead bot. Channel
            # traffic is only answered if it @-mentions the bot; otherwise every
            # unrelated chat message on a monitored channel would get an error reply.
            if not is_dm and not _is_mentioned(msg.body, self._mention_name):
                return
            cmd, args = "unknown", ""

        # Global cooldown — prevent burst flooding
        now = time.monotonic()
        if now - self._global_ts < self._global_cooldown:
            log.info("MeshBot: cmd=%s dropped — global cooldown (%.1fs remaining)",
                     cmd, self._global_cooldown - (now - self._global_ts))
            return
        # Per-user cooldown
        if now - self._user_ts.get(msg.from_id, 0) < self._user_cooldown:
            log.info("MeshBot: cmd=%s from=%s dropped — per-user cooldown (%.1fs remaining)",
                     cmd, msg.from_id[:12], self._user_cooldown - (now - self._user_ts.get(msg.from_id, 0)))
            return

        self._global_ts = now
        self._user_ts[msg.from_id] = now

        asyncio.ensure_future(self._dispatch(msg, cmd, args, mention_required))

    async def _dispatch(self, msg: NormalizedMessage, cmd: str, args: str,
                        mention_required: bool = False) -> None:
        from_id = (msg.from_display or msg.from_id or "?")[:20]
        log.info("MeshBot: cmd=%s from=%s args=%r adapter=%s", cmd, from_id, args[:40], msg.source_adapter)
        try:
            if cmd == "ping":
                reply = self._cmd_ping(msg)
            elif cmd == "weather":
                reply = await self._cmd_weather(args)
            elif cmd == "overhead":
                reply = await self._cmd_overhead()
            elif cmd == "satpass":
                reply = await self._cmd_satpass(args)
            elif cmd == "solar":
                reply = await self._cmd_solar()
            elif cmd == "ships":
                reply = await self._cmd_ships()
            elif cmd == "fcc":
                reply = await self._cmd_fcc(args)
            elif cmd == "trivia":
                reply = await self._cmd_trivia(msg)
            elif cmd == "score":
                reply = await self._cmd_trivia_score(msg)
            elif cmd == "leaderboard":
                reply = await self._cmd_trivia_leaderboard()
            elif cmd == "mud":
                reply = self._cmd_mud_start(msg, mention_required)
            elif cmd == "skywarn":
                reply = await self._cmd_skywarn(msg)
            elif cmd == "dad":
                reply = await self._cmd_dad()
            elif cmd == "alerts":
                reply = await self._cmd_alerts()
            elif cmd == "metar":
                reply = await self._cmd_metar(args)
            elif cmd == "sun":
                reply = await self._cmd_sun()
            elif cmd == "nodes":
                reply = await self._cmd_nodes(args)
            elif cmd == "aprs":
                reply = await self._cmd_aprs(args)
            elif cmd == "anomalies":
                reply = await self._cmd_anomalies()
            elif cmd == "tide":
                reply = await self._cmd_tide()
            elif cmd == "grid":
                reply = self._cmd_grid(args)
            elif cmd == "id":
                reply = self._cmd_id()
            elif cmd == "path":
                reply = self._cmd_path(msg)
            elif cmd == "moon":
                reply = self._cmd_moon()
            elif cmd == "dxcc":
                reply = await self._cmd_dxcc(args)
            elif cmd == "contest":
                reply = await self._cmd_contest()
            elif cmd == "help":
                reply = self._cmd_help()
            elif cmd == "unknown":
                reply = self._cmd_unknown()
            else:
                return
        except Exception as exc:
            log.warning("MeshBot: %s handler error: %s", cmd, exc)
            reply = f"{cmd}: error ({type(exc).__name__})"

        if reply:
            # Trivia's "question ready" path sends directly via _send_trivia_question
            # (splitting into two messages when needed) and returns "" here to skip
            # this generic single-message send; every other command still returns
            # a normal string that goes out exactly as before.
            await self._send(msg, reply)

        # Record activity in DB and push live WS event
        from_display = (msg.from_display or msg.from_id or "?")
        if self._db:
            try:
                await self._db.save_bot_activity(
                    from_id=msg.from_id or "",
                    from_display=from_display,
                    command=cmd,
                    args=args,
                    adapter=msg.source_adapter or "",
                    response=reply,
                )
            except Exception as exc:
                log.debug("MeshBot: failed to save activity: %s", exc)
        if self._router:
            from datetime import datetime, timezone
            try:
                await self._router.broadcast_ws_event("bot_activity", {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "from_id": msg.from_id or "",
                    "from_display": from_display,
                    "command": cmd,
                    "args": args,
                    "adapter": msg.source_adapter or "",
                    "response": reply,
                })
            except Exception:
                pass

    async def _send(self, msg: NormalizedMessage, text: str) -> None:
        if not self._router:
            return
        body = text[:self._max_len]
        from_id = msg.from_id or ""
        ch = (msg.source_channel or "").lower()
        came_via_dm = ch == "dm"

        # DMs always get a DM reply; reply_dm setting only governs channel-sourced messages.
        if came_via_dm or self._reply_dm:
            to_id: str | None = from_id if _HEX_NODE_RE.match(from_id) else None
            if to_id is not None:
                raw: dict = {}
                log.info("MeshBot: DM reply to %s/%s: %s", msg.source_adapter, from_id[:12], body[:60])
            else:
                # reply_dm wants a DM, but polled channel messages never carry a
                # sender pubkey (MeshCore protocol), so there's no one to DM.
                # Fall back to the channel the message was received on — NOT the
                # adapter's configured default channel — so the reply still reaches
                # the sender instead of vanishing into whatever channel the adapter
                # happens to be set to.
                ch_idx = _parse_channel_idx(msg.source_channel)
                raw = {"channel_idx": ch_idx} if ch_idx is not None else {}
                log.info("MeshBot: no sender pubkey for DM reply — replying on origin channel %s/%s: %s",
                         msg.source_adapter, msg.source_channel, body[:60])
        else:
            # Channel message with reply_dm=false → broadcast back on the same channel
            to_id = None
            ch_idx = _parse_channel_idx(msg.source_channel)
            raw = {"channel_idx": ch_idx} if ch_idx is not None else {}
            log.info("MeshBot: channel reply on %s/%s: %s", msg.source_adapter, msg.source_channel, body[:60])

        await self._router.send(
            body=body,
            adapter_names=[msg.source_adapter],
            to_id=to_id,
            priority=Priority.NORMAL,
            raw=raw or None,
        )

    # ── Command handlers ──────────────────────────────────────────────────────

    # ── grid ──────────────────────────────────────────────────────────────────

    @staticmethod
    def _latlon_to_grid(lat: float, lon: float) -> str:
        lon += 180; lat += 90
        g  = chr(ord('A') + int(lon / 20))
        g += chr(ord('A') + int(lat / 10))
        g += str(int((lon % 20) / 2))
        g += str(int(lat % 10))
        g += chr(ord('a') + int((lon % 2) * 12))
        g += chr(ord('a') + int((lat % 1) * 24))
        return g

    @staticmethod
    def _grid_to_latlon(grid: str) -> tuple[float, float]:
        g = grid.strip().upper()
        if len(g) < 2:
            raise ValueError("grid too short")
        lon = (ord(g[0]) - ord('A')) * 20 - 180
        lat = (ord(g[1]) - ord('A')) * 10 - 90
        if len(g) >= 4:
            lon += int(g[2]) * 2
            lat += int(g[3])
        if len(g) >= 6:
            lon += (ord(g[4].lower()) - ord('a') + 0.5) * 2 / 24
            lat += (ord(g[5].lower()) - ord('a') + 0.5) / 24
        else:
            lon += 1.0
            lat += 0.5
        return lat, lon

    def _cmd_grid(self, args: str) -> str:
        a = args.strip()
        # No args → own grid from configured lat/lon
        if not a:
            lat, lon = self._resolve_coords()
            if lat is None:
                return "grid: observer position not set (configure base location in Settings)"
            return f"grid: {self._latlon_to_grid(lat, lon)} ({lat:.4f}, {lon:.4f})"
        # Two numbers → encode lat lon
        parts = a.split()
        if len(parts) == 2:
            try:
                lat, lon = float(parts[0]), float(parts[1])
                return f"grid: {self._latlon_to_grid(lat, lon)}"
            except ValueError:
                pass
        # Single token → try decode as Maidenhead
        token = parts[0]
        if re.match(r'^[A-Ra-r]{2}[0-9]{2}([A-Xa-x]{2})?$', token):
            try:
                lat, lon = self._grid_to_latlon(token)
                return f"grid: {token.upper()} → {lat:.3f}, {lon:.3f}"
            except Exception:
                pass
        return f"grid: '{a}' — use: grid | grid FN43 | grid 44.0 -69.1"

    # ── id ────────────────────────────────────────────────────────────────────

    def _cmd_id(self) -> str:
        # Deliberately NO operator callsign here — the operator doesn't use
        # their callsign on MeshCore, and the bot identifying with it on-air
        # was unwanted.
        try:
            ver = open(os.path.join(os.path.dirname(__file__), "..", "..", "VERSION")).read().strip()
        except Exception:
            ver = "?"
        up_sec  = int(time.time() - self._start_time) if self._start_time else 0
        days, r = divmod(up_sec, 86400)
        hrs, r  = divmod(r, 3600)
        mins    = r // 60
        if days:
            up_str = f"{days}d{hrs}h"
        elif hrs:
            up_str = f"{hrs}h{mins}m"
        else:
            up_str = f"{mins}m"
        lat, lon = self._resolve_coords()
        grid_str = self._latlon_to_grid(lat, lon) if lat is not None else "?"
        return f"SM | v{ver} | up {up_str} | {grid_str}"

    def _cmd_path(self, msg: NormalizedMessage) -> str:
        """Decode the relay path of the requester's OWN message (the pattern
        popularized by meshcore-bot's path command): the raw RX log heard the
        packet's actual relay chain and the adapter correlated it onto this
        message; here we resolve each hop hash to a repeater name."""
        hop_count = msg.hop_count
        path = (msg.path or "").strip()
        if not path:
            if hop_count == 0:
                return "Path: direct — I heard you with no relays (0 hops)."
            return (f"Path: no relay data captured for your message"
                    + (f" ({hop_count} hops)" if hop_count else "")
                    + " — send 'path' again; the next packet usually correlates.")
        adapter = None
        if self._router:
            adapter = self._router._adapters.get(msg.source_adapter)
        names = []
        for h in path.split(","):
            resolved = None
            if adapter is not None and hasattr(adapter, "_resolve_traffic_node"):
                resolved = adapter._resolve_traffic_node(h)
            if resolved:
                label = resolved["name"]
                if resolved.get("ambiguous"):
                    label += "?"
                names.append(label)
            else:
                names.append(h)
        return f"Path: you → {' → '.join(names)} → me ({len(names)} hop{'s' if len(names) != 1 else ''})"

    # ── moon ──────────────────────────────────────────────────────────────────

    @staticmethod
    def _julian_day(dt: "datetime.datetime") -> float:
        a = (14 - dt.month) // 12
        y = dt.year + 4800 - a
        m = dt.month + 12 * a - 3
        return (dt.day + (153 * m + 2) // 5 + 365 * y + y // 4
                - y // 100 + y // 400 - 32045
                + (dt.hour * 3600 + dt.minute * 60 + dt.second) / 86400.0)

    def _cmd_moon(self) -> str:
        now = datetime.now(timezone.utc)
        jd  = self._julian_day(now)
        synodic = 29.53058867
        known_new_jd = 2451550.1           # 2000-01-06 18:14 UTC
        age = (jd - known_new_jd) % synodic
        illum = int((1 - math.cos(2 * math.pi * age / synodic)) / 2 * 100)
        if   age <  1.85: phase = "New Moon"
        elif age <  7.38: phase = "Waxing Crescent"
        elif age <  9.22: phase = "First Quarter"
        elif age < 14.77: phase = "Waxing Gibbous"
        elif age < 16.61: phase = "Full Moon"
        elif age < 22.15: phase = "Waning Gibbous"
        elif age < 23.99: phase = "Last Quarter"
        else:             phase = "Waning Crescent"
        return f"Moon: {phase} {illum}% (day {age:.1f}/{synodic:.0f})"

    def _cmd_ping(self, msg: NormalizedMessage) -> str:
        parts = ["pong"]
        snr = msg.raw.get("snr") if msg.raw else None
        if snr is not None:
            parts.append(f"SNR:{snr:+.1f}dB")
        hops = msg.hop_count
        if hops is not None:
            parts.append(f"Hops:{hops}")
        return " | ".join(parts)

    async def _cmd_contest(self) -> str:
        """Active and next-up contests from the WA7BNM 8-day RSS feed
        (contestcalendar.com — NO hyphen: the previous implementation pointed
        at 'contest-calendar.com/...json', a domain that doesn't exist, so
        this command had never actually worked before this rewrite).
        RSS item shape: <title>name</title>, <description> in one of three
        forms: '0100Z-0230Z, Jul 10' | '1200Z, Jul 11 to 1200Z, Jul 12' |
        two same-day windows joined by ' and '."""
        import datetime as _dt
        try:
            resp = await self._client.get(
                "https://www.contestcalendar.com/calendar.rss", timeout=8,
            )
            resp.raise_for_status()
            xml = resp.text
        except Exception as exc:
            return f"contest: fetch failed ({exc})"

        now = _dt.datetime.now(_dt.timezone.utc)

        def _mk(hhmm: str, mon: str, day: str) -> "_dt.datetime | None":
            try:
                hh, mm = int(hhmm[:2]), int(hhmm[2:])
                extra_day = 0
                if hh >= 24:              # feed writes end-of-day as "2400Z"
                    hh -= 24
                    extra_day = 1
                dt = _dt.datetime.strptime(f"{mon} {day} {now.year}", "%b %d %Y").replace(
                    hour=hh, minute=mm, tzinfo=_dt.timezone.utc) + _dt.timedelta(days=extra_day)
                if (now - dt).days > 180:  # December feed item parsed in early January
                    dt = dt.replace(year=now.year + 1)
                return dt
            except ValueError:
                return None

        active, upcoming = [], []
        for m in re.finditer(r"<item>(.*?)</item>", xml, re.S):
            item = m.group(1)
            t = re.search(r"<title>(.*?)</title>", item, re.S)
            d = re.search(r"<description>(.*?)</description>", item, re.S)
            if not t or not d:
                continue
            name = re.sub(r"\s+", " ", t.group(1)).strip()[:32]
            desc = re.sub(r"\s+", " ", d.group(1)).strip()
            windows = []
            for r_ in re.finditer(r"(\d{4})Z-(\d{4})Z, (\w{3}) (\d{1,2})", desc):
                s = _mk(r_.group(1), r_.group(3), r_.group(4))
                e = _mk(r_.group(2), r_.group(3), r_.group(4))
                if s and e:
                    if e <= s:            # window crosses midnight
                        e += _dt.timedelta(days=1)
                    windows.append((s, e))
            for r_ in re.finditer(r"(\d{4})Z, (\w{3}) (\d{1,2}) to (\d{4})Z, (\w{3}) (\d{1,2})", desc):
                s = _mk(r_.group(1), r_.group(2), r_.group(3))
                e = _mk(r_.group(4), r_.group(5), r_.group(6))
                if s and e:
                    windows.append((s, e))
            if not windows:
                continue
            if any(s <= now <= e for s, e in windows):
                active.append(name)
            else:
                nxt = min((s for s, _e in windows if s > now), default=None)
                if nxt:
                    upcoming.append((nxt, f"{name} ({nxt.strftime('%a %H%M')}Z)"))

        parts = []
        if active:
            parts.append("ACTIVE: " + " | ".join(active[:3]))
        if upcoming:
            upcoming.sort(key=lambda x: x[0])
            parts.append("NEXT: " + " | ".join(lbl for _, lbl in upcoming[:2]))
        return " ".join(parts) if parts else "contest: none active or upcoming this week"

    async def _cmd_dxcc(self, args: str) -> str:
        cs = args.strip().upper()
        if not cs or not re.match(r'^[A-Z0-9/]{3,12}$', cs):
            return "dxcc: provide a callsign, e.g. dxcc DL5YYM"
        # Use the base call for prefix lookup (strip portable suffix like /P or /MM)
        base = cs.split("/")[0]
        try:
            resp = await self._client.get(
                "https://dxheat.com/dxcc/",
                params={"call": base},
                timeout=8,
            )
            resp.raise_for_status()
            d = resp.json()
        except Exception as exc:
            return f"dxcc: lookup failed ({exc})"
        if not d or "name" not in d:
            return f"dxcc: no DXCC entity found for {base}"
        name      = d.get("name", "?")
        pfx       = d.get("prefix", "?")
        continent = d.get("continent", "?")
        cq        = d.get("cq_zone", "?")
        itu       = d.get("itu_zone", "?")
        return f"{cs}: {name} ({pfx}) {continent} CQ{cq} ITU{itu}"

    def _cmd_help(self) -> str:
        # Kept under ~155 chars on purpose: config.yaml's default max_reply_len is
        # 160, and this used to silently exceed it (228 chars), hard-truncating
        # mid-word before "skywarn"/"trivia"/"mud" ever appeared. Per-command
        # usage (e.g. "fcc <callsign>") is shown by that command's own error
        # reply when called with no args, so it's dropped here to fit everything.
        return ("Cmds: ping wx overhead satpass solar ships fcc aprs dxcc contest nodes anomalies "
                "grid moon id path dad alerts metar sun tide skywarn trivia score lb mud help")

    def _cmd_unknown(self) -> str:
        return "Unrecognized command. Send 'help' for a list."

    # ── Weather ───────────────────────────────────────────────────────────────

    async def _cmd_weather(self, args: str) -> str:
        args = args.strip()
        client = self._client
        assert client is not None

        if not args:
            lat, lon = self._resolve_coords()
            if lat is None:
                return "weather: provide a zip/city or set base location in Settings"
            return await self._fetch_weather_coords(client, lat, lon, "base")

        m = _ZIP_RE.search(args)
        if m:
            return await self._fetch_weather_zip(client, m.group(1))

        # Free-text place name — Nominatim geocode then NWS
        return await self._fetch_weather_place(client, args)

    async def _fetch_weather_zip(self, client: Any, zip5: str) -> str:
        geo = await client.get(
            NOMINATIM_URL,
            params={"postalcode": zip5, "countrycodes": "us", "format": "json", "limit": "1"},
        )
        geo.raise_for_status()
        hits = geo.json()
        if not hits:
            return f"zip {zip5} not found"
        lat   = float(hits[0]["lat"])
        lon   = float(hits[0]["lon"])
        place = hits[0].get("display_name", zip5).split(",")[0].strip()
        return await self._fetch_weather_coords(client, lat, lon, place or zip5)

    async def _fetch_weather_place(self, client: Any, query: str) -> str:
        geo = await client.get(
            NOMINATIM_URL,
            params={"q": query, "countrycodes": "us", "format": "json", "limit": "1"},
        )
        geo.raise_for_status()
        hits = geo.json()
        if not hits:
            # retry without US restriction — might be a Canadian city, etc.
            geo2 = await client.get(
                NOMINATIM_URL,
                params={"q": query, "format": "json", "limit": "1"},
            )
            geo2.raise_for_status()
            hits = geo2.json()
        if not hits:
            return f"place '{query}' not found"
        lat   = float(hits[0]["lat"])
        lon   = float(hits[0]["lon"])
        place = hits[0].get("display_name", query).split(",")[0].strip()
        return await self._fetch_weather_coords(client, lat, lon, place or query)

    async def _fetch_weather_coords(self, client: Any, lat: float, lon: float, label: str) -> str:
        try:
            pts = await client.get(f"{NWS_BASE}/points/{lat:.4f},{lon:.4f}")
            pts.raise_for_status()
        except Exception:
            return f"WX: NWS doesn't cover {label} (non-US or offshore?)"
        nws          = pts.json()["properties"]
        forecast_url = nws["forecast"]
        stations_url = nws["observationStations"]

        current = "obs unavail"
        try:
            st = await client.get(stations_url)
            st.raise_for_status()
            sid   = st.json()["features"][0]["properties"]["stationIdentifier"]
            obs_r = await client.get(f"{NWS_BASE}/stations/{sid}/observations/latest")
            obs_r.raise_for_status()
            obs    = obs_r.json()["properties"]
            temp_c = (obs.get("temperature") or {}).get("value")
            temp_f = round(temp_c * 9 / 5 + 32) if temp_c is not None else None
            desc   = obs.get("textDescription", "")
            w_mps  = (obs.get("windSpeed") or {}).get("value")
            w_mph  = round(w_mps * 2.237) if w_mps is not None else None
            w_deg  = (obs.get("windDirection") or {}).get("value")
            w_dir  = _WIND_DIRS[int((w_deg + 11.25) / 22.5) % 16] if w_deg is not None else ""
            parts: list[str] = []
            if temp_f is not None: parts.append(f"{temp_f}F")
            if desc: parts.append(desc)
            if w_mph is not None: parts.append(f"Wind {w_mph}mph {w_dir}".strip())
            if parts: current = ", ".join(parts)
        except Exception as exc:
            log.debug("MeshBot/weather obs error: %s", exc)

        forecast = "fcst unavail"
        try:
            fc = await client.get(forecast_url)
            fc.raise_for_status()
            p        = fc.json()["properties"]["periods"][0]
            forecast = f"{p['name']}: {p['temperature']}F {p['shortForecast']} wind {p['windSpeed']}"
        except Exception as exc:
            log.debug("MeshBot/weather fcst error: %s", exc)

        return f"WX {label}|Now:{current}|{forecast}"

    # ── Aircraft overhead ─────────────────────────────────────────────────────

    async def _cmd_overhead(self) -> str:
        lat, lon = self._resolve_coords()
        if lat is None or lon is None:
            return "overhead: observer position not set (set base location in Settings or mesh_bot.lat/lon in config)"
        try:
            if self._dump1090.startswith("http://") or self._dump1090.startswith("https://"):
                client = self._client
                assert client is not None
                r = await client.get(self._dump1090, timeout=8.0)
                r.raise_for_status()
                text = r.text
            else:
                text = await asyncio.to_thread(self._read_dump1090)
        except FileNotFoundError:
            return f"overhead: {self._dump1090} not found (is dump1090 running?)"
        except Exception as exc:
            return f"overhead: read error ({exc})"
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return "overhead: invalid aircraft.json"

        now_ts = data.get("now", time.time())
        aircraft = data.get("aircraft", [])
        nearby: list[tuple[float, dict]] = []

        for ac in aircraft:
            ac_lat = ac.get("lat")
            ac_lon = ac.get("lon")
            if ac_lat is None or ac_lon is None:
                continue
            # Skip stale entries (not updated in last 60 s relative to file timestamp)
            seen_ago = ac.get("seen", 999)
            if seen_ago > 60:
                continue
            dist = _haversine_nm(lat, lon, ac_lat, ac_lon)
            if dist <= self._radius_nm:
                nearby.append((dist, ac))

        if not nearby:
            return f"No aircraft within {self._radius_nm:.0f}nm"

        nearby.sort(key=lambda x: x[0])
        dist, ac = nearby[0]

        callsign = (ac.get("flight") or ac.get("hex", "?")).strip()
        alt      = ac.get("alt_baro") or ac.get("altitude")
        speed    = ac.get("gs") or ac.get("speed")
        track    = ac.get("track")
        bearing  = _bearing(lat, lon, ac.get("lat"), ac.get("lon"))
        direction = _card8(bearing)

        parts = [callsign]
        if alt is not None:
            parts.append(f"{int(alt)}ft")
        if speed is not None:
            parts.append(f"{int(speed)}kt")
        parts.append(f"{direction} {dist:.1f}nm")
        if track is not None:
            parts.append(f"hdg{int(track)}")

        count_str = f" (+{len(nearby)-1} more)" if len(nearby) > 1 else ""
        return "OVHD: " + " ".join(parts) + count_str

    def _read_dump1090(self) -> str:
        with open(self._dump1090, "r", encoding="utf-8", errors="replace") as f:
            return f.read()

    # ── Satellite passes ──────────────────────────────────────────────────────

    async def _cmd_satpass(self, args: str) -> str:
        lat, lon = self._resolve_coords()
        if lat is None or lon is None:
            return "satpass: observer position not set (set base location in Settings or mesh_bot.lat/lon in config)"
        # Determine which satellite to predict
        want = args.strip().upper() if args.strip() else None
        if self._tle_cache.expired():
            try:
                await asyncio.wait_for(self._prefetch_tles(), timeout=15.0)
            except asyncio.TimeoutError:
                if not self._tle_cache._data:
                    return "satpass: TLE fetch timed out — try again in a minute"

        # Pick target: explicit name first, else first in configured list
        target_name: str | None = None
        target_lines: tuple[str, str] | None = None
        if want:
            for key in self._tle_cache._data:
                if want in key:
                    target_name = key
                    target_lines = self._tle_cache.get(key)
                    break
            if target_lines is None:
                return f"satpass: '{args}' not found in TLE cache"
        else:
            for t in self._tle_targets:
                target_lines = self._tle_cache.get(t)
                if target_lines:
                    target_name = t
                    break

        if target_lines is None or target_name is None:
            err = self._tle_cache.last_error
            suffix = f" ({err})" if err else " — check internet connection"
            return f"satpass: no TLE data{suffix}"

        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._compute_pass, target_name, target_lines[0], target_lines[1], lat, lon),
                timeout=30.0,
            )
        except asyncio.TimeoutError:
            return "satpass: computation timed out (skyfield)"

    def _compute_pass(self, name: str, line1: str, line2: str, lat: float, lon: float) -> str:
        try:
            from skyfield.api import load, wgs84, EarthSatellite
        except ImportError:
            return "satpass: skyfield not installed (pip install skyfield)"

        ts = load.timescale(builtin=True)
        sat = EarthSatellite(line1, line2, name, ts)
        observer = wgs84.latlon(lat, lon)

        t0 = ts.now()
        t1 = ts.tt_jd(t0.tt + 1.0)   # search up to 24h ahead

        times, events = sat.find_events(observer, t0, t1, altitude_degrees=10.0)

        # Group into passes: AOS(0) + TCA(1) + LOS(2)
        passes: list[dict] = []
        current: dict = {}
        for t, ev in zip(times, events):
            if ev == 0:
                current = {"aos": t}
            elif ev == 1 and current:
                current["tca"] = t
                diff = (sat - observer).at(t)
                alt, _, _ = diff.altaz()
                current["max_el"] = alt.degrees
            elif ev == 2 and "aos" in current:
                current["los"] = t
                dur = (t - current["aos"]) * 86400
                current["duration_s"] = int(dur)
                passes.append(current)
                current = {}

        if not passes:
            return f"{name[:12]}: no pass in next 24h (el>10)"

        p = passes[0]
        aos_dt = p["aos"].utc_datetime()
        aos_str = aos_dt.strftime("%H:%Mz")
        date_str = aos_dt.strftime("%d%b").upper()
        max_el  = p.get("max_el", 0)
        dur_min = p.get("duration_s", 0) // 60
        dur_sec = p.get("duration_s", 0) % 60
        short_name = name.split("(")[0].strip()[:10]
        return f"{short_name} {date_str} AOS {aos_str} El {max_el:.0f}deg {dur_min}m{dur_sec:02d}s"

    async def _prefetch_tles(self) -> None:
        client = self._client
        if client is None:
            return

        async def _fetch_one(fmt: str, url: str) -> tuple[int, str]:
            src = url.split("/")[2]
            try:
                r = await client.get(url, timeout=_TLE_SOURCE_TIMEOUT)
                r.raise_for_status()
                n = self._tle_cache.load_json(r.json()) if fmt == "json" else self._tle_cache.load_text(r.text)
                log.debug("MeshBot: loaded %d TLEs from %s", n, src)
                return n, ""
            except Exception as exc:
                log.warning("MeshBot: TLE fetch failed (%s): %s", src, exc)
                return 0, f"{src}: {type(exc).__name__}"

        results = await asyncio.gather(*[_fetch_one(fmt, url) for fmt, url in TLE_SOURCES])
        total = sum(n for n, _ in results)
        errors = [e for _, e in results if e]
        if errors and not total:
            self._tle_cache.last_error = "; ".join(errors)
        elif total:
            self._tle_cache.last_error = ""
        log.info("MeshBot: %d TLEs cached from %d source(s)", total, len(TLE_SOURCES))

    # ── Solar / space weather ─────────────────────────────────────────────────

    async def _cmd_solar(self) -> str:
        now = time.monotonic()
        if self._solar_cache and now - self._solar_ts < self._solar_cache_sec:
            return self._solar_cache
        client = self._client
        assert client is not None
        try:
            r = await client.get(HAMQSL_URL, timeout=10.0)
            r.raise_for_status()
            root = ET.fromstring(r.text)
            # hamqsl XML nests data inside <solardata> — use XPath .//<tag>
            sfi  = root.findtext(".//solarflux") or "?"
            ssn  = root.findtext(".//sunspots")  or "?"
            ki   = root.findtext(".//kindex")    or "?"
            ai   = root.findtext(".//aindex")    or "?"
            upd  = root.findtext(".//updated")   or ""
            result = f"Solar SFI:{sfi} SSN:{ssn} K:{ki} A:{ai}"
            if upd:
                result += f" ({upd[:12]})"
        except Exception as exc:
            log.warning("MeshBot/solar fetch error: %s", exc)
            result = f"solar: data unavailable ({type(exc).__name__})"
        self._solar_cache = result
        self._solar_ts = now
        return result

    # ── Nearby AIS ships ──────────────────────────────────────────────────────

    async def _cmd_ships(self) -> str:
        lat, lon = self._resolve_coords()
        radius = float((self._config.get("mesh_bot") or {}).get("ships_radius_nm", 50))
        vessels: list = []

        if self._router:
            for adapter in self._router._adapters.values():
                if not getattr(adapter, "_connected", False):
                    continue
                try:
                    nodes = await adapter.nodes()
                    for n in nodes:
                        if (n.node_id or "").startswith("mmsi:"):
                            vessels.append(n)
                except Exception:
                    pass

        if not vessels:
            return "ships: no AIS vessels visible (is ais_catcher adapter connected?)"

        def _dist(v) -> float:
            if lat is None or lon is None or v.lat is None or v.lon is None:
                return 9999.0
            return _haversine_nm(lat, lon, v.lat, v.lon)

        vessels.sort(key=_dist)
        if lat is not None:
            vessels = [v for v in vessels if _dist(v) <= radius]

        if not vessels:
            return f"ships: no vessels within {radius:.0f}nm"

        parts = []
        for v in vessels[:4]:
            d     = _dist(v)
            spd   = (v.meta or {}).get("speed_kts", "")
            label = v.display_name[:10]
            entry = f"{label} {d:.1f}nm"
            if spd:
                entry += f" {float(spd):.0f}kts"
            parts.append(entry)
        return f"Ships({len(vessels)}): " + " | ".join(parts)

    # ── FCC callsign lookup ───────────────────────────────────────────────────

    async def _cmd_fcc(self, args: str) -> str:
        call = args.strip().upper().split()[0] if args.strip() else ""
        if not call:
            return "Usage: fcc <callsign>  e.g. fcc W1ABC"
        client = self._client
        assert client is not None
        try:
            url = CALLOOK_URL.format(callsign=call)
            r = await client.get(url, timeout=8.0)
            r.raise_for_status()
            data = r.json()
            status = data.get("status", "INVALID")
            if status == "INVALID":
                return f"FCC: {call} not found"
            name    = data.get("name", "?")
            op_cls  = (data.get("current") or {}).get("operClass", "")
            exp     = ((data.get("otherInfo") or {}).get("expiryDate") or "")
            addr    = (data.get("address") or {}).get("line2", "")
            parts = [name]
            if op_cls:
                parts.append(op_cls)
            if addr:
                parts.append(addr)
            if exp:
                parts.append(f"exp {exp}")
            return f"FCC {call}: " + " | ".join(parts)
        except Exception as exc:
            log.warning("MeshBot/fcc error: %s", exc)
            return f"fcc: lookup failed ({type(exc).__name__})"

    # ── Trivia (scored multiple-choice) ───────────────────────────────────────

    @staticmethod
    def _trivia_channel_key(msg: NormalizedMessage) -> tuple:
        return (msg.source_adapter or "", (msg.source_channel or "").lower())

    async def _cmd_trivia(self, msg: NormalizedMessage) -> str:
        is_dm = (msg.source_channel or "").lower() == "dm"
        # DMs key the pending question by sender (from_id is a real pubkey there).
        # Channels can't do that — polled MeshCore channel messages carry no
        # reliable sender pubkey — so a channel question is instead scoped to the
        # channel itself and open to whoever answers first, like a live call-out.
        ctx_key = ("dm", msg.from_id) if is_dm else self._trivia_channel_key(msg)

        # Re-derive the full argument ourselves: the shared _CMD_RE args group
        # caps at ~40 chars, and a category name plus "stop"/"any" all need to
        # survive intact.
        m = re.search(r'trivia\s*(.*)', msg.body, re.IGNORECASE)
        arg = (m.group(1).strip() if m else "")
        sub = arg.lower()

        if sub in ("stop", "end", "pause"):
            self._trivia_paused.add(ctx_key)
            self._trivia_pending.pop(msg.from_id, None)
            self._trivia_channel_pending.pop(ctx_key, None)
            return "Trivia stopped. Send 'trivia' to play again."

        # Any other explicit "trivia ..." (including bare "trivia") re-enables
        # auto-continue for this context if it had been stopped.
        self._trivia_paused.discard(ctx_key)

        if is_dm:
            if msg.from_id in self._trivia_pending:
                p = self._trivia_pending[msg.from_id]
                return f"You still have a question open — reply A, B, C, or D (times out in {int(p['expires'] - time.monotonic())}s)"
        else:
            if ctx_key in self._trivia_channel_pending:
                p = self._trivia_channel_pending[ctx_key]
                return f"A question is already open here — reply A, B, C, or D (times out in {int(p['expires'] - time.monotonic())}s)"

        if sub in ("any", "all", "random"):
            self._trivia_category.pop(ctx_key, None)
        elif arg:
            cats = await self._load_trivia_categories()
            cid = self._match_trivia_category(cats, arg)
            if cid is None and cats:
                return f"trivia: no category matching '{arg}'. Try e.g. 'trivia science', 'trivia history', 'trivia sports', or 'trivia any'."
            self._trivia_category[ctx_key] = cid
        elif ctx_key not in self._trivia_category:
            # Bare "trivia" with no category chosen yet for this context (or
            # since the last "trivia stop") — ask instead of silently picking
            # random, the same way "mud" asks which game before playing.
            self._trivia_choosing.add(ctx_key)
            return "Pick a trivia category (e.g. science, history, sports, movies) or reply 'any' for random:"

        await self._send_next_trivia_question(msg, is_dm, ctx_key)
        return ""

    async def _dispatch_trivia_category_pick(self, msg: NormalizedMessage, ctx_key: tuple, text: str) -> None:
        self._trivia_choosing.discard(ctx_key)
        pick = text.strip().lower()
        if pick in ("any", "all", "random", ""):
            self._trivia_category.pop(ctx_key, None)
        else:
            cats = await self._load_trivia_categories()
            cid = self._match_trivia_category(cats, pick)
            if cid is None and cats:
                # Re-prompt rather than silently defaulting to random, so a typo
                # doesn't quietly start the wrong (or no) category.
                self._trivia_choosing.add(ctx_key)
                await self._send(msg, f"Didn't recognize '{text.strip()}'. Reply with a category name, or 'any' for random:")
                return
            self._trivia_category[ctx_key] = cid
        is_dm = ctx_key[0] == "dm"
        await self._send_next_trivia_question(msg, is_dm, ctx_key)

    async def _send_next_trivia_question(self, msg: NormalizedMessage, is_dm: bool, ctx_key: tuple) -> None:
        result = await self._next_trivia_question(msg, is_dm, ctx_key)
        if isinstance(result, str):
            await self._send(msg, result)  # a short error string — always fits in one message
        else:
            question, letters, options = result
            await self._send_trivia_question(msg, question, letters, options)

    async def _load_trivia_categories(self) -> dict[str, int]:
        if self._trivia_categories:
            return self._trivia_categories
        client = self._client
        assert client is not None
        try:
            r = await client.get(TRIVIA_CATEGORY_URL, timeout=8.0)
            r.raise_for_status()
            data = r.json()
            self._trivia_categories = {
                c["name"].lower(): c["id"] for c in data.get("trivia_categories", [])
            }
        except Exception as exc:
            log.warning("MeshBot/trivia category fetch error: %s", exc)
        return self._trivia_categories

    @staticmethod
    def _match_trivia_category(cats: dict[str, int], want: str) -> int | None:
        want = want.lower().strip()
        if want in cats:
            return cats[want]
        # opentdb names most subcategories "Entertainment: Video Games" etc. —
        # match on any word so "games" or "science" finds the right one.
        return next((cid for name, cid in cats.items() if want in name), None)

    async def _next_trivia_question(self, msg: NormalizedMessage, is_dm: bool,
                                    ctx_key: tuple) -> str | tuple[str, str, list[str]]:
        """Fetch and store one question for this context. Used both for a fresh
        'trivia' command and to auto-continue a round after an answer. Returns
        an error string, or (question, letters, options) for the caller to send
        via _send_trivia_question — this doesn't send it directly since the
        chosen mesh reply target/routing differs per call site."""
        client = self._client
        assert client is not None
        params = {"amount": "1", "type": "multiple"}
        category_id = self._trivia_category.get(ctx_key)
        if category_id:
            params["category"] = category_id
        try:
            r = await client.get(TRIVIA_URL, params=params, timeout=8.0)
            r.raise_for_status()
            data = r.json()
            if data.get("response_code") != 0 or not data.get("results"):
                return "trivia: no question available, try again"
            q = data["results"][0]
        except Exception as exc:
            log.warning("MeshBot/trivia error: %s", exc)
            return f"trivia: fetch failed ({type(exc).__name__})"

        import html
        question = html.unescape(q["question"])
        correct  = html.unescape(q["correct_answer"])
        wrong    = [html.unescape(a) for a in q.get("incorrect_answers", [])]
        options  = wrong + [correct]
        random.shuffle(options)
        letters  = "ABCD"[:len(options)]
        correct_letter = letters[options.index(correct)]

        pending = {
            "letter": correct_letter,
            "answer": correct,
            "expires": time.monotonic() + _TRIVIA_TIMEOUT_SEC,
        }
        if is_dm:
            self._trivia_pending[msg.from_id] = pending
        else:
            self._trivia_channel_pending[ctx_key] = pending

        return question, letters, self._strip_common_prefix(options)

    @staticmethod
    def _strip_common_prefix(options: list[str]) -> list[str]:
        """If every option shares a long common prefix (e.g. four different
        "Monster Hunter: X" titles), truncating each to the same width for the
        LoRa payload makes them all look identical and the question unanswerable.
        Strip the shared part so the truncation budget goes to what differs."""
        if len(options) < 2:
            return options
        prefix = options[0]
        for o in options[1:]:
            i = 0
            while i < len(prefix) and i < len(o) and prefix[i] == o[i]:
                i += 1
            prefix = prefix[:i]
        if len(prefix) < 4:
            return options
        stripped = [o[len(prefix):].strip() for o in options]
        if any(not s for s in stripped):  # one option WAS exactly the prefix
            return options
        return stripped

    async def _send_trivia_question(self, msg: NormalizedMessage, question: str,
                                    letters: str, options: list[str]) -> None:
        # The category prefix used to be shown here ("[Entertainment:] ...") but
        # that ate budget that should go to the question itself, so it's dropped
        # entirely — the category is still tracked internally (_trivia_category)
        # for "sticks across the round", just not displayed per question.
        limit = self._max_len
        suffix = " (reply A-D)"
        n = len(options)
        # The options line no longer has to share a message with the question —
        # give it the FULL budget on its own so per-option width is generous
        # (this is what actually keeps _strip_common_prefix's output legible;
        # cramming both into one message is what caused truncated/collided
        # options in the first place).
        overhead = len(suffix) + n * 3 + (n - 1)  # "A)" per option + join spaces
        opt_width = max((limit - overhead) // n, 10)
        opts_line = " ".join(f"{l}){o[:opt_width]}" for l, o in zip(letters, options)) + suffix

        combined = f"{question} {opts_line}"
        if len(combined) <= limit:
            await self._send(msg, combined)
            return
        # Doesn't fit in one message — split rather than truncating the
        # question into the options' budget (or vice versa).
        await self._send(msg, question[:limit])
        await asyncio.sleep(1.0)
        await self._send(msg, opts_line[:limit])

    async def _dispatch_trivia_answer(self, msg: NormalizedMessage, letter: str,
                                      channel_key: tuple | None = None) -> None:
        is_dm = channel_key is None
        ctx_key = ("dm", msg.from_id) if is_dm else channel_key
        if channel_key is not None:
            pending = self._trivia_channel_pending.get(channel_key)
        else:
            pending = self._trivia_pending.get(msg.from_id)
        if pending is None:
            return
        if channel_key is not None:
            del self._trivia_channel_pending[channel_key]
        else:
            del self._trivia_pending[msg.from_id]

        if time.monotonic() > pending["expires"]:
            await self._send(msg, "trivia: that question expired. Send 'trivia' for a new one.")
            return
        correct = letter == pending["letter"]
        rec = await self._update_trivia_score(msg.from_id, msg.from_display or msg.from_id, correct)
        # On channels, several people may be racing to answer — name who got it
        # so the result isn't ambiguous to everyone else reading the channel.
        who = f"{(msg.from_display or msg.from_id or '?')[:16]}: " if channel_key is not None else ""
        if correct:
            reply = f"{who}Correct! {pending['answer'][:50]} — score {rec['correct']}/{rec['asked']}"
        else:
            reply = f"{who}Nope, it was {pending['letter']}) {pending['answer'][:50]} — score {rec['correct']}/{rec['asked']}"
        await self._send(msg, reply)

        # Auto-continue the round so players don't have to retype "trivia" for
        # every question — stops only if this context said "trivia stop", or
        # (implicitly) if nobody answers the next question before it expires,
        # since nothing re-triggers this without an actual answer coming in.
        if ctx_key not in self._trivia_paused:
            # A short gap before the next question — sending two messages back
            # to back with zero delay risks the radio still transmitting/
            # processing the first one when the second send lands, dropping or
            # garbling one of them (reported: messages missing even at close
            # range, which points at a send-timing issue rather than RF range).
            # Matches the delay _send_multi already uses between split chunks.
            await asyncio.sleep(1.5)
            await self._send_next_trivia_question(msg, is_dm, ctx_key)

    async def _load_trivia_scores(self) -> dict:
        if not self._db:
            return {}
        try:
            raw = await self._db.get_kv(_TRIVIA_SCORES_KV_KEY)
            return json.loads(raw) if raw else {}
        except Exception as exc:
            log.debug("MeshBot: trivia score load error: %s", exc)
            return {}

    async def _update_trivia_score(self, from_id: str, display: str, correct: bool) -> dict:
        scores = await self._load_trivia_scores()
        rec = scores.get(from_id, {"correct": 0, "asked": 0, "name": display})
        rec["asked"] += 1
        rec["name"] = display or rec.get("name") or from_id
        if correct:
            rec["correct"] += 1
        scores[from_id] = rec
        if self._db:
            try:
                await self._db.set_kv(_TRIVIA_SCORES_KV_KEY, json.dumps(scores))
            except Exception as exc:
                log.debug("MeshBot: trivia score save error: %s", exc)
        return rec

    async def _cmd_trivia_score(self, msg: NormalizedMessage) -> str:
        scores = await self._load_trivia_scores()
        rec = scores.get(msg.from_id)
        if not rec or not rec["asked"]:
            return "trivia: no questions answered yet. Send 'trivia' to play."
        pct = round(100 * rec["correct"] / rec["asked"])
        return f"Your trivia score: {rec['correct']}/{rec['asked']} ({pct}%)"

    async def _cmd_trivia_leaderboard(self) -> str:
        scores = await self._load_trivia_scores()
        if not scores:
            return "leaderboard: no scores yet. Send 'trivia' to play."
        ranked = sorted(scores.values(), key=lambda r: (-r["correct"], r["asked"]))
        parts = [f"{r.get('name', '?')[:12]} {r['correct']}/{r['asked']}" for r in ranked[:5]]
        return "Trivia leaderboard: " + " | ".join(parts)

    # ── MUD / text adventure — game picker + two engines ──────────────────────

    def _mud_key(self, msg: NormalizedMessage):
        """Session key: per-sender for DMs; per-(adapter, channel) for channel play.
        Polled MeshCore channel messages carry no sender identity, so a channel
        can't have per-player sessions — instead the whole channel shares ONE
        game and anyone's message drives it (same keying as channel trivia)."""
        if (msg.source_channel or "").lower() == "dm":
            return msg.from_id
        return self._trivia_channel_key(msg)

    def _cmd_mud_start(self, msg: NormalizedMessage, mention_required: bool = False) -> str:
        is_dm = (msg.source_channel or "").lower() == "dm"
        if not is_dm and mention_required:
            # A channel session claims EVERY subsequent channel message as game
            # input — fine on a dedicated bot channel, but it would swallow
            # ordinary conversation on a general channel (the same reasoning
            # that put that channel on the @mention list in the first place).
            return "mud: DM the bot to play, or use the bot channel — a shared game here would swallow normal chat"
        key = self._mud_key(msg)
        self._mud_sessions[key] = {"game": "choose", "key": key, "last_active": time.monotonic()}
        opts = " | ".join(f"{n}) {name}" for n, _, name in _MUD_GAME_CHOICES)
        shared = "" if is_dm else " Shared — anyone can send moves."
        return f"Pick a game (reply 1-3): {opts}.{shared}"

    async def _send_multi(self, msg: NormalizedMessage, text: str, max_chunks: int = 3) -> None:
        """Send long text as several sequential replies instead of one hard-truncated
        one. Used for Adventure's paragraph-length room descriptions, which regularly
        exceed a single LoRa payload. Splits on word boundaries; a short pause between
        chunks lets each transmission clear before the next goes out."""
        text = text.strip()
        if not text:
            return
        limit = self._max_len
        chunks: list[str] = []
        remaining = text
        while remaining and len(chunks) < max_chunks:
            if len(remaining) <= limit:
                chunks.append(remaining)
                remaining = ""
                break
            cut = remaining.rfind(" ", 0, limit)
            if cut <= 0:
                cut = limit
            chunks.append(remaining[:cut].rstrip())
            remaining = remaining[cut:].lstrip()
        if remaining:
            marker = " [...]"
            last = chunks[-1].rstrip()
            if len(last) + len(marker) > limit:
                last = last[: limit - len(marker)]
            chunks[-1] = last + marker
        for i, chunk in enumerate(chunks):
            await self._send(msg, chunk)
            if i < len(chunks) - 1:
                await asyncio.sleep(1.5)

    async def _dispatch_mud(self, msg: NormalizedMessage) -> None:
        sess = self._mud_sessions.get(self._mud_key(msg))
        if sess is None:
            return
        sess["last_active"] = time.monotonic()
        text = msg.body.strip()

        if sess.get("game") == "choose":
            await self._mud_pick_game(msg, sess, text)
            return

        if text.lower() in ("quit", "exit", "bye"):
            self._mud_sessions.pop(sess["key"], None)
            await self._send(msg, "Left the game. Send 'mud' to play again.")
            return

        if sess["game"] == "adventure":
            await self._dispatch_adventure(msg, sess, text)
        elif sess["game"] == "derelict":
            await self._dispatch_room_game(msg, sess, text, _DERELICT_ROOMS, extra_verb=self._derelict_extra_verb)
        else:
            await self._dispatch_room_game(msg, sess, text, _MUD_ROOMS)

    async def _mud_pick_game(self, msg: NormalizedMessage, sess: dict, text: str) -> None:
        skey = sess["key"]
        pick = text.strip().lower()
        match = next((key for n, key, _ in _MUD_GAME_CHOICES if pick in (n, key)), None)
        if match is None:
            opts = " | ".join(f"{n}) {name}" for n, _, name in _MUD_GAME_CHOICES)
            await self._send(msg, f"Not a valid choice. {opts}")
            return
        if match == "tinymud":
            self._mud_sessions[skey] = {"game": "tinymud", "key": skey, "room": _MUD_START, "inv": [],
                                        "last_active": time.monotonic()}
            await self._send(msg, "Welcome to TinyMUD-lite! " + self._room_desc(_MUD_ROOMS, self._mud_sessions[skey]))
            return
        if match == "derelict":
            self._mud_sessions[skey] = {"game": "derelict", "key": skey, "room": _DERELICT_START, "inv": [], "flags": {},
                                        "last_active": time.monotonic()}
            await self._send(msg, "Derelict — you wake in the cryo bay. "
                             + self._room_desc(_DERELICT_ROOMS, self._mud_sessions[skey]))
            return
        # match == "adventure"
        if not _ADVENTURE_AVAILABLE:
            self._mud_sessions.pop(skey, None)
            await self._send(msg, "adventure: not installed on this server (pip install adventure). Send 'mud' to pick again.")
            return
        game = _AdventureGame()
        _adventure_load_advent_dat(game)
        game.start()
        # Default to brief mode (short room descriptions after the first visit) —
        # a real built-in toggle (normally triggered by the player typing "brief"),
        # defaulted on here since full paragraph descriptions routinely exceed a
        # LoRa payload; see _send_multi for how we still handle the long first visit.
        game.full_description_period = 10000
        self._mud_sessions[skey] = {"game": "adventure", "key": skey, "engine": game, "last_active": time.monotonic()}
        await self._send_multi(msg, game.output)

    # ── Shared room-graph engine (TinyMUD-lite + Derelict) ─────────────────────
    # Both games are the same look/inventory/take/drop/exit mechanics over a
    # rooms dict; only Derelict adds put/use + a launch win-check, handled via
    # the optional extra_verb hook so the two don't carry duplicate copies of
    # the shared logic that would silently drift out of sync.

    @staticmethod
    def _room_desc(rooms: dict, sess: dict) -> str:
        room = rooms[sess["room"]]
        parts = [f"{room['name']}: {room['desc']}"]
        if room["items"]:
            parts.append("Here: " + ", ".join(room["items"]))
        parts.append("Exits: " + ", ".join(sorted(room["exits"])))
        return " | ".join(parts)

    @staticmethod
    def _mud_find_item(rest: str, items: list[str]) -> str | None:
        """Match "take key" against an item named "brass key" as well as an exact name."""
        if not rest:
            return None
        if rest in items:
            return rest
        return next((it for it in items if rest in it.lower()), None)

    async def _dispatch_room_game(self, msg: NormalizedMessage, sess: dict, text: str,
                                  rooms: dict, extra_verb=None) -> None:
        words = text.lower().split()
        verb = _MUD_DIRS.get(words[0], words[0]) if words else "look"
        rest = " ".join(words[1:]).strip()
        room = rooms[sess["room"]]

        if extra_verb is not None:
            handled = extra_verb(msg, sess, room, verb, rest)
            if handled is not None:
                await self._send(msg, handled)
                return

        if verb in ("look", "l", ""):
            reply = self._room_desc(rooms, sess)
        elif verb in ("inventory", "inv", "i"):
            reply = "Carrying: " + (", ".join(sess["inv"]) if sess["inv"] else "nothing")
        elif verb in ("take", "get"):
            item = self._mud_find_item(rest, room["items"])
            if item:
                room["items"].remove(item)
                sess["inv"].append(item)
                reply = f"Taken: {item}"
            else:
                reply = f"There is no {rest or 'that'} here." if rest else "Take what?"
        elif verb == "drop":
            item = self._mud_find_item(rest, sess["inv"])
            if item:
                sess["inv"].remove(item)
                room["items"].append(item)
                reply = f"Dropped: {item}"
            else:
                reply = f"You aren't carrying {rest or 'that'}." if rest else "Drop what?"
        elif verb in room["exits"]:
            needed = room.get("locks", {}).get(verb)
            if needed and needed not in sess["inv"]:
                reply = f"That way is locked. You need: {needed}."
            else:
                dest = room["exits"][verb]
                if dest == "WIN":
                    self._mud_sessions.pop(sess["key"], None)
                    reply = "You climb out into daylight, home at last. You win! Send 'mud' to play again."
                else:
                    sess["room"] = dest
                    reply = self._room_desc(rooms, sess)
        else:
            reply = "Try: look, north/south/east/west/up/down, take <item>, drop <item>, inventory, quit."

        await self._send(msg, reply)

    def _derelict_extra_verb(self, msg: NormalizedMessage, sess: dict, room: dict,
                             verb: str, rest: str) -> str | None:
        """Derelict's two verbs beyond the shared engine. Returns a reply string if
        handled, or None to fall through to the shared look/take/drop/exit logic."""
        if verb in ("put", "insert", "use"):
            item = self._mud_find_item(rest, sess["inv"])
            if sess["room"] == "reactor_room" and item == "fuel cell":
                sess["inv"].remove("fuel cell")
                sess["flags"]["powered"] = True
                return "The reactor hums to life — main power restored shipwide."
            elif item:
                return "Nothing happens."
            else:
                return f"You aren't carrying {rest or 'that'}." if rest else "Use what?"
        if verb in ("launch", "eject") and sess["room"] == "airlock":
            if sess["flags"].get("powered"):
                self._mud_sessions.pop(sess["key"], None)
                return "Main power holds the outer door steady as you launch the escape pod. You win! Send 'mud' to play again."
            return "The airlock panel is dead — restore main power first."
        return None

    # ── Colossal Cave Adventure (the real 1977 game, via the `adventure` package) ─

    async def _dispatch_adventure(self, msg: NormalizedMessage, sess: dict, text: str) -> None:
        game = sess["engine"]
        words = re.findall(r"\w+", text.lower())
        if not words:
            await self._send(msg, "Say something (e.g. 'look', 'north', 'take lamp').")
            return
        output = game.do_command(words)
        if game.is_finished:
            self._mud_sessions.pop(sess["key"], None)
            output = (output + "\nGAME OVER. Send 'mud' to play again.").strip()
        await self._send_multi(msg, output)

    # ── SKYWARN spotter reports ────────────────────────────────────────────────
    # NOTE on where these go: this logs the report into ECH's own local database
    # (skywarn_reports table) for the operator's own situational awareness — it
    # is NOT auto-submitted to the National Weather Service. There's no
    # documented public API for that: the real channels are (1) ham radio
    # SKYWARN nets (voice reports to net control), and (2) iNWS's "Submit a
    # Storm Report" form (inws.ncep.noaa.gov/report), which is a human-facing,
    # partner-oriented web form, not a public REST API. Deliberately not
    # auto-submitting: these reports feed real warning decisions, and every
    # trained spotter is vetted so their reports can be trusted at that level —
    # an unreviewed auto-relay from any mesh sender shouldn't be indistinguishable
    # from that. Instead: 'skywarn net' formats the last report as standard net
    # check-in phrasing to read aloud, 'skywarn inws'/'skywarn nws' formats it
    # using standard NWS Local Storm Report categories for a human to copy into
    # the iNWS form (or any equivalent) themselves, and 'skywarn winlink' emails
    # it via ECH's own Pat Winlink adapter to an operator-configured destination
    # (mesh_bot.skywarn_winlink_to) — e.g. a served agency or EOC — never a
    # hardcoded NWS address, since no verified official inbox exists.

    async def _cmd_skywarn(self, msg: NormalizedMessage) -> str:
        # Re-derive the full text ourselves: the shared _CMD_RE args group caps at
        # ~40 chars (fine for callsigns/zips elsewhere), far too short for a whole
        # spotter report.
        m = re.search(r'skywarn\s+(.+)', msg.body, re.IGNORECASE)
        args = (m.group(1).strip() if m else "")
        sub = args.lower()

        if sub in ("last", "check", "status"):
            # Independent proof-of-receipt: re-reads back what's actually stored,
            # rather than just trusting the confirmation reply's own delivery.
            return await self._cmd_skywarn_last(msg)
        if sub == "net":
            # Sends directly (may be more than one LoRa message) rather than
            # returning a string through the generic single-message send — a
            # hard-truncated report is worse than useless when someone's about
            # to read it aloud or copy it somewhere.
            await self._send_skywarn_format(msg, "net")
            return ""
        if sub in ("inws", "nws"):
            await self._send_skywarn_format(msg, "inws")
            return ""
        if sub == "winlink":
            return await self._cmd_skywarn_winlink(msg)

        if args:
            # Fast path: "skywarn W1ABC <freeform report>" — works on channels too,
            # since it's a single self-contained message with no session to track.
            parts = args.split(None, 1)
            callsign = parts[0].upper()
            report_text = parts[1] if len(parts) > 1 else ""
            if not report_text:
                return "skywarn: usage: skywarn <callsign> <report>  or just 'skywarn' for a guided form"
            await self._save_skywarn(msg, callsign, report_text)
            return self._skywarn_confirmation(callsign, "", report_text)

        # No args: guided form. DM-only — needs reliable per-sender session state,
        # which polled MeshCore channel messages can't provide (no sender pubkey).
        if (msg.source_channel or "").lower() != "dm":
            return "skywarn: DM the bot for the guided form, or send 'skywarn <callsign> <report>' here"
        self._skywarn_sessions[msg.from_id] = {"step": 0, "answers": {}}
        return f"Skywarn report — {_SKYWARN_FIELDS[0][1]} (or 'cancel')"

    async def _cmd_skywarn_last(self, msg: NormalizedMessage) -> str:
        if not self._db:
            return "skywarn: no database configured"
        try:
            reports = await self._db.get_skywarn_reports(limit=1, from_id=msg.from_id or "")
        except Exception as exc:
            return f"skywarn: lookup failed ({type(exc).__name__})"
        if not reports:
            return "skywarn: no report on file for you yet."
        mine = reports[0]
        spotter_id = mine.get("spotter_id")
        id_suffix = f" (Spotter #{spotter_id})" if spotter_id else ""
        return f"On file ({mine['timestamp'][:16]}): {mine['callsign']}{id_suffix} — {mine['raw_text'][:100]}"

    async def _cmd_skywarn_format(self, msg: NormalizedMessage, fmt: str) -> str:
        """Look up the caller's last stored report and format it for a human to
        actually relay it — 'net' for ham radio net check-in phrasing, 'inws' for
        a structured summary to copy into iNWS or an equivalent form manually."""
        if not self._db:
            return "skywarn: no database configured"
        try:
            reports = await self._db.get_skywarn_reports(limit=1, from_id=msg.from_id or "")
        except Exception as exc:
            return f"skywarn: lookup failed ({type(exc).__name__})"
        if not reports:
            return "skywarn: no report on file for you yet. Send 'skywarn' to file one first."
        r = reports[0]
        return self._format_skywarn_net(r) if fmt == "net" else self._format_skywarn_inws(r)

    async def _send_skywarn_format(self, msg: NormalizedMessage, fmt: str) -> None:
        text = await self._cmd_skywarn_format(msg, fmt)
        # The inws format is multi-line (real newlines matter for the Winlink
        # email version); over LoRa, join with " | " to match this file's usual
        # single-line reply convention, and let _send_multi split across
        # several messages if it's still too long rather than truncating.
        await self._send_multi(msg, text.replace("\n", " | "), max_chunks=5)

    @staticmethod
    def _has_value(v) -> bool:
        return bool(v) and str(v).strip().lower() not in ("none", "n/a", "")

    @classmethod
    def _format_skywarn_net(cls, r: dict) -> str:
        """Standard ham radio SKYWARN net check-in phrasing, meant to be read
        aloud to net control — this is the primary real-world path most spotter
        reports actually take."""
        callsign = r.get("callsign") or "?"
        spotter = f", spotter {r['spotter_id']}" if cls._has_value(r.get("spotter_id")) else ""
        loc = r.get("location") if cls._has_value(r.get("location")) else "location not given"
        event = f" reporting {r['event_type']}" if cls._has_value(r.get("event_type")) else ""
        parts = [f"Spotter report, this is {callsign}{spotter},{event} near {loc}."]
        if cls._has_value(r.get("temp_f")):
            parts.append(f"Temperature {r['temp_f']} degrees.")
        if cls._has_value(r.get("wind_mph")):
            dir_part = f" from the {r['wind_dir']}" if cls._has_value(r.get("wind_dir")) else ""
            parts.append(f"Wind {r['wind_mph']} miles per hour{dir_part}.")
        if cls._has_value(r.get("hail_size")):
            parts.append(f"Hail size {r['hail_size']} inches.")
        if cls._has_value(r.get("precip")):
            parts.append(f"Precipitation: {r['precip']}.")
        if cls._has_value(r.get("notes")):
            parts.append(f"{r['notes']}.")
        parts.append("Over.")
        return " ".join(parts)

    @classmethod
    def _format_skywarn_inws(cls, r: dict) -> str:
        """Standard NWS Local Storm Report categories (Report Type / Date-Time /
        Location / Magnitude / Source / Remarks) for manual entry into iNWS or
        an equivalent. NOT a verified match to inws.ncep.noaa.gov's exact live
        field labels — that form renders its fields via JavaScript, so a static
        fetch only sees the page shell. Says so explicitly rather than implying
        a field-for-field match that was never actually confirmed."""
        callsign = r.get("callsign") or "?"
        spotter = f" (Spotter #{r['spotter_id']})" if cls._has_value(r.get("spotter_id")) else ""
        loc = r.get("location") if cls._has_value(r.get("location")) else "unknown"
        event_type = r["event_type"] if cls._has_value(r.get("event_type")) else "Spotter Report"
        wind_dir = f" {r['wind_dir']}" if cls._has_value(r.get("wind_dir")) else ""
        hail = f"; hail {r['hail_size']}in" if cls._has_value(r.get("hail_size")) else ""
        precip = r["precip"] if cls._has_value(r.get("precip")) else "none"
        notes = r["notes"] if cls._has_value(r.get("notes")) else "none"
        lines = [
            "STORM REPORT (standard NWS LSR categories — not a verified field match to the live iNWS form; adapt as needed)",
            f"Report Type: {event_type}",
            f"Date/Time: {r.get('timestamp', '?')} UTC",
            f"Location: {loc}",
            f"Magnitude: temp {r.get('temp_f', '?')}F; wind {r.get('wind_mph', '?')}mph{wind_dir}{hail}; precip {precip}",
            f"Source: Trained Spotter — {callsign}{spotter}",
            f"Remarks: {notes}",
        ]
        return "\n".join(lines)

    async def _cmd_skywarn_winlink(self, msg: NormalizedMessage) -> str:
        """Email the caller's last report via ECH's own Pat Winlink adapter to an
        operator-configured destination — never a hardcoded NWS address, since no
        verified official Winlink inbox for storm reports exists. This is just
        outbound radio email to whoever the operator has actually arranged to
        receive it (a served agency, EOC, or NWS liaison), keeping a human at
        the other end deciding whether/how to relay it further."""
        to_addr = str((self._config.get("mesh_bot") or {}).get("skywarn_winlink_to", "")).strip()
        if not to_addr:
            return "skywarn winlink: not configured — set mesh_bot.skywarn_winlink_to (email or Winlink callsign) in config.yaml"
        if not self._db:
            return "skywarn: no database configured"
        try:
            reports = await self._db.get_skywarn_reports(limit=1, from_id=msg.from_id or "")
        except Exception as exc:
            return f"skywarn: lookup failed ({type(exc).__name__})"
        if not reports:
            return "skywarn: no report on file for you yet. Send 'skywarn' to file one first."
        r = reports[0]
        if not self._router:
            return "skywarn winlink: router not available"
        winlink_adapter = next(
            (a.name for a in self._router._adapters.values() if "winlink" in a.name.lower()), None
        )
        if not winlink_adapter:
            return "skywarn winlink: no Winlink adapter connected"
        loc = r.get("location") if self._has_value(r.get("location")) else "location not given"
        subject = f"SKYWARN Report: {r.get('callsign', '?')} - {loc}"
        body = subject + "\n\n" + self._format_skywarn_inws(r)
        result = await self._router.send(body=body, adapter_names=[winlink_adapter], to_id=to_addr)
        if result.get(winlink_adapter):
            return f"Skywarn report sent via Winlink to {to_addr}."
        return "skywarn winlink: send failed — check the Winlink adapter is connected to the CMS"

    async def _dispatch_skywarn(self, msg: NormalizedMessage) -> None:
        sess = self._skywarn_sessions.get(msg.from_id)
        if sess is None:
            return
        text = msg.body.strip()
        if text.lower() in ("cancel", "quit", "exit"):
            del self._skywarn_sessions[msg.from_id]
            await self._send(msg, "Skywarn report cancelled.")
            return

        field_key, _ = _SKYWARN_FIELDS[sess["step"]]
        sess["answers"][field_key] = text
        sess["step"] += 1

        if sess["step"] < len(_SKYWARN_FIELDS):
            _, prompt = _SKYWARN_FIELDS[sess["step"]]
            await self._send(msg, prompt)
            return

        answers = sess["answers"]
        del self._skywarn_sessions[msg.from_id]
        callsign = answers.get("callsign", "?").upper()
        spotter_id = answers.get("spotter_id", "none")
        location = answers.get("location", "")
        event_type = answers.get("event_type", "")
        hail_size = answers.get("hail_size", "")
        precip = answers.get("precip", "none")
        notes = answers.get("notes", "none")
        loc_prefix = f"{location}: " if self._has_value(location) else ""
        event_prefix = f"{event_type} — " if self._has_value(event_type) else ""
        report_text = f"{event_prefix}{loc_prefix}{answers.get('temp_f', '?')}F, wind {answers.get('wind_mph', '?')}mph {answers.get('wind_dir', '?')}"
        if self._has_value(hail_size):
            report_text += f", hail {hail_size}in"
        report_text += f", precip {precip}"
        if notes.lower() not in ("none", "n/a", ""):
            report_text += f", {notes}"
        await self._save_skywarn(msg, callsign, report_text, answers)
        id_suffix = f" (Spotter #{spotter_id})" if spotter_id.lower() not in ("none", "n/a", "") else ""
        await self._send(msg, self._skywarn_confirmation(callsign, id_suffix, report_text))

    def _skywarn_confirmation(self, callsign: str, id_suffix: str, report_text: str) -> str:
        # The disclaimer is the whole point of this method: without it, a user
        # has no way to know from the bot's own reply that this only logged
        # locally and went nowhere near NWS — sizes the report-text truncation
        # dynamically (same technique as trivia's reply hint) so the disclaimer
        # always survives instead of being the part that gets cut off.
        disclaimer = " [local log only, not sent to NWS]"
        prefix = f"Skywarn report logged: {callsign}{id_suffix} — "
        avail = max(self._max_len - len(prefix) - len(disclaimer), 15)
        return f"{prefix}{report_text[:avail]}{disclaimer}"[:self._max_len]

    async def _save_skywarn(self, msg: NormalizedMessage, callsign: str, raw_text: str,
                            fields: dict | None = None) -> None:
        if not self._db:
            return
        fields = fields or {}
        try:
            await self._db.save_skywarn_report(
                from_id=msg.from_id or "", from_display=msg.from_display or "",
                callsign=callsign, adapter=msg.source_adapter or "",
                source_channel=msg.source_channel or "", raw_text=raw_text,
                spotter_id=fields.get("spotter_id"), location=fields.get("location"),
                event_type=fields.get("event_type"),
                temp_f=fields.get("temp_f"), wind_mph=fields.get("wind_mph"),
                wind_dir=fields.get("wind_dir"), hail_size=fields.get("hail_size"),
                precip=fields.get("precip"), notes=fields.get("notes"),
            )
        except Exception as exc:
            log.warning("MeshBot: skywarn save error: %s", exc)
            return
        # Notify anyone with the SKYWARN page open, live — mirrors how anomaly
        # findings and bot_activity already push over the same /ws connection.
        if self._router:
            try:
                saved = await self._db.get_skywarn_reports(limit=1, from_id=msg.from_id or "")
                if saved:
                    await self._router.broadcast_ws_event("skywarn_report", saved[0])
            except Exception as exc:
                log.debug("MeshBot: skywarn WS broadcast error: %s", exc)

    # ── Dad jokes ────────────────────────────────────────────────────────────

    async def _cmd_dad(self) -> str:
        client = self._client
        assert client is not None
        try:
            r = await client.get(
                DADJOKE_URL,
                headers={"Accept": "application/json", "User-Agent": USER_AGENT},
                timeout=8.0,
            )
            r.raise_for_status()
            return r.json().get("joke", "I couldn't think of a joke.")[:200]
        except Exception as exc:
            log.warning("MeshBot/dadjoke error: %s", exc)
            return f"dad: fetch failed ({type(exc).__name__})"

    # ── NWS Alerts ───────────────────────────────────────────────────────────

    async def _cmd_alerts(self) -> str:
        lat, lon = self._resolve_coords()
        client = self._client
        assert client is not None
        wx_cfg = self._config.get("weather_service", {})
        # Prefer the live weather service's area (updated by Settings UI) over the static config value
        _wx_svc = getattr(getattr(self, "_state", None), "_wx_service", None)
        area = (getattr(_wx_svc, "_area", None) or wx_cfg.get("nws_area", "")).strip().upper()
        try:
            features = None
            # Try point query first if coords are available (no status= filter, NWS defaults to active)
            if lat is not None and lon is not None:
                r = await client.get(
                    f"{NWS_BASE}/alerts/active",
                    params={"point": f"{lat:.4f},{lon:.4f}"},
                    timeout=10.0,
                )
                if r.status_code == 200:
                    features = r.json().get("features", [])
                # Any non-200 → fall through to area
            # Fall back to area/state code
            if features is None:
                if not area:
                    return "alerts: no location — set nws_area (e.g. ME) or base location in Settings"
                r2 = await client.get(
                    f"{NWS_BASE}/alerts/active",
                    params={"area": area},
                    timeout=10.0,
                )
                if r2.status_code >= 400:
                    try:
                        detail = r2.json().get("detail", "")
                    except Exception:
                        detail = ""
                    return f"alerts: NWS HTTP {r2.status_code}" + (f" — {detail[:60]}" if detail else "")
                features = r2.json().get("features", [])

            if not features:
                return "ALERTS: none active"
            parts = []
            for f in features[:3]:
                props    = f.get("properties", {})
                event    = props.get("event", "Alert")
                severity = props.get("severity", "")[:3].upper()
                headline = (props.get("headline") or props.get("description", "")).split("\n")[0][:60]
                parts.append(f"[{severity}] {event}: {headline}")
            total  = len(features)
            header = f"ALERTS({total}): " if total > 1 else "ALERT: "
            return header + " | ".join(parts)
        except Exception as exc:
            log.warning("MeshBot/alerts error: %s", exc)
            return f"alerts: fetch failed ({type(exc).__name__})"

    # ── Tides ────────────────────────────────────────────────────────────────

    async def _cmd_tide(self) -> str:
        station = self._tide_station
        if not station:
            return "tide: no tide_station configured (set mesh_bot.tide_station in config.yaml)"
        client = self._client
        assert client is not None
        import datetime
        now = datetime.datetime.now()
        begin = now.strftime("%Y%m%d")
        try:
            url = (
                f"https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"
                f"?product=predictions&application=ECH&begin_date={begin}&range=24"
                f"&station={station}&time_zone=lst_ldt&interval=hilo"
                f"&units=english&datum=MLLW&format=json"
            )
            r = await client.get(url)
            data = r.json()
        except Exception as exc:
            return f"tide: fetch failed ({type(exc).__name__})"
        if "error" in data:
            return f"tide: {data['error'].get('message', 'unknown error')}"
        preds = data.get("predictions", [])
        if not preds:
            return "tide: no predictions returned"
        parts = []
        for p in preds:
            raw_t = p.get("t", "")   # "YYYY-MM-DD HH:MM"
            v = float(p.get("v", 0))
            typ = "H" if p.get("type", "").upper() == "H" else "L"
            # Convert "HH:MM" to 12h AM/PM
            try:
                import datetime as _dt
                hh, mm = int(raw_t[-5:-3]), int(raw_t[-2:])
                ampm = "am" if hh < 12 else "pm"
                hr12 = hh % 12 or 12
                t_str = f"{hr12}:{mm:02d}{ampm}"
            except Exception:
                t_str = raw_t[-5:]
            parts.append(f"{typ} {t_str} {v:.1f}ft")
        station_name = data.get("metadata", {}).get("name", station)
        return f"TIDES {station_name}: " + " | ".join(parts)

    # ── METAR ────────────────────────────────────────────────────────────────

    async def _cmd_metar(self, args: str) -> str:
        query = args.strip()
        if not query:
            # Use base location
            lat, lon = self._resolve_coords()
            if lat is None:
                return "Usage: metar <ICAO or city>  e.g. metar KPWM"
            return await self._fetch_metar_nearby(lat, lon)
        client = self._client
        assert client is not None
        # 4-letter all-alpha ICAO code?
        m = _ICAO_RE.search(query)
        if m and re.fullmatch(r'[A-Za-z]{4}', query.strip()):
            return await self._fetch_metar_icao(m.group(1).upper())
        # Zip code?
        zm = _ZIP_RE.search(query)
        if zm:
            geo = await client.get(NOMINATIM_URL,
                params={"postalcode": zm.group(1), "countrycodes": "us", "format": "json", "limit": "1"})
            hits = geo.json()
        else:
            # Free-text place name
            geo = await client.get(NOMINATIM_URL,
                params={"q": query, "format": "json", "limit": "1"})
            hits = geo.json()
        if not hits:
            return f"metar: location '{query}' not found"
        lat = float(hits[0]["lat"])
        lon = float(hits[0]["lon"])
        return await self._fetch_metar_nearby(lat, lon)

    async def _fetch_metar_icao(self, icao: str) -> str:
        client = self._client
        assert client is not None
        try:
            r = await client.get(METAR_URL, params={"ids": icao, "format": "json", "taf": "false"}, timeout=8.0)
            r.raise_for_status()
            data = r.json()
            if not data:
                return f"METAR {icao}: no recent report"
            return self._format_metar(data[0])
        except Exception as exc:
            log.warning("MeshBot/metar error: %s", exc)
            return f"metar: fetch failed ({type(exc).__name__})"

    async def _fetch_metar_nearby(self, lat: float, lon: float) -> str:
        client = self._client
        assert client is not None
        try:
            # bbox: 1.5 deg radius (~100nm)
            r = await client.get(METAR_URL, params={
                "bbox": f"{lat-1.5},{lon-2.5},{lat+1.5},{lon+2.5}",
                "format": "json", "taf": "false",
            }, timeout=8.0)
            r.raise_for_status()
            data = r.json()
            if not data:
                return "METAR: no stations found nearby"
            # Sort by distance to query point
            def _d(obs):
                try:
                    return _haversine_nm(lat, lon, float(obs["lat"]), float(obs["lon"]))
                except Exception:
                    return 9999.0
            data.sort(key=_d)
            return self._format_metar(data[0])
        except Exception as exc:
            log.warning("MeshBot/metar nearby error: %s", exc)
            return f"metar: fetch failed ({type(exc).__name__})"

    def _format_metar(self, obs: dict) -> str:
        raw = obs.get("rawOb") or obs.get("raw_text", "")
        if raw:
            return f"METAR {raw[:180]}"
        icao = obs.get("icaoId") or obs.get("stationId", "?")
        tmp  = obs.get("temp")
        wspd = obs.get("wspd")
        wdir = obs.get("wdir")
        vis  = obs.get("visib")
        sky  = obs.get("skyCondition") or obs.get("clouds", "")
        parts = [icao]
        if tmp  is not None: parts.append(f"{tmp}C")
        if wdir is not None and wspd is not None: parts.append(f"{wdir:03.0f}/{wspd}kt")
        if vis  is not None: parts.append(f"vis {vis}sm")
        if sky: parts.append(str(sky)[:20])
        return "METAR " + " ".join(parts)

    # ── Sunrise / Sunset ─────────────────────────────────────────────────────

    async def _cmd_sun(self) -> str:
        lat, lon = self._resolve_coords()
        if lat is None:
            return "sun: set base location in Settings"
        try:
            from datetime import date
            today = date.today().isoformat()
            client = self._client
            assert client is not None
            r = await client.get(
                "https://api.sunrise-sunset.org/json",
                params={"lat": lat, "lng": lon, "formatted": "0", "date": today},
                timeout=8.0,
            )
            r.raise_for_status()
            res = r.json().get("results", {})
            # Convert UTC → server local time; server should be co-located with the station
            local_tz = datetime.now().astimezone().tzinfo
            def _fmt(iso: str) -> str:
                try:
                    from datetime import timezone as _tz
                    dt_utc = datetime.fromisoformat(iso.replace("Z", "+00:00"))
                    dt_loc = dt_utc.astimezone(local_tz)
                    return dt_loc.strftime("%-I:%M%p").lower()
                except Exception:
                    return iso[:5]
            rise   = _fmt(res.get("sunrise", ""))
            sset   = _fmt(res.get("sunset", ""))
            solar  = res.get("day_length", 0)
            h, rem = divmod(int(solar), 3600)
            m2     = rem // 60
            tz_abbr = datetime.now().astimezone().strftime("%Z")
            return f"Sun {today}: rise {rise} set {sset} {tz_abbr} ({h}h{m2:02d}m daylight)"
        except Exception as exc:
            log.warning("MeshBot/sun error: %s", exc)
            return f"sun: fetch failed ({type(exc).__name__})"

    # ── Active mesh nodes ────────────────────────────────────────────────────

    async def _cmd_nodes(self, args: str = "") -> str:
        if not self._router:
            return "nodes: router not available"
        from datetime import datetime, timezone
        now_ts = datetime.now(timezone.utc).timestamp()
        stale  = 3600.0

        # Optional adapter-type filter keyword: "nodes meshcore", "nodes aprs", etc.
        filt = args.strip().lower()

        # Collect (node, adapter_name) pairs
        collected: list[tuple] = []   # (node, adapter_name)
        for adapter in self._router._adapters.values():
            if not getattr(adapter, "_connected", False):
                continue
            aname = adapter.name.lower()
            try:
                nodes = await adapter.nodes()
                for n in nodes:
                    nid = n.node_id or ""
                    # Always skip ADS-B and AIS position-only nodes
                    if nid.startswith("icao:") or nid.startswith("mmsi:"):
                        continue
                    if not (n.last_heard and now_ts - n.last_heard.timestamp() <= stale):
                        continue
                    collected.append((n, aname))
            except Exception:
                pass

        if not collected:
            return "nodes: none heard in last hour"

        # Apply filter or default behaviour
        if filt:
            # User asked for a specific type — match against adapter name
            collected = [(n, a) for n, a in collected if filt in a]
            if not collected:
                return f"nodes: none matching '{filt}' heard in last hour"
        else:
            # Default: exclude APRS (floods with internet digipeaters)
            non_aprs = [(n, a) for n, a in collected if "aprs" not in a]
            if non_aprs:
                collected = non_aprs
            # else all nodes are APRS — show them anyway

        # Deduplicate by node_id, keep first (most recent due to sort)
        collected.sort(key=lambda x: x[0].last_heard.timestamp() if x[0].last_heard else 0, reverse=True)
        seen_ids: set[str] = set()
        unique = []
        for n, a in collected:
            if n.node_id not in seen_ids:
                seen_ids.add(n.node_id)
                unique.append((n, a))

        parts = []
        for n, _ in unique[:8]:
            age_m = int((now_ts - n.last_heard.timestamp()) / 60) if n.last_heard else 0
            label = (n.display_name or n.node_id or "?")[:12]
            parts.append(f"{label}({age_m}m)")

        label_str = f" [{filt}]" if filt else ""
        return f"Nodes{label_str}({len(unique)}): " + " ".join(parts)

    # ── APRS position lookup ──────────────────────────────────────────────────

    async def _cmd_aprs(self, args: str) -> str:
        call = args.strip().upper().split()[0] if args.strip() else ""
        if not call:
            return "Usage: aprs <callsign>  e.g. aprs W1ABC"

        # 1. Check our own connected APRS adapter nodes first (no external API needed)
        if self._router:
            for adapter in self._router._adapters.values():
                aname = adapter.name.lower()
                if "aprs" not in aname:
                    continue
                if not getattr(adapter, "_connected", False):
                    continue
                try:
                    nodes = await adapter.nodes()
                    for n in nodes:
                        nid = (n.node_id or "").upper()
                        # APRS node IDs are typically the callsign or callsign-SSID
                        if nid == call or nid.startswith(call + "-") or nid.startswith(call + ">"):
                            lat_s = f"{n.lat:.4f}" if n.lat is not None else "?"
                            lon_s = f"{n.lon:.4f}" if n.lon is not None else "?"
                            age_m = ""
                            if n.last_heard:
                                from datetime import datetime, timezone
                                age_s = int((datetime.now(timezone.utc) - n.last_heard).total_seconds())
                                age_m = f" ({age_s//60}m ago)"
                            comment = (n.meta or {}).get("comment", "")
                            return f"APRS {nid}: {lat_s},{lon_s}{age_m}{' — ' + comment[:40] if comment else ''}"
                except Exception:
                    pass

        # 2. Fall back to aprs.fi API if a key is configured
        aprs_key = (self._config.get("mesh_bot") or {}).get("aprs_fi_key", "")
        if not aprs_key:
            return f"APRS {call}: not heard locally. Add aprs_fi_key to mesh_bot config for remote lookup."
        client = self._client
        assert client is not None
        try:
            r = await client.get(APRS_FI_URL, params={
                "name": call, "what": "loc", "apikey": aprs_key, "format": "json",
            }, timeout=8.0)
            r.raise_for_status()
            data = r.json()
            if data.get("result") != "ok" or not data.get("entries"):
                return f"APRS {call}: not found on aprs.fi"
            e    = data["entries"][0]
            lat_s = e.get("lat", "?")
            lon_s = e.get("lng", "?")
            name  = e.get("name", call)
            lasttime = int(e.get("lasttime", 0))
            age_m = ""
            if lasttime:
                import time
                age_s = int(time.time()) - lasttime
                age_m = f" ({age_s//60}m ago)"
            comment = e.get("comment", "")
            return f"APRS {name}: {lat_s},{lon_s}{age_m}{' — ' + comment[:40] if comment else ''}"
        except Exception as exc:
            log.warning("MeshBot/aprs error: %s", exc)
            return f"aprs: lookup failed ({type(exc).__name__})"

    # ── Anomalies ─────────────────────────────────────────────────────────────

    async def _cmd_anomalies(self) -> str:
        anomaly_engine = getattr(self._router, "_anomaly_engine", None) if self._router else None
        if anomaly_engine is None:
            return "anomalies: anomaly engine not available"
        findings = anomaly_engine.active_findings()
        if not findings:
            return "ANOMALIES: none active"
        # Sort by severity (highest first), then recency
        sev_order = {"ALERT": 0, "WARN": 1, "INFO": 2}
        findings = sorted(findings,
            key=lambda f: (sev_order.get(f.severity.value.upper(), 9),
                           -(f.timestamp.timestamp() if hasattr(f.timestamp, "timestamp") else 0)))
        parts = []
        for f in findings[:4]:
            sev  = f.severity.value.upper()[:4]
            rule = f.rule.replace("_", " ")[:14]
            node = (f.node_id or f.adapter or "?")[:10]
            summ = f.summary[:40]
            parts.append(f"[{sev}] {node} {rule}: {summ}")
        total = len(findings)
        header = f"ANOMALIES({total}): " if total > 1 else "ANOMALY: "
        return header + " | ".join(parts)


# Backwards-compat alias so any code that imported WeatherBot still works
WeatherBot = MeshBot
