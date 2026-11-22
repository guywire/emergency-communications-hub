"""Recognise traffic from other mesh bots (shared by the mesh bot and the bridges).

Shared channels such as #bot, #weather and #test carry several bots
(EWR-BOT, Zippy_WX_BOT, VTRPH Pingbot, NoWeare.NH.PingBot, txtMesh...). ECH's
bot must not answer them (bot-to-bot loops, "Unrecognized command" spam), and
the bridges must not carry their output to another network (2026-11-21: #test
was relaying "Path", "ack @[...] hops" and node-list replies onto Meshtastic).
"""
from __future__ import annotations

import re

# A sender whose name says it is a bot: "bot" ending a word, not inside one ("Abbott").
BOT_NAME_RE = re.compile(r"bot(?![a-z])", re.IGNORECASE)
# Bodies that are another bot's output, whatever the sender is called.
BOT_BODY_RE = re.compile(
    r"^\s*(?:\[[0-9a-f]{2,8}\]\s|[0-9a-f]{4}:\s|pong\b|bot stats\b|unrecognized command|unknown command)",
    re.IGNORECASE)


def is_bot_message(msg, ignore_names=(), check_names: bool = True) -> bool:
    """True for traffic from another bot: a name on the ignore list, a name that
    ends a word in "bot", or a body shaped like a bot reply (multi-line reports,
    "[8875] ..." tags, "E0C8: ..." node lists, "Pong!")."""
    who = (getattr(msg, "from_display", "") or getattr(msg, "from_id", "") or "").strip().lower()
    if who and any(who == n or who.startswith(n + " ") for n in ignore_names):
        return True
    if check_names and BOT_NAME_RE.search(who):
        return True
    body = getattr(msg, "body", "") or ""
    return bool(BOT_BODY_RE.match(body)) or body.count("\n") >= 2
