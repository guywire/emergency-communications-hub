"""Internet audio streams on the PBX: dial 7xx on a desk phone to listen.

Each stream becomes an Asterisk extension that answers and plays the URL with
MP3Player() (Asterisk runs mpg123, which decodes MP3 over http and https).
ECH writes the extensions to /etc/asterisk/ech_streams.conf, which
extensions.conf pulls into [from-internal] with `#include ech_streams.conf`
(an #include, not an `include =>`, so these exact extensions win over the
`_[1-9]X.` Hams-Over-IP catch-all). Only MP3 streams work: AAC/HLS do not.

config.yaml:
  pbx_streams:
    enabled: true            # default
    defaults: true           # keep the built-in list below (default)
    streams:                 # added to / overriding the defaults by ext
      - {ext: "731", name: "My club net", url: "http://example.org:8000/net.mp3"}   # 700-749
      - {ext: "705", enabled: false}      # drop a default
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path

log = logging.getLogger(__name__)

CONF_PATH = "/etc/asterisk/ech_streams.conf"
AUDIO_CONF_PATH = "/etc/asterisk/ech_audio.conf"      # #included at the END of extensions.conf (own context)


def audio_options(cfg: dict) -> dict:
    """config.yaml pbx_audio: web UI listening + browser phone (O191)."""
    a = (cfg or {}).get("pbx_audio") or {}
    return {"enabled": a.get("enabled", True) is not False,
            "port": int(a.get("audiosocket_port", 9093)),
            "allstar_node": str(a.get("allstar_node", "") or ""),
            "webphone_ext": str(a.get("webphone_ext", "111") or ""),
            "m17_ext": str(a.get("m17_ext", "760") or "")}

# Tested on the ECH server with mpg123 (2026-11-19). Ext ranges: 70x SomaFM,
# 71x synthwave, 72x trance / bass.
DEFAULT_STREAMS: list[dict] = [
    {"ext": "701", "name": "SomaFM Groove Salad (ambient downtempo)", "url": "http://ice1.somafm.com/groovesalad-128-mp3"},
    {"ext": "702", "name": "SomaFM Dub Step Beyond (dubstep)", "url": "http://ice1.somafm.com/dubstep-128-mp3"},
    {"ext": "703", "name": "SomaFM The Trip (progressive / trance)", "url": "http://ice1.somafm.com/thetrip-128-mp3"},
    {"ext": "704", "name": "SomaFM Vaporwaves", "url": "http://ice1.somafm.com/vaporwaves-128-mp3"},
    {"ext": "705", "name": "SomaFM DEF CON Radio", "url": "http://ice1.somafm.com/defcon-128-mp3"},
    {"ext": "706", "name": "SomaFM Space Station Soma", "url": "http://ice1.somafm.com/spacestation-128-mp3"},
    {"ext": "707", "name": "SomaFM Drone Zone", "url": "http://ice1.somafm.com/dronezone-128-mp3"},
    {"ext": "708", "name": "SomaFM Secret Agent", "url": "http://ice1.somafm.com/secretagent-128-mp3"},
    {"ext": "709", "name": "SomaFM Black Rock FM", "url": "http://ice1.somafm.com/brfm-128-mp3"},
    {"ext": "711", "name": "Nightride FM (synthwave)", "url": "https://stream.nightride.fm/nightride.mp3"},
    {"ext": "712", "name": "Nightride Chillsynth", "url": "https://stream.nightride.fm/chillsynth.mp3"},
    {"ext": "713", "name": "Nightride Darksynth", "url": "https://stream.nightride.fm/darksynth.mp3"},
    {"ext": "721", "name": "1.FM Amsterdam Trance", "url": "http://strm112.1.fm/atr_mobile_mp3"},
    {"ext": "722", "name": "RauteMusik Trance", "url": "https://trance-high.rautemusik.fm/"},
    {"ext": "723", "name": "UK Bass Radio (dubstep / bass)", "url": "https://www.ukbassradio.com/stream"},
]

# AllStar access numbers hand-written in extensions.conf [ech-allstar] (2026-11-19).
ALLSTAR_PHONE_ENTRIES: list[tuple[str, str]] = [
    ("750", "[ALLSTAR] Local node (phone mode: *3 node connect, *99 talk)"),
    ("751", "[ALLSTAR] W1PBR Pen Bay ARC hub 620860 (listen)"),
    ("752", "[ALLSTAR] W1PBR 147.060 Hope ME 620861 (listen)"),
]

_EXT_RE = re.compile(r"^7[0-4]\d$")      # 750-799 belong to AllStar access (extensions.conf [ech-allstar])
# Dialplan-safe: no comma (argument separator), quotes, spaces, semicolons, ${ or newlines.
_URL_RE = re.compile(r"^https?://[A-Za-z0-9._~:/?#\[\]@!&'()*+=%-]+$")


def streams_from_config(cfg: dict) -> list[dict]:
    """The enabled streams, sorted by extension; bad entries are skipped with a warning."""
    sc = (cfg or {}).get("pbx_streams") or {}
    if sc.get("enabled") is False:
        return []
    merged: dict[str, dict] = {}
    if sc.get("defaults", True):
        merged = {s["ext"]: dict(s) for s in DEFAULT_STREAMS}
    for s in sc.get("streams") or []:
        ext = str((s or {}).get("ext", "")).strip()
        if not _EXT_RE.match(ext):
            log.warning("pbx_streams: skipping %r (ext must be 700-749)", s)
            continue
        if s.get("enabled") is False:
            merged.pop(ext, None)
            continue
        merged[ext] = {**merged.get(ext, {}), **{k: str(v).strip() for k, v in s.items() if k in ("name", "url")}, "ext": ext}
    out = []
    for ext, s in sorted(merged.items()):
        if not _URL_RE.match(s.get("url", "")) or "${" in s.get("url", ""):
            log.warning("pbx_streams: skipping %s, unusable URL %r", ext, s.get("url"))
            continue
        out.append({"ext": ext, "name": s.get("name") or f"Stream {ext}", "url": s["url"]})
    return out


def render_dialplan(streams: list[dict], audio: dict | None = None) -> str:
    """Extension lines for [from-internal] (the file is #included inside that context)."""
    lines = ["; Written by ECH (ech/core/pbx_streams.py) from config.yaml pbx_streams.",
             "; Do not edit: changes are overwritten. Dial 7xx to listen to an internet stream."]
    if audio and audio["enabled"] and re.fullmatch(r"\d{2,6}", audio["webphone_ext"]):
        lines += [f"exten => {audio['webphone_ext']},1,Goto(ech-audio,webphone,1)   ; ECH web phone"]
    if audio and audio["enabled"] and re.fullmatch(r"7[5-9]0", audio.get("m17_ext") or ""):
        # 760 = first M17 adapter, 761 = second, ... (ECH sorts them by name)
        lines += [f"exten => _{audio['m17_ext'][:2]}[0-4],1,Set(ECHM17=${{EXTEN:2}})   ; live M17 audio",
                  " same => n,Goto(ech-audio,m17,1)"]
    for s in streams:
        name = re.sub(r"[^\w .()/&'+-]", "", s["name"])[:60]
        lines += [f"exten => {s['ext']},1,NoOp(ECH stream: {name})",
                  " same => n,Answer()",
                  f" same => n,MP3Player({s['url']})",
                  " same => n,Hangup()"]
    return "\n".join(lines) + "\n"


def render_audio_conf(audio: dict) -> str:
    """[ech-audio]: AllStar listen leg and the browser phone's inbound side."""
    a = audio
    lines = ["; Written by ECH (ech/core/pbx_streams.py) from config.yaml pbx_audio. Do not edit.",
             "[ech-audio]"]
    if a["enabled"] and re.fullmatch(r"\d{3,7}", a["allstar_node"]):
        lines += ["; web UI listening: AMI originates Local/allstar@ech-audio into AudioSocket",
                  "exten => allstar,1,Answer()",
                  f" same => n,Rpt({a['allstar_node']},P)",
                  " same => n,Hangup()"]
    if a["enabled"]:
        svc = f"127.0.0.1:{a['port']}"
        # Dial(AudioSocket/...), not the AudioSocket() app: only the channel driver
        # forwards the phone's DTMF (*99 / # push-to-talk). On the live ASL3 build
        # app_audiosocket has no DTMF code at all (checked 2026-11-21).
        lines += ["; desk phones on live M17 audio: ECH plays the hub source into the call,",
                  "; *99 keys up (the phone's mic goes out on M17), # unkeys",
                  "exten => m17,1,Answer()",
                  " same => n,Set(ECHU=${UUID()})",
                  " same => n,UserEvent(ECHListen,Uuid: ${ECHU},Source: m17#${ECHM17})",
                  f" same => n,Dial(AudioSocket/{svc}/${{ECHU}})",
                  " same => n,Hangup()"]
        lines += ["; inbound calls to the web phone: ring the browsers if one is available",
                  'exten => webphone,1,GotoIf($["${DB(ech/webphone)}" = "1"]?ring)',
                  " same => n,Answer()",
                  " same => n,Playback(vm-nobodyavail)",
                  " same => n,Hangup()",
                  " same => n(ring),Answer()",
                  " same => n,Set(ECHU=${UUID()})",
                  " same => n,UserEvent(ECHWebPhone,Uuid: ${ECHU},Num: ${CALLERID(num)},Name: ${FILTER(A-Za-z0-9 ._-,${CALLERID(name)})})",
                  f" same => n,AudioSocket(${{ECHU}},{svc})",
                  " same => n,Hangup()"]
    return "\n".join(lines) + "\n"


def _write_if_changed(path: str, text: str) -> bool:
    p = Path(path)
    try:
        if p.exists() and p.read_text() == text:
            return False
        if not p.exists() and not os.access(p.parent, os.W_OK):
            log.info("pbx_streams: %s not set up; skipped", path)
            return False
        p.write_text(text)
        return True
    except OSError as e:
        log.warning("pbx_streams: cannot write %s: %s", path, e)
        return False


def sync_conf(cfg: dict, path: str = CONF_PATH, audio_path: str = AUDIO_CONF_PATH) -> bool:
    """Write the dialplan files if they changed. True = changed (dialplan needs a reload).
    Missing, unwritable targets are skipped (they need the one-time setup)."""
    audio = audio_options(cfg)
    a = _write_if_changed(path, render_dialplan(streams_from_config(cfg), audio))
    b = _write_if_changed(audio_path, render_audio_conf(audio)) if Path(audio_path).parent.exists() else False
    return a or b
