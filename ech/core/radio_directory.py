"""
ech/core/radio_directory.py
-----------------------------
O98: a directory of commonly-used HamVOIP/AllStarLink nodes (group, test,
audio) and AREDN mesh addresses/servers, surfaced in the UI with a
click-to-dial action (reuses the existing POST /api/pbx/call → originate()
path — no new dial mechanism needed).

Honesty note on scope: AllStarLink/HamVOIP "test"/parrot nodes below are
real, specific node numbers confirmed via public sources this session (see
each entry's `source`) — not guessed. Live "music"/jam node numbers and any
"group"/hub node are NOT included built-in: no specific, confirmed-real,
durable public number for either was found in this session's research, and
these are the kind of informal, regionally/community-specific nodes where
a wrong number silently reaches a stranger's station — worse than leaving
it blank. AREDN has NO built-in entries at all: AREDN addresses
(`nodename.local.mesh`, 44.x.x.x) are private to each physical mesh network
with no universal public directory, so nothing here could possibly be a
real cross-deployment default.

Both lists are meant to be extended via config.yaml with entries the
operator actually knows are good for their own region/mesh — see
`radio_directory:` in config.yaml.
"""

from __future__ import annotations

# (name, node/address, category, description, source)
# category: "test" | "group" | "music"
BUILTIN_HAMVOIP_ENTRIES = [
    {
        "name": "HamVoIP Parrot (Dallas, TX)",
        "destination": "55553",
        "category": "test",
        "description": "Audio-level parrot — records a transmission, reports whether your "
                        "level is low/normal/high, then plays it back.",
        "source": "mackinnon.info/ampersand/parrot-55553-notes",
    },
    {
        "name": "DVSwitch Parrot",
        "destination": "42565",
        "category": "test",
        "description": "Echo/parrot test node.",
        "source": "dvswitch.groups.io allstarlink topic 28901497",
    },
    {
        "name": "UK Hubnet Parrot",
        "destination": "40894",
        "category": "test",
        "description": "Echo/parrot test node (UK Hubnet).",
        "source": "AllStarLink community forum research, 2026-10",
    },
    {
        "name": "Echo-Test Node 48230",
        "destination": "48230",
        "category": "test",
        "description": "Echo-test/parrot-type node.",
        "source": "community.allstarlink.org \"Test A ECHO-TEST NODE 48230 for me?\"",
    },
    {
        "name": "Audio Diagnostics Node 2002",
        "destination": "2002",
        "category": "test",
        "description": "Special software for audio diagnostics and network testing.",
        "source": "AllStarLink community forum research, 2026-10",
    },
]

# No confirmed-real built-in AREDN addresses exist — see module docstring.
BUILTIN_AREDN_ENTRIES: list[dict] = []


def get_directory(config: dict) -> dict:
    """Merge built-in confirmed-real entries with operator-supplied ones
    from config.yaml's radio_directory: block. Operator entries are NOT
    validated (ECH has no way to confirm an arbitrary node/address is
    real/safe) — they're trusted as the operator's own knowledge."""
    cfg = config.get("radio_directory", {}) or {}
    hamvoip = list(BUILTIN_HAMVOIP_ENTRIES) + list(cfg.get("hamvoip_entries", []) or [])
    aredn = list(BUILTIN_AREDN_ENTRIES) + list(cfg.get("aredn_entries", []) or [])
    return {"hamvoip": hamvoip, "aredn": aredn}
