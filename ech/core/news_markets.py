"""News headlines (local + world) and index futures for the mesh bot and desk phones.

config.yaml (all optional):
  news:
    local: ["https://www.penbaypilot.com/rss.xml", ...]   # RSS 2.0 or Atom
    world: ["https://feeds.bbci.co.uk/news/world/rss.xml", ...]
  markets:
    symbols: [["NQ=F", "Nasdaq-100 futures (NQ)"], ["ES=F", "S&P 500 futures (ES)"]]

Headlines are cached 15 min and quotes 3 min so phones and bot commands don't
hammer the sources. Futures come from Yahoo Finance's public chart endpoint
(unofficial, delayed, personal use) - quotes, not predictions.
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
import time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

log = logging.getLogger(__name__)

DEFAULT_FEEDS = {
    "local": ["https://www.penbaypilot.com/rss.xml", "https://www.bangordailynews.com/feed/"],
    "world": ["https://feeds.bbci.co.uk/news/world/rss.xml", "https://feeds.npr.org/1004/rss.xml"],
}
DEFAULT_SYMBOLS = [("NQ=F", "Nasdaq-100 futures (NQ)"), ("ES=F", "S&P 500 futures (ES)")]
# The web Outlook panel's wider board (config markets.panel: [[group, [[sym, name], ...]], ...]).
# Heating oil, lumber and the Canadian dollar are there for a Maine station.
DEFAULT_PANEL = [
    ("Index futures", [("ES=F", "S&P 500"), ("NQ=F", "Nasdaq-100"), ("YM=F", "Dow"), ("RTY=F", "Russell 2000")]),
    ("Energy", [("CL=F", "Crude oil"), ("HO=F", "Heating oil"), ("RB=F", "Gasoline"), ("NG=F", "Natural gas")]),
    ("Metals", [("GC=F", "Gold"), ("SI=F", "Silver"), ("HG=F", "Copper")]),
    ("Rates & currency", [("^TNX", "10-yr Treasury yield"), ("DX-Y.NYB", "US dollar index"),
                          ("EURUSD=X", "EUR/USD"), ("CADUSD=X", "CAD/USD")]),
    ("Crypto", [("BTC-USD", "Bitcoin"), ("ETH-USD", "Ethereum")]),
    ("Commodities", [("LBR=F", "Lumber"), ("ZC=F", "Corn")]),
]
NEWS_TTL, QUOTE_TTL = 900, 180
CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=1d&interval=15m"
_UA = {"User-Agent": "Mozilla/5.0 (ECH news)"}


def _text(s) -> str:
    """Plain text from feed HTML. Some feeds escape their HTML twice (PenBay
    Pilot sends &lt;span ...&gt;), so unescape and strip tags twice."""
    s = s or ""
    for _ in range(2):
        s = re.sub(r"<[^>]+>", " ", html.unescape(s))
    return re.sub(r"\s+", " ", s).strip()


def _source_name(url: str) -> str:
    host = re.sub(r"^https?://(www\.|feeds\.)?", "", url).split("/")[0]
    return {"bbci.co.uk": "BBC", "npr.org": "NPR", "penbaypilot.com": "PenBay Pilot",
            "bangordailynews.com": "BDN", "pressherald.com": "Press Herald"}.get(host, host)


def parse_feed(xml_text: str, source: str = "") -> list[dict]:
    """RSS 2.0 <item> or Atom <entry> -> [{title, summary, link, published, source}]."""
    try:
        root = ET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    except ET.ParseError:
        return []
    out = []
    for it in root.iter():
        tag = it.tag.rsplit("}", 1)[-1]
        if tag not in ("item", "entry"):
            continue
        get = lambda name: next((c for c in it if c.tag.rsplit("}", 1)[-1] == name), None)
        title = _text(getattr(get("title"), "text", ""))
        if not title:
            continue
        summ = get("description") if get("description") is not None else get("summary")
        link = get("link")
        href = (link.get("href") or link.text or "") if link is not None else ""
        pub = getattr(get("pubDate"), "text", None) or getattr(get("published"), "text", None) \
            or getattr(get("updated"), "text", None)
        ts = 0.0
        if pub:
            try:
                ts = parsedate_to_datetime(pub).timestamp()
            except (TypeError, ValueError):
                try:
                    from datetime import datetime
                    ts = datetime.fromisoformat(pub.replace("Z", "+00:00")).timestamp()
                except ValueError:
                    ts = 0.0
        out.append({"title": title[:200], "summary": _text(getattr(summ, "text", ""))[:600],
                    "link": href.strip(), "published": ts, "source": source})
    return out


def interleave(lists: list[list[dict]], limit: int = 30) -> list[dict]:
    """Round-robin across sources (each newest first), dropping repeated titles."""
    lists = [sorted(l, key=lambda x: -x["published"]) for l in lists if l]
    out, seen = [], set()
    i = 0
    while len(out) < limit and any(i < len(l) for l in lists):
        for l in lists:
            if i < len(l):
                key = re.sub(r"\W+", "", l[i]["title"].lower())[:60]
                if key not in seen:
                    seen.add(key)
                    out.append(l[i])
        i += 1
    return out[:limit]


def parse_chart(data: dict, name: str) -> dict | None:
    try:
        m = data["chart"]["result"][0]["meta"]
    except (KeyError, IndexError, TypeError):
        return None
    price = m.get("regularMarketPrice")
    prev = m.get("chartPreviousClose") or m.get("previousClose")
    if price is None:
        return None
    chg = (price - prev) if prev else None
    return {"symbol": m.get("symbol"), "name": name, "price": price, "prev": prev,
            "change": chg, "pct": (chg / prev * 100) if prev else None,
            "high": m.get("regularMarketDayHigh"), "low": m.get("regularMarketDayLow"),
            "time": m.get("regularMarketTime")}


class NewsMarkets:
    def __init__(self, cfg: dict | None = None):
        self.configure(cfg or {})
        self._news: dict[str, tuple[float, list]] = {}
        self._quotes: tuple[float, list] = (0.0, [])
        self._sent: tuple[float, dict | None] = (0.0, None)
        self._lock = asyncio.Lock()

    def configure(self, cfg: dict) -> None:
        n = (cfg or {}).get("news") or {}
        self.feeds = {k: list(n.get(k) or DEFAULT_FEEDS[k]) for k in DEFAULT_FEEDS}
        mk = (cfg or {}).get("markets") or {}
        self.symbols = [(str(s[0]), str(s[1])) for s in (mk.get("symbols") or DEFAULT_SYMBOLS) if s]
        self.kalshi_series = [(str(k[0]), str(k[1])) for k in (mk.get("kalshi") or DEFAULT_KALSHI) if k]
        self.panel = [(str(g[0]), [(str(s[0]), str(s[1])) for s in g[1]]) for g in (mk.get("panel") or DEFAULT_PANEL)]
        self._panel: tuple[float, list] = (0.0, [])

    async def sentiment(self) -> dict:
        return await fetch_sentiment(self)

    async def headlines(self, kind: str = "world", limit: int = 30) -> list[dict]:
        kind = "local" if kind == "local" else "world"
        hit = self._news.get(kind)
        if hit and time.time() - hit[0] < NEWS_TTL:
            return hit[1][:limit]
        import httpx
        async with self._lock:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True, headers=_UA) as c:
                async def one(url):
                    try:
                        r = await c.get(url)
                        return parse_feed(r.text, _source_name(url)) if r.status_code == 200 else []
                    except Exception as e:
                        log.info("news feed %s: %s", url, e.__class__.__name__)
                        return []
                lists = await asyncio.gather(*(one(u) for u in self.feeds[kind]))
        items = interleave(list(lists), 40)
        if items or not hit:
            self._news[kind] = (time.time(), items)
        return (items or (hit[1] if hit else []))[:limit]

    async def futures(self) -> list[dict]:
        if time.time() - self._quotes[0] < QUOTE_TTL:
            return self._quotes[1]
        import httpx
        out = []
        async with httpx.AsyncClient(timeout=12, headers=_UA) as c:
            for sym, name in self.symbols:
                try:
                    r = await c.get(CHART_URL.format(sym=sym))
                    q = parse_chart(r.json(), name) if r.status_code == 200 else None
                    if q:
                        out.append(q)
                except Exception as e:
                    log.info("quote %s: %s", sym, e.__class__.__name__)
        if out or not self._quotes[1]:
            self._quotes = (time.time(), out)
        return out or self._quotes[1]


    async def panel_quotes(self) -> list[dict]:
        """[{group, quotes: [...]}] for the web Outlook panel, fetched in parallel."""
        hit = getattr(self, "_panel", (0.0, []))
        if hit[1] and time.time() - hit[0] < QUOTE_TTL:
            return hit[1]
        import httpx
        from urllib.parse import quote as _q
        sem = asyncio.Semaphore(6)
        async with httpx.AsyncClient(timeout=12, headers=_UA) as c:
            async def one(sym, name):
                async with sem:
                    try:
                        r = await c.get(CHART_URL.format(sym=_q(sym, safe="=-.")))
                        return parse_chart(r.json(), name) if r.status_code == 200 else None
                    except Exception as e:
                        log.info("quote %s: %s", sym, e.__class__.__name__)
                        return None
            groups = []
            for g, syms in self.panel:
                qs = await asyncio.gather(*(one(s, n) for s, n in syms))
                groups.append({"group": g, "quotes": [q for q in qs if q]})
        if any(g["quotes"] for g in groups) or not hit[1]:
            self._panel = (time.time(), groups)
            return groups
        return hit[1]


# ── Sentiment: VIX + Kalshi "closes up today" odds ─────────────────────────

KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"
# Kalshi's daily "up or down" markets: "Will <index> be above <yesterday's close> at 4pm ET?"
DEFAULT_KALSHI = [("KXINXDUD", "S&P 500"), ("KXNASDAQDUD", "Nasdaq-100")]


def vix_label(v: float) -> str:
    return "calm" if v < 15 else "normal" if v < 20 else "elevated" if v < 30 else "high fear"


def parse_kalshi(markets: list[dict], name: str) -> dict | None:
    """The soonest-closing open market -> implied probability (mid of bid/ask, else last trade)."""
    ms = [m for m in markets if m.get("status") in (None, "active", "open")]
    if not ms:
        return None
    m = min(ms, key=lambda x: x.get("close_time") or "")
    f = lambda k: float(m.get(k) or 0)
    bid, ask, last = f("yes_bid_dollars"), f("yes_ask_dollars"), f("last_price_dollars")
    prob = (bid + ask) / 2 if bid and ask else (last or ask or bid or None)
    if prob is None:
        return None
    return {"name": name, "ticker": m.get("ticker"), "strike": m.get("floor_strike"), "prob": prob,
            "bid": bid, "ask": ask, "last": last, "volume": f("volume_fp"),
            "close_time": m.get("close_time"), "title": m.get("title")}


async def fetch_sentiment(self: "NewsMarkets") -> dict:
    """{'vix': quote|None, 'kalshi': [odds...]} cached like the quotes."""
    hit = self._sent
    if hit[1] is not None and time.time() - hit[0] < QUOTE_TTL:
        return hit[1]
    import httpx
    out: dict = {"vix": None, "kalshi": []}
    async with httpx.AsyncClient(timeout=12, headers=_UA) as c:
        try:
            r = await c.get(CHART_URL.format(sym="%5EVIX"))
            out["vix"] = parse_chart(r.json(), "VIX") if r.status_code == 200 else None
        except Exception as e:
            log.info("VIX: %s", e.__class__.__name__)
        for series, name in self.kalshi_series:
            try:
                r = await c.get(f"{KALSHI_API}/markets", params={"series_ticker": series, "status": "open"},
                                headers={"Accept": "application/json"})
                k = parse_kalshi((r.json().get("markets") or []), name) if r.status_code == 200 else None
                if k:
                    out["kalshi"].append(k)
            except Exception as e:
                log.info("Kalshi %s: %s", series, e.__class__.__name__)
    if out["vix"] or out["kalshi"] or hit[1] is None:
        self._sent = (time.time(), out)
    return out if (out["vix"] or out["kalshi"]) else (hit[1] or out)


def fmt_kalshi(k: dict, short: bool = False) -> str:
    pct = round(k["prob"] * 100)
    if short:
        return f"{k['name']} up {pct}%"
    strike = f" (above {float(k['strike']):,.2f})" if k.get("strike") else ""
    vol = f", ${float(k['volume']):,.0f} traded" if k.get("volume") else ""
    return f"{k['name']} closes up today: {pct}%{strike}{vol}"


def bot_markets_all(quotes: list[dict], s: dict, max_len: int = 160) -> str:
    """One mesh message: futures % change, VIX, Kalshi up-close odds."""
    parts = []
    if quotes:
        parts.append(" ".join(f"{(q.get('symbol') or '').replace('=F', '')} {q['price']:,.0f} "
                              f"{'+' if (q.get('pct') or 0) >= 0 else ''}{(q.get('pct') or 0):.1f}%" for q in quotes))
    if s.get("vix"):
        parts.append(f"VIX {s['vix']['price']:.1f} {vix_label(s['vix']['price'])}")
    if s.get("kalshi"):
        parts.append("Kalshi up-close: " + " ".join(
            f"{'SPX' if 'S&P' in k['name'] else 'NDX' if 'Nasdaq' in k['name'] else k['name']} {round(k['prob'] * 100)}%"
            for k in s["kalshi"]))
    text = "Mkts: " + (" | ".join(parts) if parts else "unavailable right now")
    return text[:max_len]


def bot_sentiment(s: dict) -> str:
    parts = []
    if s.get("vix"):
        v = s["vix"]["price"]
        parts.append(f"VIX {v:.1f} {vix_label(v)}")
    if s.get("kalshi"):
        parts.append("Kalshi odds " + ", ".join(fmt_kalshi(k, short=True) for k in s["kalshi"]))
    return "Sentiment: " + ("; ".join(parts) if parts else "unavailable right now.")


def fmt_quote(q: dict, short: bool = False) -> str:
    sign = "+" if (q.get("change") or 0) >= 0 else ""
    chg = f"{sign}{q['change']:,.2f} ({sign}{q['pct']:.2f}%)" if q.get("change") is not None else "n/a"
    sym = (q.get("symbol") or "").replace("=F", "")
    if short:
        return f"{sym} {q['price']:,.2f} {chg}"
    rng = f"\nRange {q['low']:,.2f} - {q['high']:,.2f}" if q.get("low") and q.get("high") else ""
    return f"{q['name']}\n{q['price']:,.2f}  {chg}{rng}"


def bot_news(items: list[dict], kind: str, max_len: int) -> str:
    head = "Local news: " if kind == "local" else "World news: "
    if not items:
        return head + "no headlines right now."
    out = head
    for i, it in enumerate(items, 1):
        piece = f"{'' if i == 1 else ' | '}{it['title']}"
        if len(out) + len(piece) > max_len:
            if i == 1:
                out += piece[:max_len - len(out) - 1] + "~"
            break
        out += piece
    return out


def bot_markets(quotes: list[dict]) -> str:
    if not quotes:
        return "Markets: quotes unavailable right now."
    return "Futures (delayed): " + "; ".join(fmt_quote(q, short=True) for q in quotes)


service: NewsMarkets | None = None


def get_service(cfg: dict | None = None) -> NewsMarkets:
    global service
    if service is None:
        service = NewsMarkets(cfg)
    elif cfg is not None:
        service.configure(cfg)
    return service
