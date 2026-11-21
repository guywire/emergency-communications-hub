"""News headlines and index futures: feed parsing, mixing sources, bot replies, phone pages."""
import xml.etree.ElementTree as ET

from ech.core import news_markets as nm
from ech.core import yealink_xml as yx

RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>X</title>
<item><title>Storm closes Route 1 in Rockport</title><description>&lt;p&gt;Crews &amp;amp; police on scene.&lt;/p&gt;</description>
<link>https://example.org/a</link><pubDate>Thu, 20 Nov 2026 10:00:00 -0500</pubDate></item>
<item><title>Harbor committee meets</title><pubDate>Wed, 19 Nov 2026 09:00:00 -0500</pubDate></item>
</channel></rss>"""

ATOM = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>World summit opens</title><summary>Leaders meet.</summary><link href="https://example.org/w"/>
<updated>2026-11-20T12:00:00Z</updated></entry>
<entry><title>Storm closes Route 1 in Rockport!</title><updated>2026-11-20T11:00:00Z</updated></entry>
</feed>"""

CHART = {"chart": {"result": [{"meta": {"symbol": "NQ=F", "regularMarketPrice": 21050.5,
                                        "chartPreviousClose": 21000.0, "regularMarketDayHigh": 21100.25,
                                        "regularMarketDayLow": 20950.0, "regularMarketTime": 1795170000}}]}}


def test_parse_rss_and_atom():
    rss = nm.parse_feed(RSS, "PenBay Pilot")
    assert rss[0]["title"] == "Storm closes Route 1 in Rockport"
    assert rss[0]["summary"] == "Crews & police on scene." and rss[0]["published"] > rss[1]["published"]
    atom = nm.parse_feed(ATOM, "BBC")
    assert atom[0]["link"] == "https://example.org/w" and atom[0]["summary"] == "Leaders meet."
    assert nm.parse_feed("not xml") == []


def test_double_escaped_html_is_cleaned():
    assert nm._text('&lt;span class="t"&gt;Rev. Libby&lt;/span&gt; &amp;amp; more') == "Rev. Libby & more"


def test_interleave_round_robin_and_dedupe():
    mixed = nm.interleave([nm.parse_feed(RSS, "A"), nm.parse_feed(ATOM, "B")])
    titles = [m["title"] for m in mixed]
    assert titles[0] == "Storm closes Route 1 in Rockport" and titles[1] == "World summit opens"
    assert "Storm closes Route 1 in Rockport!" not in titles          # same story, other source
    assert len(titles) == 3


def test_parse_chart_and_format():
    q = nm.parse_chart(CHART, "Nasdaq-100 futures (NQ)")
    assert q["change"] == 50.5 and round(q["pct"], 3) == 0.24
    assert nm.fmt_quote(q, short=True) == "NQ 21,050.50 +50.50 (+0.24%)"
    assert "Range 20,950.00 - 21,100.25" in nm.fmt_quote(q)
    assert nm.parse_chart({"chart": {"result": []}}, "x") is None


def test_bot_replies_fit_the_mesh_limit():
    items = [{"title": f"Headline number {i} about something", "published": 0} for i in range(10)]
    text = nm.bot_news(items, "world", 160)
    assert text.startswith("World news: ") and len(text) <= 160 and " | " in text
    long_one = nm.bot_news([{"title": "x" * 400, "published": 0}], "local", 160)
    assert len(long_one) <= 160 and long_one.startswith("Local news: ")
    assert nm.bot_news([], "local", 160) == "Local news: no headlines right now."
    q = nm.parse_chart(CHART, "NQ")
    assert nm.bot_markets([q]) == "Futures (delayed): NQ 21,050.50 +50.50 (+0.24%)"


KALSHI = [
    {"ticker": "KXINXDUD-26NOV21H1600-T7100", "status": "active", "close_time": "2026-11-21T21:00:00Z",
     "floor_strike": 7100.0, "yes_bid_dollars": "0.5000", "yes_ask_dollars": "0.6000", "last_price_dollars": "0.55",
     "volume_fp": "100.0"},
    {"ticker": "KXINXDUD-26NOV20H1600-T7175.89", "status": "active", "close_time": "2026-11-20T21:00:00Z",
     "floor_strike": 7175.89, "yes_bid_dollars": "0.2800", "yes_ask_dollars": "0.3200", "last_price_dollars": "0.3200",
     "volume_fp": "29319.59"},
]


def test_kalshi_uses_soonest_market_mid_price():
    k = nm.parse_kalshi(KALSHI, "S&P 500")
    assert k["ticker"].endswith("T7175.89") and round(k["prob"], 2) == 0.30
    assert nm.fmt_kalshi(k) == "S&P 500 closes up today: 30% (above 7,175.89), $29,320 traded"
    no_quotes = [{**KALSHI[1], "yes_bid_dollars": "0", "yes_ask_dollars": "0", "last_price_dollars": "0.41"}]
    assert nm.parse_kalshi(no_quotes, "x")["prob"] == 0.41
    assert nm.parse_kalshi([], "x") is None


def test_sentiment_and_combined_market_reply_fit():
    q = nm.parse_chart(CHART, "NQ")
    vix = {"price": 30.49}
    s = {"vix": vix, "kalshi": [nm.parse_kalshi(KALSHI, "S&P 500"), {"name": "Nasdaq-100", "prob": 0.22}]}
    assert nm.vix_label(12) == "calm" and nm.vix_label(30.49) == "high fear"
    assert nm.bot_sentiment(s) == "Sentiment: VIX 30.5 high fear; Kalshi odds S&P 500 up 30%, Nasdaq-100 up 22%"
    allm = nm.bot_markets_all([q, {**q, "symbol": "ES=F", "price": 7138.5, "pct": -0.82}], s, 160)
    assert allm == "Mkts: NQ 21,050 +0.2% ES 7,138 -0.8% | VIX 30.5 high fear | Kalshi up-close: SPX 30% NDX 22%"
    assert len(allm) <= 160
    scr = ET.fromstring(yx.markets_screen([q], nm.fmt_quote, s, nm.fmt_kalshi, nm.vix_label)).findtext("Text")
    assert "VIX 30.49 - high fear" in scr and "S&P 500 closes up today: 30%" in scr


def test_config_overrides_feeds_and_symbols():
    s = nm.NewsMarkets({"news": {"local": ["https://example.org/rss"]}, "markets": {"symbols": [["YM=F", "Dow"]]}})
    assert s.feeds["local"] == ["https://example.org/rss"] and s.feeds["world"] == nm.DEFAULT_FEEDS["world"]
    assert s.symbols == [("YM=F", "Dow")]


def test_phone_pages():
    base = "http://10.0.0.44:8766/yealink/K"
    items = nm.parse_feed(RSS, "PenBay Pilot")
    m = ET.fromstring(yx.news_menu(base, "local", items))
    assert m.find("MenuItem/URI").text == f"{base}/news/local/0.xml"
    scr = ET.fromstring(yx.news_item(items[0]))
    assert "Crews & police on scene." in scr.findtext("Text")
    mk = ET.fromstring(yx.markets_screen([nm.parse_chart(CHART, "Nasdaq-100 futures (NQ)")], nm.fmt_quote))
    assert "21,050.50" in mk.findtext("Text") and "not advice" in mk.findtext("Text")
    main = [i.findtext("URI") for i in ET.fromstring(yx.main_menu(base)).findall("MenuItem")]
    assert f"{base}/news/world.xml" in main and f"{base}/markets.xml" in main
