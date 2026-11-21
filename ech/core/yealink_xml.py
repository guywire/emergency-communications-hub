"""Yealink XML Browser pages: ECH on a desk phone without a web browser.

A line key of type 27 ("XML Browser") opens a URL on the phone and renders
Yealink's XML objects natively: YealinkIPPhoneTextMenu (a list of choices,
each opening another URL) and YealinkIPPhoneTextScreen (a page of text).
These builders return those documents; ech/api/app.py serves them under
/yealink/<key>/..., where <key> is a random per-install secret that only
phones ECH configured know, so a LAN host can't read the Winlink inbox by
guessing a URL.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from xml.sax.saxutils import escape

# A T67 shows "File layout error" for a 2-4 KB TextScreen while ~350-byte ones
# render, so long text is split into pages of PAGE_CHARS and offered as a menu.
PAGE_CHARS = 700
_MAX_TEXT = PAGE_CHARS
_ASCII = {"…": "...", "‘": "'", "’": "'", "“": '"', "”": '"',
          "–": "-", "—": "-", " ": " ", "•": "*", "°": "", "→": "->", "←": "<-",
          "↑": "^", "↓": "v", "·": "-", "×": "x", "±": "+/-", "µ": "u"}


def _x(s) -> str:
    return escape(str(s if s is not None else ""), {'"': "&quot;"})


def _ascii(text) -> str:
    """Plain ASCII for the phone: known symbols mapped, accents stripped, emoji dropped."""
    text = "".join(_ASCII.get(c, c) for c in str(text or ""))
    text = unicodedata.normalize("NFKD", text)
    out = []
    for c in text:
        if 32 <= ord(c) < 127 or c in "\n\t":
            out.append(c)
        elif unicodedata.combining(c) or ord(c) >= 0x2000 or 0xFE00 <= ord(c) <= 0xFE0F:
            continue                      # accent marks, emoji, pictographs: drop
        else:
            out.append("?")
    return re.sub(r"[ \t]{2,}", " ", "".join(out))


def _clean(text: str, limit: int = _MAX_TEXT) -> str:
    text = re.sub(r"\r\n?", "\n", str(text or ""))
    text = _ascii(text)
    text = re.sub(r"[^\S\n]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text if len(text) <= limit else text[: limit - 20].rstrip() + "\n... (truncated)"


def split_pages(text: str, size: int = PAGE_CHARS) -> list[str]:
    """Split at paragraph breaks, then at spaces, into chunks of at most `size` chars."""
    text = _clean(text, limit=10 ** 7)
    pages: list[str] = []
    cur = ""
    for para in text.split("\n\n"):
        if cur and len(para) > size:
            para = f"{cur}\n\n{para}"          # top up the current page before cutting
            cur = ""
        while len(para) > size:
            cut = para.rfind(" ", 0, size)
            cut = cut if cut > size // 2 else size
            if cur:
                pages.append(cur)
                cur = ""
            pages.append(para[:cut].strip())
            para = para[cut:].strip()
        candidate = f"{cur}\n\n{para}" if cur else para
        if len(candidate) > size:
            pages.append(cur)
            cur = para
        else:
            cur = candidate
    if cur.strip():
        pages.append(cur)
    return [p for p in pages if p.strip()] or [""]



def text_menu(title: str, items: list[tuple[str, str]], timeout: int = 0) -> str:
    """items: (prompt, absolute URL). Numbered so keypad digits pick an item."""
    rows = "".join(f"<MenuItem><Prompt>{_x(_ascii(p)[:80])}</Prompt><URI>{_x(u)}</URI></MenuItem>" for p, u in items)
    if not rows:
        rows = "<MenuItem><Prompt>(nothing here)</Prompt><URI></URI></MenuItem>"
    return (f'<?xml version="1.0" encoding="UTF-8"?>'
            f'<YealinkIPPhoneTextMenu style="numbered" Beep="no" Timeout="{timeout}" LockIn="no">'
            f'<Title wrap="no">{_x(_ascii(title)[:60])}</Title>{rows}</YealinkIPPhoneTextMenu>')


def text_screen(title: str, text: str, timeout: int = 0, beep: bool = False) -> str:
    return (f'<?xml version="1.0" encoding="UTF-8"?>'
            f'<YealinkIPPhoneTextScreen Beep="{"yes" if beep else "no"}" Timeout="{timeout}" LockIn="no">'
            f'<Title wrap="yes">{_x(_ascii(title)[:60])}</Title><Text>{_x(_clean(text))}</Text></YealinkIPPhoneTextScreen>')


def _when(iso: str) -> str:
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).astimezone().strftime("%m/%d %H:%M")
    except (ValueError, TypeError):
        return str(iso or "")[:16]


# Bot commands the phone menu can run: path name -> (menu label, bot command).
# The mesh bot's own handlers produce the text, so the phone shows what a mesh
# user would get from the same command.
BOT_PAGES: dict[str, tuple[str, str]] = {
    "weather": ("Weather forecast", "weather"),
    "metar":   ("Airport weather (METAR)", "metar"),
    "planes":  ("Aircraft overhead", "overhead"),
    "ships":   ("Ships nearby", "ships"),
    "sat":     ("Satellite passes", "satpass"),
    "solar":   ("Space weather", "solar"),
    "tide":    ("Tides", "tide"),
    "marine":  ("Marine / buoy", "marine"),
    "sun":     ("Sun and moon", "sun"),
    "nodes":   ("Mesh nodes heard", "nodes"),
    "contest": ("Contests", "contest"),
}


def main_menu(base: str) -> str:
    def bot(k):
        return BOT_PAGES[k][0], f"{base}/bot/{k}.xml"
    return text_menu("ECH", [
        ("Winlink inbox", f"{base}/winlink.xml"),
        ("Directory", f"{base}/dir.xml"),
        ("Weather alerts", f"{base}/alerts.xml"),
        ("Forecast (7 day)", f"{base}/forecast.xml"),
        ("Conditions now", f"{base}/bot/weather.xml"), bot("metar"),
        ("Air quality", f"{base}/aq.xml"),
        ("HF conditions", f"{base}/hf.xml"),
        bot("solar"),
        bot("planes"), bot("ships"),
        ("APRS stations heard", f"{base}/aprs.xml"),
        bot("sat"), bot("tide"), bot("marine"), bot("sun"),
        ("World news", f"{base}/news/world.xml"),
        ("Local news", f"{base}/news/local.xml"),
        ("Markets (NQ, ES futures)", f"{base}/markets.xml"),
        ("AllStar", f"{base}/allstar.xml"),
        ("M17", f"{base}/m17.xml"),
        ("Mesh messages", f"{base}/mesh.xml"),
        bot("nodes"),
        ("Phones online", f"{base}/pbx.xml"),
        bot("contest"),
    ])


def forecast_menu(base: str, periods: list[dict]) -> str:
    """NWS 7-day forecast: one line per period, each opening the detailed text."""
    items = [(f"{p.get('name', '')}: {p.get('temperature', '')}{p.get('temperatureUnit', '')} {p.get('shortForecast', '')}",
              f"{base}/forecast.xml?period={i}") for i, p in enumerate(periods[:14], 1)]
    return text_menu("Forecast (NWS)", items)


def forecast_period(p: dict) -> str:
    wind = f"{p.get('windDirection', '')} {p.get('windSpeed', '')}".strip()
    head = f"{p.get('temperature', '')}{p.get('temperatureUnit', '')}  {p.get('shortForecast', '')}"
    return text_screen(p.get("name") or "Forecast",
                       f"{head}\nWind {wind}\n\n{p.get('detailedForecast') or ''}")


def paged_screen(title: str, text: str, page_url: str, page: int = 0) -> str:
    """Text that may be long: one screen if it fits, else a menu of small parts."""
    pages = split_pages(text)
    if page and 1 <= page <= len(pages):
        return text_screen(f"{title} ({page}/{len(pages)})", pages[page - 1])
    if len(pages) == 1:
        return text_screen(title, pages[0])
    sep = "&" if "?" in page_url else "?"
    return text_menu(title, [(f"Part {i} of {len(pages)}: " + re.sub(r"\s+", " ", t)[:48], f"{page_url}{sep}page={i}")
                             for i, t in enumerate(pages, 1)])


DIR_PAGE = 20


def directory_index(base: str, books: list[tuple[str, str, int]]) -> str:
    """books: (key, label, entry count)."""
    return text_menu("Directory", [(f"{label} ({n})", f"{base}/dir/{key}.xml") for key, label, n in books])


def _initial(name: str) -> str:
    c = _ascii(name).lstrip("[(*# ")[:1].upper()
    return c if c.isalpha() else "#"


def directory_letters(title: str, entries: list[tuple[str, str]], page_url: str) -> str:
    """Big books: first pick a letter ("A (34)"), then page through that letter."""
    counts: dict[str, int] = {}
    for n, _ in entries:
        counts[_initial(n)] = counts.get(_initial(n), 0) + 1
    return text_menu(f"{title} ({len(entries)})",
                     [(f"{k} ({v})", f"{page_url}?letter={'0' if k == '#' else k}") for k, v in sorted(counts.items())])


def directory_page(title: str, entries: list[tuple[str, str]], page_url: str, dial_url: str,
                   page: int = 1, letter: str = "") -> str:
    """A text menu of 'Name  number'; picking one dials it (via dial_url). DIR_PAGE per page,
    with Prev/Next items. (The T67 rejects YealinkIPPhoneDirectory with "File layout error".)"""
    if letter:
        want = "#" if letter == "0" else letter.upper()
        entries = [e for e in entries if _initial(e[0]) == want]
    pages = max(1, (len(entries) + DIR_PAGE - 1) // DIR_PAGE)
    page = min(max(1, page), pages)
    chunk = entries[(page - 1) * DIR_PAGE: page * DIR_PAGE]
    q = f"letter={letter}&" if letter else ""
    items = []
    if page > 1:
        items.append(("< Previous page", f"{page_url}?{q}page={page - 1}"))
    for n, t in chunk:
        num = re.sub(r"[^0-9*#]", "", str(t))
        if num:
            items.append((f"{_ascii(n)[:44]}  {num}", f"{dial_url}?n={num}"))
    if page < pages:
        items.append((f"Next page > ({page + 1}/{pages})", f"{page_url}?{q}page={page + 1}"))
    t = f"{title}{' ' + (letter if letter != '0' else '#') if letter else ''}"
    return text_menu(f"{t} {page}/{pages}" if pages > 1 else t, items)


def dial_execute(number: str) -> str:
    """Tell the phone to dial a number."""
    return (f'<?xml version="1.0" encoding="UTF-8"?>'
            f'<YealinkIPPhoneExecute Beep="no"><ExecuteItem URI="Dial:{_x(number)}"/></YealinkIPPhoneExecute>')


def aprs_menu(base: str, packets: list[dict], limit: int = 30) -> str:
    """Most recently heard APRS stations, newest first; each opens the bot's lookup."""
    seen, items = set(), []
    for p in reversed(packets):
        call = str(p.get("from") or "").strip().upper()
        if not call or call in seen or not re.fullmatch(r"[A-Z0-9]{3,9}(-[A-Z0-9]{1,2})?", call):
            continue
        seen.add(call)
        when = datetime.fromtimestamp(p["ts"]).strftime("%H:%M") if p.get("ts") else ""
        items.append((f"{when} {call} {p.get('summary') or ''}", f"{base}/bot/aprs.xml?call={call}"))
        if len(items) >= limit:
            break
    if not items:
        return text_screen("APRS", "No APRS stations heard yet.")
    return text_menu(f"APRS heard ({len(items)})", items)


def winlink_menu(base: str, inbox: dict) -> str:
    msgs = inbox.get("messages") or []
    if not msgs:
        return text_screen("Winlink", "Inbox is empty (or Pat is not reachable).")
    items = []
    for m in msgs[:40]:
        unread = str(m.get("unread")).lower() in ("true", "1", "yes")
        items.append((f"{'* ' if unread else ''}{_when(m.get('date'))} {m.get('from', '')}: {m.get('subject', '')}",
                      f"{base}/winlink/{m.get('mid', '')}.xml"))
    n, u = inbox.get("count", len(msgs)), inbox.get("unread", 0)
    return text_menu(f"Winlink {inbox.get('callsign', '')} ({u} unread of {n})".replace("  ", " "), items)


def winlink_message(msg: dict, page_url: str = "", page: int = 0) -> str:
    """One page of a message. page 0 = whole message if it fits, else a menu of its pages."""
    to = msg.get("to") or []
    to_s = ", ".join(to[:6]) + (f" +{len(to) - 6} more" if len(to) > 6 else "") if isinstance(to, list) else str(to)
    head = f"From: {msg.get('from', '')}\nDate: {_when(msg.get('date'))}\nTo: {to_s}\n\n"
    pages = split_pages(head + str(msg.get("body") or ""))
    subject = msg.get("subject") or "(no subject)"
    if page and 1 <= page <= len(pages):
        return text_screen(f"{subject} ({page}/{len(pages)})", pages[page - 1])
    if len(pages) == 1 or not page_url:
        return text_screen(subject, pages[0])
    items = []
    for i, txt in enumerate(pages, 1):
        first = re.sub(r"\s+", " ", txt)[:48]
        items.append((f"Part {i} of {len(pages)}: {first}", f"{page_url}?page={i}"))
    return text_menu(subject, items)


def alerts_screen(alerts: list[dict]) -> str:
    if not alerts:
        return text_screen("Weather alerts", "No active NWS alerts.")
    parts = []
    for a in alerts[:6]:
        parts.append(f"{a.get('event') or 'Alert'}\n{a.get('headline') or ''}")
    return text_screen(f"Weather alerts ({len(alerts)})", "\n\n".join(parts), beep=True)


def aq_screen(status: dict) -> str:
    c = (status or {}).get("current") or {}
    if not c:
        return text_screen("Air quality", "No air quality reading yet.")
    return text_screen("Air quality",
                       f"AQI {c.get('aqi')}  {c.get('category', '')}\n"
                       f"Pollutant: {c.get('parameter', '')}\nArea: {c.get('reporting_area', '')}\n"
                       f"Smoke areas tracked: {status.get('smoke_polygon_count', 0)}")


def hf_screen(p: dict) -> str:
    if not p or p.get("error"):
        return text_screen("HF conditions", "Propagation data unavailable.")
    lines = [f"SFI {p.get('sfi')}  SSN {p.get('sunspots')}", f"K {p.get('kindex')}  A {p.get('aindex')}",
             f"X-ray {p.get('xray')}  Noise {p.get('signalnoise')}", "", "Band     day / night"]
    for band, c in (p.get("bands") or {}).items():
        lines.append(f"{band:<8} {c.get('day', '?')} / {c.get('night', '?')}")
    lines.append(f"\n{p.get('created', '')}")
    return text_screen("HF conditions", "\n".join(lines))


def mesh_screen(messages: list[dict]) -> str:
    rows = []
    for m in messages:
        if m.get("msg_type") != "text" or re.match(r"^(aprs|irc)", m.get("source_adapter") or ""):
            continue
        rows.append(f"{_when(m.get('timestamp'))} {m.get('from_display') or m.get('from_id')} "
                    f"[{m.get('source_channel') or m.get('source_adapter')}]\n{_clean(m.get('body') or '', 120)}")
        if len(rows) >= 4:
            break
    return text_screen("Mesh messages", "\n\n".join(rows) or "No recent mesh messages.")


def pbx_screen(endpoints: list[dict]) -> str:
    if not endpoints:
        return text_screen("Phones", "PBX not connected.")
    on = [e for e in endpoints if e.get("online")]
    lines = [f"{e.get('extension')}  {e.get('device_state', '')}" for e in on] or ["No extensions online."]
    return text_screen(f"Phones ({len(on)} of {len(endpoints)} online)", "\n".join(lines))


# ── AllStar and M17 panels (line keys "AllStar" / "M17") ─────────────────

def input_screen(title: str, url: str, param: str, prompt: str, kind: str = "number") -> str:
    """Numeric (kind="number") or text ("string", multi-tap) entry; the phone requests url?<param>=<value>."""
    kind = "string" if kind == "string" else "number"
    return (f'<?xml version="1.0" encoding="UTF-8"?>'
            f'<YealinkIPPhoneInputScreen type="{kind}" displayMode="normal" inputLanguage="English" '
            f'defaultIndex="1" Beep="no" Timeout="0" LockIn="no">'
            f'<Title>{_x(_ascii(title)[:60])}</Title><URL>{_x(url)}</URL>'
            f'<InputField type="{kind}"><Prompt>{_x(_ascii(prompt)[:30])}</Prompt>'
            f'<Parameter>{_x(param)}</Parameter><Selection>1</Selection><Default></Default></InputField>'
            f'</YealinkIPPhoneInputScreen>')


TALK_HELP = {
    "allstar": ("How to talk: AllStar",
                "1. Dial 750 (or pick Listen to the node).\n"
                "2. Press *99 to transmit (PTT on). Wait a second, then speak.\n"
                "3. Press # to stop transmitting. Then you hear the node again.\n\n"
                "751 / 752 link the hub listen-only: your audio reaches node {node} "
                "but not the hub. To be heard there, link it transceive from the web "
                "Listen page.\n\n"
                "Each transmission times out after 3 minutes. Identify with your callsign."),
    "m17": ("How to talk: M17",
            "1. Dial 760 (761 = second link), or pick Listen / talk.\n"
            "2. Press *99 to transmit. Talk after the high beep.\n"
            "3. Press # to stop (two falling beeps).\n\n"
            "A low tone means you can't transmit: someone else is talking, or the "
            "link is listen-only. You don't hear the reflector while you transmit.\n\n"
            "3 minute limit. Your audio goes out under the station callsign; "
            "identify with yours."),
}


def talk_help(kind: str, node: str = "") -> str:
    title, text = TALK_HELP.get(kind, TALK_HELP["m17"])
    return text_screen(title, text.replace("{node}", node or "ECH's"))


def allstar_menu(base: str, node: str, links: list[str], presets: list[dict]) -> str:
    linked = ", ".join(x.lstrip("RT") for x in links) or "nothing"
    items = [(f"Linked to: {linked}", f"{base}/allstar.xml"),
             ("How to talk (PTT: *99 on, # off)", f"{base}/talk.xml?t=allstar"),
             ("Listen to the node (750, *99 = talk)", f"{base}/dial.xml?n=750")]
    for i, p in enumerate(presets[:2]):
        items.append((f"Listen: {p['name']}", f"{base}/dial.xml?n={751 + i}"))
    for p in presets:
        items.append((f"Link (no call): {p['name']}", f"{base}/allstar/link.xml?node={p['node']}"))
    items += [("Node directory...", f"{base}/allstar/dir.xml"),
              ("Link another node...", f"{base}/allstar/input.xml"),
              ("Disconnect all links", f"{base}/allstar/unlink.xml")]
    return text_menu(f"AllStar {node}", items)


def allstar_dir_menu(base: str, state: str, n_state: int, n_total: int, error: str = "") -> str:
    if not n_total:
        return text_screen("AllStar directory", "The node list isn't downloaded yet"
                           + (f" ({error})" if error else "") + ". Try again in a minute.")
    items = []
    if state:
        items.append((f"Nodes in {state.title()} ({n_state})", f"{base}/allstar/dir/state.xml"))
    items += [("Search by callsign...", f"{base}/allstar/dir/callinput.xml"),
              ("Look up a node number...", f"{base}/allstar/dir/nodeinput.xml")]
    return text_menu(f"AllStar directory ({n_total} nodes)", items)


def allstar_node_list(base: str, title: str, rows: list[dict], page_url: str, page: int = 1) -> str:
    """rows from AllStarDirectory; picking one opens its action menu. page_url gets &page=N."""
    from ech.core.allstar_directory import label
    if not rows:
        return text_screen(title, "No nodes found.")
    pages = max(1, (len(rows) + DIR_PAGE - 1) // DIR_PAGE)
    page = min(max(1, page), pages)
    sep = "&" if "?" in page_url else "?"
    items = []
    if page > 1:
        items.append(("< Previous page", f"{page_url}{sep}page={page - 1}"))
    items += [(label(r), f"{base}/allstar/node.xml?node={r['node']}")
              for r in rows[(page - 1) * DIR_PAGE: page * DIR_PAGE]]
    if page < pages:
        items.append((f"Next page > ({page + 1}/{pages})", f"{page_url}{sep}page={page + 1}"))
    return text_menu(f"{title} {page}/{pages}" if pages > 1 else title, items)


def allstar_node_menu(base: str, r: dict, linked: bool) -> str:
    n = r["node"]
    head = " - ".join(x for x in (r.get("desc"), r.get("loc")) if x) or "(no description)"
    items = [(head, f"{base}/allstar/node.xml?node={n}")]
    if linked:
        items += [("Listen now (call 750)", f"{base}/dial.xml?n=750"),
                  ("Disconnect this node", f"{base}/allstar/unlink.xml?node={n}")]
    else:
        items += [("Link and listen (calls 750)", f"{base}/allstar/link.xml?node={n}&dial=1"),
                  ("Link listen-only (no call)", f"{base}/allstar/link.xml?node={n}")]
    items.append(("Link transceive (talk there)...", f"{base}/allstar/node.xml?node={n}&confirm=tx"))
    return text_menu(f"{n} {r['call']}{'  (linked)' if linked else ''}", items)


def allstar_confirm_tx(base: str, mine: str, r: dict) -> str:
    return text_menu(f"Transceive {r['node']}?", [
        (f"Yes: *99 on {mine} goes out on {r['node']} {r['call']}", f"{base}/allstar/link.xml?node={r['node']}&mode=tx&dial=1"),
        ("No, go back", f"{base}/allstar/node.xml?node={r['node']}")])


def m17_menu(base: str, adapters: list[dict]) -> str:
    """adapters: name, host, module, connected, ext (the listen extension)."""
    if not adapters:
        return text_screen("M17", "No M17 adapter is configured.")
    items = []
    for a in adapters:
        state = f"module {a['module']}" if a.get("connected") else "not connected"
        items += [(f"{a['host']} - {state}", f"{base}/m17.xml")]
        if a.get("ext"):
            items.append((f"Listen live ({a['ext']}) - receive only" if a.get("listen_only")
                          else f"Listen / talk ({a['ext']}, *99 talk, # stop)", f"{base}/dial.xml?n={a['ext']}"))
        items += [("How to talk on M17", f"{base}/talk.xml?t=m17"),
                  ("Change module...", f"{base}/m17/module.xml?a={a['name']}"),
                  ("Change reflector...", f"{base}/m17/reflectors.xml?a={a['name']}"),
                  ("Recent messages", f"{base}/m17/messages.xml?a={a['name']}")]
    return text_menu("M17", items)


def m17_modules(base: str, adapter: str, current: str, host: str = "", port: int = 0) -> str:
    """Module letters. With host: pick a module on that (new) reflector, which switches server."""
    if host:
        items = [(c, f"{base}/m17/server.xml?a={adapter}&h={host}&p={port or 17000}&m={c}")
                 for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"]
        return text_menu(f"Module on {host}"[:60], items)
    items = [(f"{c}{'  (current)' if c == current else ''}", f"{base}/m17/module.xml?a={adapter}&m={c}")
             for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"]
    return text_menu(f"M17 module (now {current or '?'})", items)


def m17_reflectors(base: str, adapter: str, rows: list[dict], current_host: str, page: int = 1) -> str:
    """Saved + directory reflectors, DIR_PAGE per page; picking one asks for a module."""
    pages = max(1, (len(rows) + DIR_PAGE - 1) // DIR_PAGE)
    page = min(max(1, page), pages)
    items = []
    if page > 1:
        items.append(("< Previous page", f"{base}/m17/reflectors.xml?a={adapter}&page={page - 1}"))
    for r in rows[(page - 1) * DIR_PAGE: page * DIR_PAGE]:
        mark = " (current)" if r["host"] == current_host else (" *" if r.get("source") == "saved" else "")
        label = r["name"] if r["name"] != r["host"] else r["host"]
        items.append((f"{label}{mark}", f"{base}/m17/module.xml?a={adapter}&h={r['host']}&p={r['port']}"))
    if page < pages:
        items.append((f"Next page > ({page + 1}/{pages})", f"{base}/m17/reflectors.xml?a={adapter}&page={page + 1}"))
    return text_menu(f"M17 reflectors {page}/{pages}" if pages > 1 else "M17 reflectors", items)


def m17_messages(rows: list[dict]) -> str:
    if not rows:
        return text_screen("M17 messages", "No M17 messages yet.")
    parts = [f"{_when(m.get('timestamp'))} {m.get('from_display') or m.get('from_id')}\n{_clean(m.get('body') or '', 140)}"
             for m in rows[:5]]
    return text_screen("M17 messages", "\n\n".join(parts))


# ── News and markets ──────────────────────────────────────────────────────

def news_menu(base: str, kind: str, items: list[dict]) -> str:
    title = "Local news" if kind == "local" else "World news"
    if not items:
        return text_screen(title, "No headlines right now.")
    return text_menu(title, [(it["title"], f"{base}/news/{kind}/{i}.xml") for i, it in enumerate(items[:30])])


def news_item(it: dict) -> str:
    when = ""
    if it.get("published"):
        when = datetime.fromtimestamp(it["published"]).strftime("%m/%d %H:%M") + "  "
    return text_screen(it.get("source") or "News",
                       f"{it['title']}\n\n{it.get('summary') or ''}\n\n{when}{it.get('source') or ''}")


def markets_screen(quotes: list[dict], fmt, sentiment: dict | None = None, fmt_kalshi=None, vix_label=None) -> str:
    sentiment = sentiment or {}
    if not quotes and not sentiment.get("vix") and not sentiment.get("kalshi"):
        return text_screen("Markets", "Quotes unavailable right now.")
    blocks = [fmt(q) for q in quotes]
    if quotes:
        t = max((q.get("time") or 0) for q in quotes)
        if t:
            blocks.append("Futures as of " + datetime.fromtimestamp(t).strftime("%m/%d %H:%M") + " (delayed)")
    v = sentiment.get("vix")
    if v and vix_label:
        blocks.append(f"VIX {v['price']:.2f} - {vix_label(v['price'])}")
    if sentiment.get("kalshi") and fmt_kalshi:
        blocks.append("Kalshi odds:\n" + "\n".join(fmt_kalshi(k) for k in sentiment["kalshi"]))
    elif v:
        blocks.append("Kalshi: no daily index market open now.")
    return text_screen("Markets", "\n\n".join(blocks) + "\nMarket odds and quotes, not advice.")
