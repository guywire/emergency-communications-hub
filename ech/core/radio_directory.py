"""
ech/core/radio_directory.py
-----------------------------
O98: a directory of commonly-used HamVOIP/AllStarLink nodes (group, test,
audio) and AREDN mesh addresses/servers, surfaced in the UI with a
click-to-dial action (reuses the existing POST /api/pbx/call → originate()
path — no new dial mechanism needed), plus the full HOIP (Hams Over IP)
network directory the live PBX adapter is actually trunked to (IAX2 to
pbx-us1.hamsoverip.com) — test numbers, conference bridges, RF/AllStar
links, and audio entertainment/dispatch-monitor feeds.

Honesty note on scope: the 5 generic AllStarLink/HamVOIP "test"/parrot
nodes below are real, specific node numbers confirmed via public sources
(see each entry's `source`) — not guessed. Live "music"/jam node numbers
and any generic "group"/hub node are NOT included for THAT bucket: no
specific, confirmed-real, durable public number for either was found for
generic AllStarLink, and these are the kind of informal, community-specific
nodes where a wrong number silently reaches a stranger's station — worse
than leaving it blank.

The HOIP_* entries below are a different, stronger case: they're sourced
directly from HOIP's own wiki reference pages (hamsoverip.github.io/wiki/
reference/), fetched 2026-10-08 — conference-bridge-list, rf-links-list,
audio-feeds-list, test-numbers. HOIP is the network the live box is
actually peered with (confirmed via the asterisk/AMI adapter's IAX2 trunk
to pbx-us1.hamsoverip.com), so every one of these extensions is reachable
through the existing `_[1-9]X.` dialplan rule with no further config.
HOIP's own list changes over time — re-check hamsoverip.github.io/wiki/
before relying on an entry still being live.

AREDN has NO built-in entries at all: AREDN addresses
(`nodename.local.mesh`, 44.x.x.x) are private to each physical mesh network
with no universal public directory, so nothing here could possibly be a
real cross-deployment default.

All lists are meant to be extended via config.yaml with entries the
operator actually knows are good for their own region/mesh — see
`radio_directory:` in config.yaml.
"""

from __future__ import annotations

_HOIP_SOURCE_BASE = "hamsoverip.github.io/wiki/reference/{page}, fetched 2026-10-08"


def _mk(entries: list[tuple[str, str]], category: str, source_page: str,
        description: str) -> list[dict]:
    source = _HOIP_SOURCE_BASE.format(page=source_page)
    return [
        {"name": name, "destination": ext, "category": category,
         "description": description, "source": source}
        for ext, name in entries
    ]


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

# ── HOIP test phone numbers (hamsoverip.github.io/wiki/reference/test-numbers/) ──
_HOIP_TEST_RAW = [
    ("3191", "DTMF Test"),
    ("3192", "Talking Clock"),
    ("3194", "Echo Test"),
    ("3195", "Milliwatt"),
    ("3196", "3 Tone Slope"),
    ("3197", "Switch ID"),
    ("3198", "Extension ID"),
]
HOIP_TEST_ENTRIES = _mk(_HOIP_TEST_RAW, "hoip_test", "test-numbers/",
                         "HOIP network test/diagnostic number.")

# ── HOIP conference bridges, split by region block per HOIP's own numbering
# (1xxxx=US, 2xxxx=EU, 3xxxx=Asia-Pacific) — kept as separate categories so
# ~95 entries don't render as one unmanageable flat list in the UI ──
_HOIP_CONFERENCE_US_RAW = [
    ("10000", "HAMS OVER IP ADMINS"), ("10001", "PUBLIC 1"), ("10002", "PUBLIC 2"),
    ("10003", "PUBLIC 3"), ("10004", "PUBLIC 4"), ("10005", "PUBLIC 5"), ("10006", "PUBLIC 6"),
    ("10020", "THE GATHERING SPOT"), ("10021", "HURRICANE WATCH"), ("10022", "MOTOHAMS"),
    ("10023", "FLORIDA STORM"), ("10024", "ARVARF"), ("10025", "DIGICOMCAFE"),
    ("10027", "REGION 2 HAMS NET"), ("10028", "EASTERN US STORM"), ("10029", "REGION 1 HAMS NET"),
    ("10030", "GOPHER STATE HAMS"), ("10031", "NEW MEXICO STATEWIDE"), ("10032", "NM ARES"),
    ("10033", "MINNESOTA STATE"), ("10034", "REGION 3 HAMS NET"), ("10035", "REGION 4 HAMS NET"),
    ("10036", "REGION 5 HAMS NET"), ("10037", "REGION 6 HAMS NET"), ("10038", "REGION 7 HAMS NET"),
    ("10039", "REGION 8 HAMS NET"), ("10040", "REGION 9 HAMS NET"), ("10041", "REGION 0 HAMS NET"),
    ("10042", "NC STATEWIDE"), ("10043", "W5JDW AUXCOMM"), ("10044", "ELLIS COUNTY ARES"),
    ("10045", "PUERTO RICO"), ("10046", "DIGITAL YOUTH GROUP"), ("10047", "MIDDLE TN HAM"),
    ("10048", "KOOL KIDZ"), ("10049", "EAST TN HAMS"), ("10050", "TN STATEWIDE"),
    ("10051", "WEST TN HAMS"), ("10052", "ARVARF-VE"), ("10053", "DX TALK"),
    ("10054", "DCARES"), ("10055", "W0CDM-CONFRENCE"), ("10056", "POTA CHAT"),
    ("10057", "SE COLO EMCOMM"), ("10058", "COLORADO STATEWIDE"), ("10059", "THE HAMS HANGOUT"),
    ("10060", "MDC ARES EMCOMM"), ("10061", "JOTA/JOTI"), ("10062", "FL OPARC"),
    ("10063", "FL CLAY ARES"), ("10064", "R10 AFMARS"), ("10065", "CONVOCATION OF THE ANCIENTS"),
    ("10066", "PARC VE TESTING GROUP"), ("10067", "BM CANADA ADMINS"), ("10068", "SIMPLEX"),
    ("10069", "KENTUCKY STATEWIDE ARES"), ("10070", "FBIARC"), ("10071", "CPT DON'S SPACE ODYSSEY"),
    ("10072", "USHRC - N7HRC"), ("10073", "GRUMPY TECHS"), ("10074", "OHIO SEVERE WX"),
    ("10075", "HISPANOS NETWORK"), ("10387", "ET SKYWARN"),
]
_HOIP_CONFERENCE_EU_RAW = [
    ("20000", "PBX-EU ADMIN OPS"), ("20001", "PUBLIC EU 1"), ("20002", "PUBLIC EU 2"),
    ("20003", "PUBLIC EU 3"), ("20004", "HAMCAM INTERNATIONAL PUBLIC"), ("20022", "UK HAM RADIO"),
    ("20023", "DV SCOTLAND GROUP"), ("20024", "DV ELITE SPAIN"), ("20025", "FRANCE CNF VOIP"),
    ("20026", "USKSIDEARC"), ("20027", "CROSS COUNTIES ARC"),
]
_HOIP_CONFERENCE_AP_RAW = [
    ("30000", "HOIP ADMIN OPS"), ("30001", "AP PUBLIC 1"), ("30002", "AP PUBLIC 2"),
    ("30003", "AP PUBLIC 3"), ("30004", "AP PUBLIC 4"), ("30005", "AP PUBLIC 5"),
    ("30006", "OCEANIA"), ("30007", "SOUTHLAND AREC ADMIN OPS"),
]
HOIP_CONFERENCE_US_ENTRIES = _mk(_HOIP_CONFERENCE_US_RAW, "hoip_conference_us",
                                  "conference-bridge-list/", "HOIP conference bridge (US).")
HOIP_CONFERENCE_EU_ENTRIES = _mk(_HOIP_CONFERENCE_EU_RAW, "hoip_conference_eu",
                                  "conference-bridge-list/", "HOIP conference bridge (EU).")
HOIP_CONFERENCE_AP_ENTRIES = _mk(_HOIP_CONFERENCE_AP_RAW, "hoip_conference_ap",
                                  "conference-bridge-list/", "HOIP conference bridge (Asia-Pacific).")

# ── HOIP RF/AllStar links, same regional split (15xxx=US, 25xxx=EU, 35xxx=AP) ──
_HOIP_RF_LINK_US_RAW = [
    ("15000", "N8EMA ALLSTAR 49050"), ("15001", "EAST COAST REFLECTOR"),
    ("15002", "WH6AV ALLSTAR 42618"), ("15003", "KA1MXL RI DIGITAL LINK"),
    ("15004", "W8UFO ALLSTAR 41611"), ("15006", "KD7LMN RF LINK"),
    ("15007", "K4KSA STEERABLE NODE"), ("15008", "M7DPD ALLSTAR 57206"),
    ("15009", "NJ3H ALLSTAR 49246"), ("15010", "K8JTK ALLSTAR 50394"),
    ("15011", "KG4BHR ALLSTAR 520990"), ("15012", "W4EDP ALLSTAR 510131"),
    ("15013", "W4EDP/N4LMC ALLSTAR 46145"), ("15014", "W4EDP/W4GTA ALLSTAR 46331"),
    ("15015", "W4EDP/N4LMC ALLSTAR 46292"), ("15016", "KO4WIL ALLSTAR 56135"),
    ("15017", "HOIP ALLSTAR NODE 49649"), ("15018", "ALLSTAR 49230 BOREDOM BREAKERS NET"),
    ("15019", "KV4S ALLSTAR 523800"), ("15020", "KO4YWF ALlSTAR 57720"),
    ("15021", "F1PTL ALLSTAR 45107"), ("15022", "N4LMC/N4BZJ ALLSTAR 510135"),
    ("15023", "ALERT-K4NWS ALLSTAR 48168"), ("15024", "KD8JUS ALLSTAR 48292"),
    ("15025", "ALLSTAR 457430"), ("15026", "N4VKF ALLSTAR 528621"),
    ("15027", "W4EDP/N4LMC N4LMC 224.560 Allstar Node 46530"), ("15028", "K8LRC ALLSTAR 42121"),
    ("15029", "KE4TLC ALLSTAR 479250"), ("15030", "K2HZE ALLSTAR 41605"),
    ("15031", "K0NNK ALLSTAR 57686"), ("15032", "KZ4FOX ALLSTAR 547942"),
    ("15033", "N0EBB ALLSTAR 47734"), ("15034", "K9PSL ALLSTAR 431420"),
    ("15035", "J62DX ALLSTAR 1758"), ("15036", "K4YWE ALLSTAR 54341"),
    ("15037", "WA4KIK ALLSTAR 53812"), ("15038", "KM4ECM ALLSTAR 41557"),
    ("15039", "Murphy Radio Network (MRN)"), ("15040", "KG4ORQ ALLSTAR 58088"),
    ("15041", "WE0FUN ALLSTAR 28299"), ("15042", "N4LMC ALLSTAR 46077"),
    ("15043", "WX8NWS ALLSTAR 52374"), ("15045", "KC8NWS ALLSTAR 52251"),
    ("15046", "VE3WVJ ALLSTAR 40489"), ("15047", "WB0YLA ALLSTAR 409901"),
    ("15048", "AD9BU ALLSTAR 578101"), ("15049", "KD8LMI ALLSTAR 53113"),
    ("15050", "N9MS ALLSTAR 53619"), ("15051", "J73ESL ALLSTAR 56142"),
    ("15052", "N4UPC FIRESIDE RADIO NETWORK 52568"), ("15053", "KE8SEW WELCOME 500 572061"),
    ("15054", "WA4TAL ALLSTAR 59916"), ("15055", "N5SPJ ALLSTAR 60060"),
    ("15056", "K3FZT ALLSTAR 539932"), ("15057", "KJ7OMO ALLSTAR 2105"),
    ("15058", "K8LRC ALLSTAR 41001"), ("15059", "N2UGS ALLSTAR 52515"),
    ("15060", "NB4V ALLSTAR 578990"), ("15062", "W5DEL ALLSTAR 469100"),
    ("15063", "K2SHF ALLSTAR 547810"), ("15064", "W2NWT ALLSTAR 420674"),
    ("15065", "K6IRK ALLSTAR 61172"), ("15066", "VE9SC ALLSTAR 60796"),
    ("15067", "N1FTE ALLSTAR 564040"), ("15068", "WC8MI WHO CARES ARG HUB 594950"),
    ("15069", "W8CPT ALLSTAR 615120"), ("15070", "W8FU K8FBI REPEATER SYSTEM 43732"),
    ("15071", "K0AAJ ALLSTAR 42545"), ("15072", "KF0LPT XLX303-K SPECIAL K 58823"),
    ("15073", "KC3YWT ALLSTAR 62038"), ("15074", "KK7BSQ ALLSTAR 562011"),
    ("15075", "KO6AGZ ALLSTAR 61134"),
]
_HOIP_RF_LINK_EU_RAW = [
    ("25001", "DV SCOTLAND GROUP"), ("25002", "M7CWN ALLSTAR 56646"),
    ("25003", "ALLSTAR -TRANI CONFERENCE"), ("25004", "2E0LXY ALLSTAR 530470"),
    ("25005", "M0UKB EXTENDED FREEDOM NETWORK 23525"), ("25006", "M0JKT FREESTAR NETWORK 54073"),
    ("25007", "G8PY ALLSTAR 54775"), ("25008", "GM7KBK CQ-UK 54025"),
    ("25009", "M6KKW CQ-SUSSEX 53743"), ("25010", "M6RWW ALLSTAR 588130"),
    ("25011", "M7TLB ALLSTAR 550291"), ("25012", "ON7HH ALLSTAR 597120"),
    ("25013", "OK1SIM ALLSTAR 583521"), ("25014", "EA5JAV RADIOX 547489"),
    ("25015", "M7REI ALLSTAR 61408"), ("25057", "KJ7OMO ALLSTAR 2105"),
]
_HOIP_RF_LINK_AP_RAW = [
    ("35001", "VK2WAY FREEDMR AUSTRALIA GATEWAY 572110"), ("35002", "ZL2RO DVNZ NETWORK NZ 511781"),
    ("35003", "VK3VPN VK3RBA LINKED REPEATER SYSTEM 545441"),
    ("35004", "VK3VPN VK44NET TG61 HORSHAM LOCAL 50 56653"), ("35005", "4F0X ALLSTAR 41960"),
    ("35006", "VK3VPN VK44NET TG63 VK5SR-L 577351"), ("35007", "VK3VPN VK-OZHUB 61624"),
]
HOIP_RF_LINK_US_ENTRIES = _mk(_HOIP_RF_LINK_US_RAW, "hoip_rf_link_us",
                               "rf-links-list/", "HOIP-linked RF/AllStar node (US).")
HOIP_RF_LINK_EU_ENTRIES = _mk(_HOIP_RF_LINK_EU_RAW, "hoip_rf_link_eu",
                               "rf-links-list/", "HOIP-linked RF/AllStar node (EU).")
HOIP_RF_LINK_AP_ENTRIES = _mk(_HOIP_RF_LINK_AP_RAW, "hoip_rf_link_ap",
                               "rf-links-list/", "HOIP-linked RF/AllStar node (Asia-Pacific).")

# ── HOIP audio entertainment / dispatch-monitor feeds (no regional split in
# the source list — mixed countries/formats in one block) ──
_HOIP_AUDIO_FEED_RAW = [
    ("90002", "NEWSLINE CENTER"), ("90003", "VARIOUS AUDIO FEEDS"), ("90004", "REGGAE 1"),
    ("90005", "REGGAE 2"), ("90006", "TOP 80's 2"), ("90007", "MOTOWN"),
    ("90008", "EASY LISTENING"), ("90009", "TOP 80's"), ("90010", "BLUES"),
    ("90021", "HAWAII LOCAL HITS"), ("90022", "PRIDE RADIO"),
    ("90023", "ABC RADIO AUSTRALIA (ENGLISH FEED)"), ("90024", "SMOOTH JAZZ"),
    ("90025", "TOLEDO, OH POLICE DISPATCH"), ("90026", "LINUX IN THE HAM SHACK"),
    ("90027", "TRI-COUNTY SCANNER"), ("90028", "Art Bell Archives"),
    ("90029", "Ground Zero Radio"), ("90030", "Sci-Fi Radio"), ("90031", "Old Time Radio"),
    ("90032", "WTRSFM.COM Rock Radio"), ("90033", "Opie And Anthony"),
    ("90034", "BBC World Radio"), ("90035", "Kansas City Air Traffic"),
    ("90036", "St. Louis Area Marine"), ("90037", "Huntsville, AL Police Department"),
]
HOIP_AUDIO_FEED_ENTRIES = _mk(_HOIP_AUDIO_FEED_RAW, "hoip_audio_feed",
                               "audio-feeds-list/", "HOIP audio broadcast/dispatch-monitor feed.")

HOIP_ALL_ENTRIES = (
    HOIP_TEST_ENTRIES
    + HOIP_CONFERENCE_US_ENTRIES + HOIP_CONFERENCE_EU_ENTRIES + HOIP_CONFERENCE_AP_ENTRIES
    + HOIP_RF_LINK_US_ENTRIES + HOIP_RF_LINK_EU_ENTRIES + HOIP_RF_LINK_AP_ENTRIES
    + HOIP_AUDIO_FEED_ENTRIES
)

# No confirmed-real built-in AREDN addresses exist — see module docstring.
BUILTIN_AREDN_ENTRIES: list[dict] = []


def get_directory(config: dict) -> dict:
    """Merge built-in confirmed-real entries with operator-supplied ones
    from config.yaml's radio_directory: block. Operator entries are NOT
    validated (ECH has no way to confirm an arbitrary node/address is
    real/safe) — they're trusted as the operator's own knowledge."""
    cfg = config.get("radio_directory", {}) or {}
    hamvoip = list(BUILTIN_HAMVOIP_ENTRIES) + list(HOIP_ALL_ENTRIES) + list(cfg.get("hamvoip_entries", []) or [])
    aredn = list(BUILTIN_AREDN_ENTRIES) + list(cfg.get("aredn_entries", []) or [])
    return {"hamvoip": hamvoip, "aredn": aredn}
