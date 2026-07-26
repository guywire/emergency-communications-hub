"""
ech/core/strip_templates.py
----------------------------
RI (Request for Information) strip format used by the SHARES Region 1
Interoperability Group's "Response Creator" tool — slant-separated field
strings like "NAME/ANSWER1/ANSWER2/.../". Field data ported verbatim from
Response_Creator.html's embedded EMBEDDED_TEMPLATES so output is
byte-compatible with what that tool (and anyone downstream expecting its
format, e.g. the GYX SKYWARN Google Sheet) already parses.

Strip anatomy: template_str.split("/") gives [name, q1, q2, ..., qN, "", ""]
— the trailing "//" always contributes two empty elements. Real fields are
`fields[1:-2]`; some (WXOBS) are literal "   " (three spaces) used as
visual row separators in the original HTML table, not real questions —
skip them when prompting, but keep the placeholder position when building
the answer string back out.
"""

from __future__ import annotations

import re

STRIP_TEMPLATES: dict[str, str] = {
    "GYX CAR SKYWARN": "GYX CAR SKYWARN/DATE (MM-DD-YYYY)/TIME (HHMML)/CALL SIGN/SPOTTER ID (or NA)/SOURCE (Amateur Radio, Trained Spotter, Media, Public Service Radio, Other 3rd Party, Direct Messaging)/LOCATION (Road, Town; MGRS or USNG optional)/STATE & CWA (e.g. NH GYX, ME CAR, MA OA)/CURRENT WEATHER (Relevant info such as Temp, Barometer, Cloud Cover or Type. Be brief.)/SNOW, SLEET (inches or NA; if storm total add ST e.g. 2.6 or 3.5ST)/ICE ACCRETION (inches or NA)/RAINFALL (inches or NA, if storm total add ST e.g. 2.6 or 3.5ST)/HAIL SIZE (Inches or NA)/WIND DIRECTION & SPEED (AAA MPH - if applicable add the gust value e.g. NNE 7 21G)/STORM DAMAGE (Wind, flooding, ice jams, other details)/MODE(Personal Observation, FM Repeater, Winlink, DMR, Direct Messaging. For others leave blank)/NET (Name of radio net or other e.g. email, Slack)//",
    "LOCALWX": "LOCALWX/CALL SIGN/SKYWARN ID ( leave blank if not applicable)/CITY/STATE/MGRS/NWS CWA (format AAA,NA)/DATE-TIME (UTC Zulu format)/WIND DIRECTION/WIND SPEED (in units of miles per hour)/GUST SPEED (in units of miles per hour)/CLOUDS (CLR,FEW,SKT,CB,OVC,TCU)/TEMPERATURE ( in degrees Fahrenheit)/BAROMETRIC PRESSURE (in units of inches of mercury)/BAROMETRIC PRESSURE 3 HOUR TREND (Rising, Steady, Falling)/PRECIPITATION TYPE (Rain, Snow, Sleat, Ice Pellets, Hail or None)/CURRENT PRECIPITATION (in units of inches)/STORM TOTAL PRECIPITATION (in units of inches)/LIQUID EQUIVALENT PRECIPITATION (in units of inches)/COMMENTS (brief information to help quantify the intensity of this event. Eye Witness comments of Flooding, Storm Surge, Damages, etc.)//",
    "SITREP": "SITREP/DATE TIME (DDHHMMZ MON YYYY)/CALL SIGN/RI ID (X)/CITY (X)/COUNTY (X)/STATE (AA)/MGRS (N)/DATA SOURCE (Personal Observation, Media, Public Safety, etc.)/AFFECTED AREA (X)/POTS LANDLINES (Y, N, Unk, NA)/CELLPHONE VOICE (Y, N, Unk, NA)/CELLPHONE TEXT (Y, N, Unk, NA)/AM & FM BROADCASTING (Y, N, Unk, NA)/OTA TV (Y, N, Unk, NA)/CABLE TV (Y, N, Unk, NA)/PUBLIC WATER WORKS (Y, N, Unk, NA)/COMMERCIAL POWER (Y, N, Unk, NA)/NATURAL GAS (Y, N, Unk, NA)/INTERNET (Y, N, Unk, NA)/NOAA WEATHER RADIO (Y, N, Unk, NA)/COMMENTS (250 characters max)//",
    "HURRICANE REPORT": "HURRICANE REPORT/DATE-TIME (UTC Zulu format)/REPORT STATUS (First Report, Update Report, Final Report)/CALL SIGN/REPORTING OBSERVER/REPORTING OBSERVER EMAIL/REPORTING OBSERVER PHONE NUMBER/CITY/COUNTY/STATE/COUNTRY/LATITUDE ( format ##.####N)/LONGITUDE ( format ###.####W)/MEASUREMENTS/LIST WEATHER INSTRUMENTS USED/WIND SPEED (in units of miles per hour)/GUST SPEED (in units of miles per hour)/WIND DIRECTION/BAROMETRIC PRESSURE (in units of inches of mercury)/COMMENTS (brief information to help quantify the intensity of this event. Eye Witness comments of Flooding, Storm Surge, Damages, etc.)//",
    "WXOBS": "WXOBS/CALL SIGN/SKYWARN ID (or NA)/CITY/STATE (AA)/MGRS (N)/NWS CWA (AAA,NA)/   /OBSERVATION TIME Z (DDHHMMZ)/   /WIND DIR (AAA)/AVE SPEED MPH (###)/GUSTS MPH (###)/   /CLOUDS (CLR,FEW,SKT,CB,OVC,TCU)/   /TEMP DEG F (###, -##)/   /BAROMETER MB (####.#)/BAROMETER 3 HR TREND (R,S,F)/   /PRECIP TYPE (RA,SN,SL,PL,GR,NONE)/CURRENT PRECIP INS (###.##,NA)/STORM TOTAL PRECIP INS (###.##,NA)/LIQUID EQUIV PRECIP INS (###.##,NA)/ /COMMENTS,DAMAGE//",
}

# Short word a user types over LoRa -> canonical STRIP_TEMPLATES key
TEMPLATE_ALIASES: dict[str, str] = {
    "skywarn": "GYX CAR SKYWARN", "gyx": "GYX CAR SKYWARN", "car": "GYX CAR SKYWARN",
    "localwx": "LOCALWX", "local": "LOCALWX",
    "sitrep": "SITREP",
    "hurricane": "HURRICANE REPORT", "hurricanereport": "HURRICANE REPORT",
    "wxobs": "WXOBS",
}


def extract_question_key(field_label: str) -> str:
    """'WIND SPEED (in mph)' -> 'WIND SPEED' — same rule as Response Creator's
    extractQuestionKey(), used to match profile fields and radiogram exceptions."""
    txt = field_label.strip()
    idx = txt.find("(")
    if idx > 0:
        txt = txt[:idx].strip()
    return txt.upper()


def raw_fields(template_name: str) -> list[str]:
    """All fields after the strip name, including blank/spacer rows, in order."""
    template = STRIP_TEMPLATES[template_name]
    parts = template.split("/")
    return parts[1:-2]   # drop name (index 0) and the two empty trailing splits from "//"


def guided_fields(template_name: str) -> list[tuple[str, str]]:
    """(question_key, prompt_label) pairs for real questions only — spacer
    rows (literal "   ") are skipped, matching the HTML tool's readonly rows."""
    out = []
    for label in raw_fields(template_name):
        if not label.strip():
            continue
        out.append((extract_question_key(label), label.strip()))
    return out


def identify_strip(candidate_name: str) -> str | None:
    """Case-insensitive match of a strip's first field against known templates."""
    want = candidate_name.strip().upper()
    for name in STRIP_TEMPLATES:
        if name.upper() == want:
            return name
    return None


def build_response_strip(template_name: str, answers: dict[str, str]) -> str:
    """Build 'NAME/ans1/ans2/.../' — blank/spacer fields and any unanswered
    question both fall back to '   ' (three spaces), matching createStrip()."""
    fields = raw_fields(template_name)
    parts = [template_name]
    for label in fields:
        key = extract_question_key(label)
        val = (answers.get(key) or "").strip() if label.strip() else ""
        parts.append(val if val else "   ")
    return "/".join(parts) + "/"


def latlon_to_mgrs(lat: float, lon: float, precision: int = 2) -> str | None:
    """WGS84 lat/lon -> MGRS grid reference (e.g. '19TDJ9172' at precision=2).

    Uses the `mgrs` package (NGA GeoTrans, ships prebuilt wheels — no compiler
    needed on common platforms) rather than a hand-rolled UTM/grid-lettering
    implementation: this feeds real emergency reports, and a coordinate
    conversion with a subtle sign/offset bug that "looks right" is worse
    than the feature plainly being unavailable. Returns None if the `mgrs`
    package isn't installed or the conversion fails (e.g. out of UTM range).
    precision: digits per axis — 1=10km, 2=1km, 3=100m, 4=10m, 5=1m.
    """
    try:
        import mgrs as _mgrs
    except ImportError:
        return None
    try:
        return _mgrs.MGRS().toMGRS(lat, lon, MGRSPrecision=precision)
    except Exception:
        return None


_LATLON_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$")


def resolve_mgrs_answer(text: str) -> str:
    """For the guided form's MGRS field: if the operator typed 'lat,lon',
    auto-convert; otherwise pass through whatever they typed as-is (an
    already-known MGRS string, 'NA', etc.) — never guess a position for
    them, only convert coordinates they explicitly supplied."""
    m = _LATLON_RE.match(text)
    if not m:
        return text
    lat, lon = float(m.group(1)), float(m.group(2))
    grid = latlon_to_mgrs(lat, lon)
    if grid:
        return grid
    return text + " [MGRS conversion unavailable — mgrs package not installed]"


def parse_response_strip(template_name: str, response_text: str) -> dict[str, str]:
    """Parse a pasted 'NAME/ans1/ans2/.../' string into {question_key: answer},
    skipping spacer positions and blank/whitespace-only values."""
    text = response_text.strip()
    if text.endswith("//"):
        text = text[:-2]
    values = text.split("/")[1:]   # drop the name field
    fields = raw_fields(template_name)
    answers: dict[str, str] = {}
    for label, val in zip(fields, values):
        if not label.strip():
            continue
        val = val.strip()
        if val:
            answers[extract_question_key(label)] = val
    return answers


# ── Radiogram transliteration (WXOBS only) — ported from Response Creator's
# formatRadiogram()/unformatRadiogram(): only A-Z, 0-9, and "/" survive
# transmission as formal radiogram traffic. ─────────────────────────────────

_NO_CAMEL = {"CALL SIGN", "SKYWARN ID", "NWS CWA", "MGRS",
             "STRIP NAME", "WIND DIR", "WIND DIRECTION", "STATE & CWA"}
_EMAIL_RE = re.compile(r"([a-zA-Z0-9._+-]+)@([a-zA-Z0-9.-]+)")
_NEG_RE = re.compile(r"(^|\s)-(\d)")
_DECIMAL_RE = re.compile(r"(\d)\.(\d)")
_NON_RADIOGRAM_RE = re.compile(r"[^A-Z0-9\s/]")
_MULTI_SPACE_RE = re.compile(r"\s{2,}")


def format_radiogram(text: str) -> str:
    if not text:
        return text
    t = _EMAIL_RE.sub(lambda m: f"{m.group(1)} ATSIGN {' DOT '.join(m.group(2).split('.'))}", text)
    t = _NEG_RE.sub(r"\1M\2", t)
    t = _DECIMAL_RE.sub(r"\1R\2", t)
    t = re.sub(r"\.\s*$", "", t)
    t = t.replace(".", " X ")
    t = t.replace("-", " DASH ")
    t = t.replace(",", " COMMA ")
    t = t.upper()
    t = _NON_RADIOGRAM_RE.sub("", t)
    t = _MULTI_SPACE_RE.sub(" ", t).strip()
    return t


def unformat_radiogram(text: str, question_key: str) -> str:
    if not text or not text.strip():
        return text
    t = text
    t = re.sub(r"(\w+)\s+ATSIGN\s+([\w\s]+)",
               lambda m: (m.group(1) + "@" + re.sub(r"\s+DOT\s+|\bDOT\b", ".", m.group(2).strip(), flags=re.I)).lower(),
               t, flags=re.I)
    t = re.sub(r"(^|\s)M(\d)", r"\1-\2", t)
    t = re.sub(r"(\d)R(\d)", r"\1.\2", t)
    t = re.sub(r"\s+DASH\s+|\bDASH\b", "-", t, flags=re.I)
    t = re.sub(r"\s+COMMA\s+|\bCOMMA\b", ",", t, flags=re.I)
    if question_key not in _NO_CAMEL:
        t = t.replace(" X ", ". ")
        if t.endswith(" X"):
            t = t[:-2] + "."
        def _camel(m):
            word = m.group(0)
            if any(c.isdigit() for c in word) or word == "NA":
                return word
            return word[0] + word[1:].lower()
        t = re.sub(r"\b([A-Z])([A-Z]+)\b", _camel, t)
    return t.strip()
