"""
ech/core/winlink_forms.py
--------------------------
Winlink message templates (O82, O127).

Scope note — what this IS and ISN'T: the real Winlink Forms Catalog (RMS
Express/Winlink Express "Standard Templates") is an interactive HTML form
system — a template renders an input form, the filled-in data round-trips as
a compact XML attachment, and a matching display-form template renders it
back for the recipient. That system's spec, template files, and a compatible
renderer were not something this pass could responsibly build and claim
correct without testing against real Winlink Express/RMS Express — getting
the XML schema subtly wrong would silently produce messages real Winlink
clients can't render, which is worse than not having forms at all.

What's implemented instead is the plain-text fallback operators already use
in practice when they don't have the interactive form installed: these are
real, standard field layouts (FEMA ICS-213, ICS-214A, ARRL Radiogram, SHARES
Region 1 RI strips) rendered as ordinary message text with a clear Subject
line — readable by ANY Winlink client or plain email, at the cost of not
being a binary-identical rendered form on the receiving end.

Validation (O127): every field has a `kind` (callsign, number, choice,
pattern, …) checked server-side before a message is rendered, and
validate_template() returns per-field errors so the UI can show them next
to the field. SHARES strips are generated from ech/core/strip_templates.py
(ported verbatim from the SHARES Region 1 "Response Creator" tool) and each
field's own format hint — "(Y, N, Unk, NA)", "STATE (AA)", "(DDHHMMZ MON
YYYY)", "(250 characters max)", … — becomes its validation rule. A "/" in
any SHARES answer is rejected: the strip is slash-delimited, so one stray
slash would silently shift every following answer into the wrong field.

Two more real, confirmed (not guessed) mechanisms that ARE plain messages,
unrelated to the Forms Catalog above:

"AI request" (ai_help): confirmed by the operator against a real sent
message (MID OJELPRVWCCRG, 2026-10-08) — just a plain message addressed
To: AIHELP, Subject: AI, body is the question as free text.

"Winlink Catalog Request" (catalog_request) — a different, older, and
separate system from AI Query and from Forms, despite the similar name:
request a standard bulletin (weather text, propagation, satellite imagery,
radar, nets schedules, etc.) by sending To: INQUIRY, Subject: REQUEST, body
is the catalog's "Inquiry ID" (or the literal word LIST to have Winlink mail
back its current full catalog). Confirmed via multiple independent
third-party Winlink training/drill documents (search, not primary-sourced
against winlink.org directly — no official spec page was found). The reply
arrives from Source=SYSTEM/Sender=SERVICE with a Subject starting
"INQUIRY:". The preset Inquiry IDs offered in catalog_request's field below
(METAR, WX_ATLANTIC, USWXRAD.GIF for radar, PROPAGATION, SAT_PIX, etc.) come
from a real screenshot of Winlink Express's own Query Catalog dialog (via
Brunswick County Emcomm Net exercise #022's briefing PDF) — they're the
actual category names, not invented ones, but the full catalog is larger
and changes over time; LIST is the honest way to get the current list,
since there's no synchronous API to fetch it through ECH (the reply is
mail, not an instant HTTP response — it shows up on your next connect).
"""

from __future__ import annotations

import json
import math
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ech.core.strip_templates import (
    STRIP_TEMPLATES, build_response_strip, extract_question_key, guided_fields,
    resolve_mgrs_answer,
)


@dataclass
class TemplateField:
    key: str
    label: str
    required: bool = False
    placeholder: str = ""
    # Suggested values (rendered as a datalist — free text is still allowed,
    # these are suggestions, not an enforced enum) for fields like a catalog
    # Inquiry ID where a fixed, confirmed-real set of common values exists.
    options: list[str] = field(default_factory=list)
    # Validation (O127). kind: text | multiline | callsign | number | choice |
    # pattern | email | mgrs | timelog. `choices` is ENFORCED (rendered as a
    # select), unlike `options`. `pattern` is a full-match regex with a
    # human hint in `hint`. min/max apply to kind="number".
    kind: str = "text"
    choices: list[str] = field(default_factory=list)
    pattern: str = ""
    hint: str = ""
    max_len: int = 0
    min: float | None = None
    max: float | None = None
    allow_na: bool = False          # also accept "NA"
    no_slash: bool = False          # SHARES strips: "/" is the field separator


@dataclass
class Template:
    id: str
    name: str
    description: str
    fields: list[TemplateField] = field(default_factory=list)
    # Fixed recipient for templates addressed to a well-known Winlink service
    # (e.g. AIHELP) — None for the general-purpose templates where the
    # operator picks the recipient themselves.
    to_address: str | None = None
    group: str = "General"


def _utc_now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%MZ")


# ── Field validation ─────────────────────────────────────────────────────────

# ITU-style amateur callsign, optional /suffix or prefix/ (e.g. W1AW, KN0O-9,
# VE3ABC/P, EA8/G4ABC, ON5VL/P).
_CALLSIGN_RE = re.compile(r"(?:[A-Z0-9]{1,4}/)?[A-Z0-9]{1,3}[0-9][A-Z0-9]{0,4}[A-Z](?:-\d{1,2})?(?:/[A-Z0-9]{1,4})?")
_EMAIL_RE = re.compile(r"[^@\s/<>()]+@[^@\s/<>()]+\.[A-Za-z]{2,}")
_MGRS_RE = re.compile(r"\d{1,2}[C-HJ-NP-X][A-HJ-NP-Z]{2}(?:\d\d){0,5}")
_NUMBER_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")
_TIMELOG_LINE_RE = re.compile(r"(\d{4})Z?\s+\S.*")


def _check(f: TemplateField, raw: str) -> str | None:
    """Error message for one field value, or None if valid. Empty optional
    fields are valid; required-ness is checked by the caller."""
    v = (raw or "").strip()
    if not v:
        return None
    if f.no_slash and "/" in v:
        return 'must not contain "/" (it separates SHARES strip fields)'
    if f.max_len and len(v) > f.max_len:
        return f"must be {f.max_len} characters or fewer (is {len(v)})"
    if f.allow_na and v.upper() == "NA":
        return None
    k = f.kind
    if k == "callsign":
        if not _CALLSIGN_RE.fullmatch(v.upper()):
            return "doesn't look like a callsign (e.g. KN0O, W1AW-9, VE3ABC/P)"
    elif k == "email":
        if not _EMAIL_RE.fullmatch(v):
            return "enter a valid email address"
    elif k == "number":
        if not _NUMBER_RE.fullmatch(v):
            return "must be a number" + (" or NA" if f.allow_na else "")
        n = float(v)
        if f.min is not None and n < f.min:
            return f"must be at least {f.min:g}"
        if f.max is not None and n > f.max:
            return f"must be at most {f.max:g}"
    elif k == "choice":
        if v.upper() not in {c.upper() for c in f.choices}:
            return "must be one of: " + ", ".join(f.choices)
    elif k == "pattern":
        if not re.fullmatch(f.pattern, v, re.IGNORECASE):
            return f"format should be {f.hint}" if f.hint else "invalid format"
    elif k == "mgrs":
        conv = resolve_mgrs_answer(v)            # "lat,lon" → MGRS when possible
        if not _MGRS_RE.fullmatch(conv.replace(" ", "").upper()):
            return "enter an MGRS grid (e.g. 19TDJ9172) or lat,lon (e.g. 44.16,-69.12)"
    elif k == "multichoice":
        allowed = {c.upper() for c in f.choices}
        bad = [x for x in v.split("|") if x.strip() and x.strip().upper() not in allowed]
        if bad:
            return f'"{bad[0].strip()[:40]}" is not one of the listed options'
    elif k == "timelog":
        bad = [ln for ln in v.splitlines() if ln.strip() and not _TIMELOG_LINE_RE.fullmatch(ln.strip())]
        if bad:
            return f'each line must start with a 4-digit time, e.g. "0830 Arrived at EOC" (bad: "{bad[0][:30]}")'
        for ln in v.splitlines():
            m = _TIMELOG_LINE_RE.fullmatch(ln.strip())
            if m and not (int(m.group(1)[:2]) < 24 and int(m.group(1)[2:]) < 60):
                return f'"{m.group(1)}" is not a valid 24-hour time'
    return None


def validate_template(template_id: str, values: dict) -> dict[str, str]:
    """{field_key: error} for every invalid field (empty dict = valid).
    Raises KeyError for an unknown template."""
    tmpl = TEMPLATES[template_id]
    errors: dict[str, str] = {}
    for f in tmpl.fields:
        v = str(values.get(f.key) or "").strip()
        if f.required and not v:
            errors[f.key] = "is required"
            continue
        err = _check(f, v)
        if err:
            errors[f.key] = err
    if template_id == "usgs_dyfi":
        errors.update(_dyfi_cross_check(values, errors))
    # Cross-field: the reference format depends on which program was chosen
    if template_id == "ota_report" and "reference" not in errors:
        rule = _OTA_REF_RULES.get(str(values.get("program") or "").strip())
        ref = str(values.get("reference") or "").strip().upper()
        if rule and ref and not re.fullmatch(rule[0], ref):
            errors["reference"] = f"doesn't look like a {values.get('program')} reference ({rule[1]})"
    return errors


# ── SHARES Region 1 RI strips (generated from strip_templates) ───────────────

_DIRS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW",
         "W", "WNW", "NW", "NNW", "VRB"]


def _shares_field(qkey: str, label: str) -> TemplateField:
    """Turn one Response-Creator label (which carries its own format hint)
    into a validated field."""
    L = label.upper()
    key = re.sub(r"[^a-z0-9]+", "_", qkey.lower()).strip("_")
    f = TemplateField(key, label.strip(), no_slash=True, placeholder=label.strip())
    hint = L[L.find("("):] if "(" in L else ""
    yn = re.search(r"\(Y, N, UNK, NA\)", L)
    if yn:
        f.kind, f.choices = "choice", ["Y", "N", "Unk", "NA"]
    elif qkey == "CALL SIGN":
        f.kind, f.required = "callsign", True
    elif "EMAIL" in L:
        f.kind = "email"
    elif qkey.startswith("STATE") and "CWA" not in L:
        f.kind, f.pattern, f.hint = "pattern", r"[A-Z]{2}", "2-letter state, e.g. ME"
    elif "STATE & CWA" in L:
        f.kind, f.pattern, f.hint = "pattern", r"[A-Z]{2} [A-Z]{3}", "state + CWA, e.g. ME GYX"
    elif "CWA" in L:
        f.kind, f.pattern, f.hint, f.allow_na = "pattern", r"[A-Z]{3}", "3-letter NWS office, e.g. GYX", True
    elif "MGRS" in L and "OPTIONAL" not in L:
        f.kind, f.allow_na = "mgrs", True
    elif "MM-DD-YYYY" in L:
        f.kind, f.pattern, f.hint, f.required = "pattern", r"(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])-20\d\d", "MM-DD-YYYY", True
    elif "HHMML" in L:
        f.kind, f.pattern, f.hint = "pattern", r"([01]\d|2[0-3])[0-5]\dL", "HHMM local + L, e.g. 1430L"
    elif "DDHHMMZ MON YYYY" in L:
        f.kind, f.pattern, f.hint, f.required = ("pattern", r"(0[1-9]|[12]\d|3[01])([01]\d|2[0-3])[0-5]\dZ [A-Z]{3} 20\d\d",
                                                 "DDHHMMZ MON YYYY, e.g. 111630Z OCT 2026", True)
    elif "DDHHMMZ" in L:
        f.kind, f.pattern, f.hint, f.required = "pattern", r"(0[1-9]|[12]\d|3[01])([01]\d|2[0-3])[0-5]\dZ", "DDHHMMZ, e.g. 111630Z", True
    elif "UTC ZULU" in L:
        f.kind, f.pattern, f.hint, f.required = ("pattern", r"(\d{6}Z( [A-Z]{3} 20\d\d)?|20\d\d-\d\d-\d\d[ T]\d\d:?\d\dZ)",
                                                 "UTC, e.g. 111630Z or 2026-10-11 16:30Z", True)
    elif "LATITUDE" in L:
        f.kind, f.pattern, f.hint = "pattern", r"\d{1,2}\.\d{4}[NS]", "##.####N, e.g. 44.1620N"
    elif "LONGITUDE" in L:
        f.kind, f.pattern, f.hint = "pattern", r"\d{1,3}\.\d{4}[EW]", "###.####W, e.g. 069.1210W"
    elif "CLOUDS" in L:
        f.kind, f.choices = "choice", ["CLR", "FEW", "SKT", "CB", "OVC", "TCU"]
    elif "TREND (R,S,F)" in L:
        f.kind, f.choices = "choice", ["R", "S", "F"]
    elif "TREND" in L:
        f.kind, f.choices = "choice", ["Rising", "Steady", "Falling"]
    elif "PRECIP TYPE (RA" in L:
        f.kind, f.choices = "choice", ["RA", "SN", "SL", "PL", "GR", "NONE"]
    elif "PRECIPITATION TYPE" in L:
        f.kind, f.choices = "choice", ["Rain", "Snow", "Sleet", "Ice Pellets", "Hail", "None"]
    elif "REPORT STATUS" in L:
        f.kind, f.choices = "choice", ["First Report", "Update Report", "Final Report"]
    elif "WIND DIR" in L and "SPEED" not in L:
        f.kind, f.choices = "choice", _DIRS
    elif "WIND DIRECTION & SPEED" in L:
        f.kind, f.pattern, f.hint, f.allow_na = ("pattern", r"(N|NNE|NE|ENE|E|ESE|SE|SSE|S|SSW|SW|WSW|W|WNW|NW|NNW|VRB) \d{1,3}( \d{1,3}G)?",
                                                 "DIR MPH [GUST G], e.g. NNE 7 21G", True)
    elif "STORM TOTAL" in L or ("(INCHES OR NA" in L and "ST" in L):
        # GYX snow/rain: inches, optional ST suffix for storm totals
        f.kind, f.pattern, f.hint, f.allow_na = "pattern", r"\d{1,3}(\.\d{1,2})?(ST)?", "inches, e.g. 2.6 or 3.5ST, or NA", True
    elif "MERCURY" in L:                 # before the generic "inches" rule
        f.kind, f.min, f.max = "number", 25, 32.5
    elif "INCH" in L or " INS " in L or L.endswith(" INS"):
        f.kind, f.allow_na, f.min, f.max = "number", True, 0, 999
    elif "MILES PER HOUR" in L or "MPH" in L:
        f.kind, f.allow_na, f.min, f.max = "number", True, 0, 300
    elif "FAHRENHEIT" in L or "DEG F" in L:
        f.kind, f.min, f.max = "number", -80, 140
    elif "BAROMETER MB" in L:
        f.kind, f.min, f.max = "number", 850, 1100
    m = re.search(r"(\d+) CHARACTERS MAX", L)
    if m:
        f.max_len = int(m.group(1))
    if f.kind in ("text",) and not f.max_len:
        f.max_len = 500
    if hint and not f.hint:
        f.hint = hint
    return f


_SHARES_INFO = {
    "GYX CAR SKYWARN": ("SHARES / SKYWARN — GYX & CAR spotter report", "SKYWARN storm report for NWS Gray (GYX) / Caribou (CAR)."),
    "LOCALWX": ("Local Weather Report (SHARES LOCALWX)", "Local weather observation in the SHARES Region 1 LOCALWX strip."),
    "SITREP": ("SHARES SITREP — infrastructure status", "Situation report: which communications/utilities are working (Y/N/Unk/NA)."),
    "HURRICANE REPORT": ("SHARES Hurricane Report", "Observer hurricane report (first/update/final) with position and measurements."),
    "WXOBS": ("SHARES WXOBS — weather observation", "Compact coded weather observation (WXOBS strip)."),
}


def _shares_templates() -> dict[str, Template]:
    out = {}
    for name in STRIP_TEMPLATES:
        title, desc = _SHARES_INFO.get(name, (f"SHARES {name}", ""))
        tid = "shares_" + re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
        out[tid] = Template(
            id=tid, name=title, group="SHARES Region 1",
            description=(desc + " Builds the slant-separated RI strip exactly as the SHARES Region 1 "
                         "Response Creator does (NAME/answer/answer/…//) — send it to the address "
                         "your RI or net asked for. Unanswered fields become blanks, as in the tool."),
            fields=[_shares_field(q, label) for q, label in guided_fields(name)],
        )
        out[tid]._strip = name        # type: ignore[attr-defined]
    return out


# ── Other templates ──────────────────────────────────────────────────────────

_OTA_PROGRAMS = ["POTA", "SOTA", "WWFF", "IOTA", "BOTA (Bunkers)", "Beaches on the Air", "COTA (Castles)",
                 "ILLW / ARLHS (Lighthouses)", "LLOTA (Lakes & Lagoons)", "HSOTA (Historical Sites)", "Other"]
# Reference formats for the programs where one is well established; others are free text.
_OTA_REF_RULES = {
    "POTA": (r"[A-Z0-9]{1,4}-\d{4,5}", "e.g. US-2664"),
    "SOTA": (r"[A-Z0-9]{1,4}/[A-Z]{2}-\d{3}", "e.g. W1/HA-126"),
    "WWFF": (r"[A-Z0-9]{1,4}FF-\d{4}", "e.g. KFF-1893"),
    "IOTA": (r"(AF|AN|AS|EU|NA|OC|SA)-\d{3}", "e.g. NA-026"),
    "BOTA (Bunkers)": (r"(B/[A-Z0-9]{1,3}-\d{3,5}|[A-Z0-9]{1,3}/B-\d{3,5})", "e.g. B/G-0946"),
}
_MODES = ["SSB", "CW", "FM", "AM", "FT8", "FT4", "RTTY", "PSK31", "JS8", "DIGI", "Other"]


TEMPLATES: dict[str, Template] = {
    "ics213": Template(
        id="ics213",
        name="ICS-213 General Message",
        description="FEMA/ICS standard general message form — the plain-text layout "
                     "operators use when sending without the interactive Winlink form.",
        group="ICS",
        fields=[
            TemplateField("to_name", "To (Name)", required=True),
            TemplateField("to_position", "To (Position/Agency)"),
            TemplateField("from_name", "From (Name)", required=True),
            TemplateField("from_position", "From (Position/Agency)"),
            TemplateField("subject", "Subject", required=True, max_len=120),
            TemplateField("message", "Message", required=True, placeholder="Multi-line message body", kind="multiline"),
        ],
    ),
    "ics214a": Template(
        id="ics214a",
        name="ICS-214A Individual Log",
        description="FEMA ICS 214a Individual Log: what one person did during an operational "
                     "period. One activity per line, each starting with a 24-hour time "
                     "(e.g. \"0830 Checked in at EOC\").",
        group="ICS",
        fields=[
            TemplateField("incident_name", "1. Incident Name", required=True, max_len=100),
            TemplateField("period_from", "2. Operational Period — From", required=True, kind="pattern",
                          pattern=r"20\d\d-\d\d-\d\d \d\d:?\d\d", hint="YYYY-MM-DD HHMM", placeholder="2026-10-11 0800"),
            TemplateField("period_to", "2. Operational Period — To", required=True, kind="pattern",
                          pattern=r"20\d\d-\d\d-\d\d \d\d:?\d\d", hint="YYYY-MM-DD HHMM", placeholder="2026-10-11 2000"),
            TemplateField("individual_name", "3. Individual Name", required=True, max_len=80),
            TemplateField("ics_section", "4. ICS Section", kind="choice",
                          choices=["Command", "Operations", "Planning", "Logistics", "Finance/Admin"]),
            TemplateField("assignment", "5. Assignment / Location", max_len=120),
            TemplateField("activity_log", "6. Activity Log (one per line: HHMM activity)", required=True,
                          kind="timelog", placeholder="0800 Checked in at EOC\n0815 Net control on 147.000"),
            TemplateField("prepared_by", "7. Prepared By (name / callsign)", required=True, max_len=80),
        ],
    ),
    "radiogram": Template(
        id="radiogram",
        name="ARRL Radiogram",
        description="Standard ARRL/NTS radiogram layout: preamble (number, precedence, "
                     "station of origin, check, place/time filed) + address + text + signature.",
        group="General",
        fields=[
            TemplateField("number", "Message Number", kind="number", min=1, max=99999),
            TemplateField("precedence", "Precedence", placeholder="R(outine) / W(elfare) / P(riority) / E(mergency)",
                          kind="choice", choices=["R", "W", "P", "EMERGENCY"]),
            TemplateField("station_of_origin", "Station of Origin", kind="callsign"),
            TemplateField("place_of_origin", "Place of Origin", max_len=60),
            TemplateField("to_address", "To (Name/Address)", required=True),
            TemplateField("text", "Text", required=True, placeholder="Radiogram message text (25 words or fewer is customary)",
                          kind="multiline"),
            TemplateField("signature", "Signature", required=True, max_len=80),
        ],
    ),
    "local_wx_report": None,      # placeholder — filled from SHARES LOCALWX below (keeps menu order)
    "ota_report": Template(
        id="ota_report",
        name="On-the-Air Spot / Hunter Report (POTA, SOTA, WWFF, …)",
        description="Off-grid activation spot or hunter contact for POTA, SOTA, WWFF, IOTA, BOTA, "
                     "castles, lighthouses, etc. — formatted like a cluster spot so whoever receives "
                     "it (a spotting buddy, club, or net) can post or log it. Winlink does NOT post "
                     "this to the POTA/SOTA spotting sites by itself; send it to someone who can.",
        group="On the Air",
        fields=[
            TemplateField("report_type", "Report type", required=True, kind="choice",
                          choices=["Spot (activator on the air)", "Self-spot (I'm activating)",
                                   "Hunter contact (I worked them)"]),
            TemplateField("program", "Program", required=True, kind="choice", choices=_OTA_PROGRAMS),
            TemplateField("reference", "Reference", required=True, max_len=20,
                          placeholder="US-2664, W1/HA-126, KFF-1893, NA-026, B/G-0946…"),
            TemplateField("activator", "Activator callsign", required=True, kind="callsign"),
            TemplateField("frequency_khz", "Frequency (kHz)", required=True, kind="number", min=1800, max=450000,
                          placeholder="14244"),
            TemplateField("mode", "Mode", required=True, kind="choice", choices=_MODES),
            TemplateField("time_utc", "Time (UTC, HHMM)", required=True, kind="pattern",
                          pattern=r"([01]\d|2[0-3])[0-5]\dZ?", hint="HHMM UTC, e.g. 1640"),
            TemplateField("rst_sent", "RST sent", kind="pattern", pattern=r"[1-5][1-9][1-9]?|[+-]\d{1,2}",
                          hint="59, 599 or a dB report like -12"),
            TemplateField("rst_rcvd", "RST received", kind="pattern", pattern=r"[1-5][1-9][1-9]?|[+-]\d{1,2}",
                          hint="59, 599 or a dB report like -12"),
            TemplateField("comments", "Comments", max_len=120, placeholder="e.g. QRT at 1700Z, park-to-park welcome"),
        ],
    ),
    "catalog_request": Template(
        id="catalog_request",
        name="Winlink Catalog Request",
        description="Request a standard bulletin (weather, propagation, satellite imagery, "
                     "radar, nets, etc.) from Winlink's catalog system — To: INQUIRY, Subject: "
                     "REQUEST, body is the Inquiry ID. Confirmed real via multiple independent "
                     "sources (Winlink training materials, ARES/RACES drill docs): reply arrives "
                     "from Source=SYSTEM/Sender=SERVICE with Subject starting \"INQUIRY:\". Pick "
                     "'LIST' to have Winlink send back its current full catalog — the closest "
                     "thing to a live-updating list this request/response-over-RF system allows; "
                     "the reply shows up in your inbox on your next connect, not instantly.",
        to_address="INQUIRY",
        group="Winlink services",
        fields=[
            TemplateField(
                "inquiry_id", "Inquiry ID", required=True, max_len=60,
                placeholder="e.g. METAR KPWM, WX_ATLANTIC, USWXRAD.GIF, or LIST for the full catalog",
                options=[
                    "LIST",
                    "METAR",
                    "PROPAGATION",
                    "USWXRAD.GIF",
                    "WX_ATLANTIC",
                    "WX_ARCTIC",
                    "WX_AK_COAST",
                    "S/PACIFIC_WX",
                    "ARES_RACES",
                    "HF_NETS",
                    "AURORA",
                    "ARCTIC_ICE",
                    "SAT_KEPS",
                    "SAT_PIX",
                    "NEWS",
                    "WL2K_HELP",
                    "WL2K_RMS",
                    "WL2K_TERMS",
                    "WL2K_USERS",
                ],
            ),
        ],
    ),
    "ai_help": Template(
        id="ai_help",
        name="Winlink AI Help Request",
        description="Plain message to Winlink's AIHELP service — To: AIHELP, Subject: AI, "
                     "body is your question as free text. This is the same request Winlink "
                     "Express's own Settings → Winlink Catalog Requests → \"AI Query\" button "
                     "generates under the hood (confirmed against both a real sent message, "
                     "MID OJELPRVWCCRG, and the Brunswick County Emcomm Net's exercise #022 "
                     "briefing). Limits per that briefing: 10 queries/callsign/day, text-only "
                     "replies (no web links or photos, so it's usable over VHF/HF RF, not just "
                     "Telnet) — reply turnaround depends on the AIHELP service itself, not ECH.",
        to_address="AIHELP",
        group="Winlink services",
        fields=[
            TemplateField("query", "Question", required=True, kind="multiline", max_len=2000,
                          placeholder="e.g. Which hospitals in Maine share bed status, and what is it right now?"),
        ],
    ),
    "sitrep": Template(
        id="sitrep",
        name="Situation Report (SITREP)",
        description="Free-form emergency situation report — not a Winlink-standard "
                     "template, but a common ARES/RACES field-report layout.",
        group="General",
        fields=[
            TemplateField("location", "Location", required=True, max_len=120),
            TemplateField("situation", "Situation", required=True, placeholder="What's happening", kind="multiline"),
            TemplateField("actions_taken", "Actions Taken", kind="multiline"),
            TemplateField("resources_needed", "Resources Needed", kind="multiline"),
            TemplateField("reported_by", "Reported By", required=True, max_len=80),
        ],
    ),
}

# SHARES strips; the generic "Local Weather Report" menu entry IS the SHARES
# LOCALWX strip (one standard format rather than an invented second one).
_SHARES = _shares_templates()
TEMPLATES["local_wx_report"] = _SHARES.pop("shares_localwx")
TEMPLATES["local_wx_report"].id = "local_wx_report"
TEMPLATES.update(_SHARES)


# ── USGS "Did You Feel It?" (DYFI) ───────────────────────────────────────────
# Unlike the plain-text templates above, this one reproduces the real Winlink
# Standard Form (Standard_Forms 1.1.22.0, "USGS/USGS DYFI.txt" + "USGS
# DYFI.html", form_version "DYFI Winlink form Ver 11.0.5 en", read from the
# copy Pat downloads): USGS's dyfi_reports_automated mailbox ingests the JSON
# between the BEGIN/END markers and drops anything else. The JSON is what the
# form's formDataToObject() builds: every non-"x" field in page order, empty
# values left out, all values strings. Answer codes (including the trailing
# spaces in "0 rattled_loudly " and "1 did_not_fall ") are copied verbatim.
# The optional ShakeAlert ("sup_*") section is not offered; unanswered, the
# real form omits those keys too.

DYFI_TO = "dyfi_reports_automated@usgs.gov"
DYFI_FORM_VERSION = "DYFI Winlink form Ver 11.0.5 en"

# key -> [(code sent to USGS, label shown to the operator)], in form order
_DYFI_CHOICES: dict[str, list[tuple[str, str]]] = {
    "eventType": [("EXERCISE", "EXERCISE"), ("REAL EVENT", "REAL EVENT")],
    "fldSituation_felt": [("1", "Yes"), ("0", "No")],
    "fldSituation_situation": [("inside", "Inside a building"), ("outside", "Outside a building"),
                               ("veh_stopped", "In a stopped vehicle"), ("veh_moving", "In a moving vehicle"),
                               ("other", "Other (describe below)")],
    "fldSituation_floor": [("underground", "Underground"), ("ground", "Ground floor"), ("floor2", "2nd floor"),
                           ("floor3", "3rd floor"), ("other", "Higher (describe below)")],
    "fldSituation_structureStories": [("story1", "1 story"), ("story2", "2 stories"), ("story3", "3 stories"),
                                      ("other", "Taller (describe below)")],
    "fldSituation_sleep": [("no", "No"), ("slept", "Slept through it"), ("woke", "Woke up")],
    "fldSituation_others": [("2", "No others felt it"), ("3", "Some felt it, most did not"),
                            ("4", "Most felt it"), ("5", "Everyone/almost everyone felt it")],
    "fldExperience_shaking": [("0", "Not felt"), ("1", "Weak"), ("2", "Mild"), ("3", "Moderate"),
                              ("4", "Strong"), ("5", "Violent")],
    "fldExperience_reaction": [("0", "No reaction/not felt"), ("1", "Very little reaction"), ("2", "Excitement"),
                               ("3", "Somewhat frightened"), ("4", "Very frightened"), ("5", "Extremely frightened")],
    "fldExperience_response": [("no_action", "Took no action"), ("doorway", "Moved to doorway"),
                               ("duck", "Dropped and covered"), ("ran_outside", "Ran outside"),
                               ("other", "Other (describe below)")],
    "fldExperience_stand": [("0", "No"), ("1", "Yes")],
    "fldEffects_doors": [("0", "No"), ("1 slight", "Yes, slight swinging"), ("1 violent", "Yes, violent swinging")],
    "fldEffects_sounds": [("0", "No"), ("1 slight", "Yes, slight noise"), ("1 loud", "Yes, loud noise")],
    "fldEffects_shelved": [("0", "No"), ("0 rattled_slightly", "Rattled slightly"),
                           ("0 rattled_loudly ", "Rattled loudly"),
                           ("1 few_toppled_or_fell", "A few toppled or fell off"),
                           ("2 many_toppled_or_fell", "Many fell off"),
                           ("2 many_toppled_or_fell", "Nearly everything fell off")],
    "fldEffects_pictures": [("0", "No"), ("1 did_not_fall ", "Yes, but did not fall"),
                            ("1 some_fell", "Yes, and some fell")],
    "fldEffects_furniture": [("0", "No"), ("1", "Yes")],
    "fldEffects_appliances": [("0", "No"), ("fell", "Yes, some contents fell out"),
                              ("shifted", "Yes, shifted by inches"),
                              ("shifted_foot", "Yes, shifted by a foot or more"), ("overturned", "Yes, overturned")],
    "fldEffects_walls": [("0", "No"), ("cracked", "Yes, some were cracked"),
                         ("fell_partial", "Yes, some partially fell"),
                         ("fell_complete", "Yes, some fell completely")],
}

_DYFI_DAMAGE: list[tuple[str, str]] = [
    ("_none", "No damage"), ("_crackmin", "Hairline cracks in walls"),
    ("_crackwallfew", "A few large cracks in walls"), ("_crackwallmany", "Many large cracks in walls"),
    ("_tilesfell", "Ceiling tiles or lighting fixtures fell"), ("_crackchim", "Cracks in chimney"),
    ("_crackwindows", "One or several cracked windows"), ("_brokenwindows", "Many windows cracked or some broken out"),
    ("_masonryfell", "Masonry fell from block or brick wall(s)"),
    ("_majoroldchim", "Old chimney, major damage or fell down"),
    ("_majormodernchim", "Modern chimney, major damage or fell down"),
    ("_tiltedwall", "Side walls tilted over/collapsed completely"),
    ("_porch", "Separation of porch, balcony, or other addition from building"),
    ("_move", "Building permanently shifted over foundation"),
]


def _dyfi_choice(key: str, label: str, required: bool = False) -> TemplateField:
    return TemplateField(key, label, required=required, kind="choice",
                         choices=[lbl for _code, lbl in _DYFI_CHOICES[key]])


def _dyfi_code(key: str, label: str) -> str:
    for code, lbl in _DYFI_CHOICES[key]:
        if lbl.upper() == label.strip().upper():
            return code
    return ""


def _dyfi_cross_check(values: dict, errors: dict) -> dict:
    out = {}
    if "quake_date" not in errors and values.get("quake_date"):
        mo, d, y = (int(x) for x in str(values["quake_date"]).strip().split("/"))
        try:
            datetime(y, mo, d)
        except ValueError:
            out["quake_date"] = "is not a real calendar date"
    if not str(values.get("user_id") or "").strip():
        out["user_id"] = "is required (no Winlink callsign is configured to fill it in)"
    eid = str(values.get("eventid") or "").strip()
    if eid and "eventid" not in errors and str(values.get("eventType") or "").strip().upper() != "REAL EVENT":
        out["eventid"] = "only applies to a REAL EVENT report"
    return out


def _dyfi_int(v: str) -> int:
    """The form's parseInt(getinteger(v)): drop the first run of non-digits."""
    m = re.match(r"\d+", re.sub(r"[^\d.]+", "", v or "", count=1))
    return int(m.group(0)) if m else 0


def dyfi_intensity(j: dict) -> int:
    """Port of the form's intensity1() (USGS's community decimal intensity:
    weighted sum of felt/experience/effects/damage, then 3.3996*ln(cws)-4.3781).
    Written into "mapmmscale" exactly as the form does before sending."""
    other = _dyfi_int(j.get("fldSituation_others", ""))
    felt = _dyfi_int(j.get("fldSituation_felt", ""))
    felt_v = felt if other <= 2 else (0.72 if other == 3 else 1)
    d = j.get("d_text", "")
    if any(t in d for t in ("_move", "_chim", "_found", "_collapse", "_porch", "_majormodernchim", "_tiltedwall")):
        dmg = 3
    elif any(t in d for t in ("_wall", "_pipe", "_win", "_brokenwindows", "_majoroldchim", "_masonryfell")):
        dmg = 2
    elif any(t in d for t in ("_crackwallmany", "_crackwall", "_crackfloor", "_crackchim", "_tilesfell")):
        dmg = 1
    elif "_crackwallfew" in d:
        dmg = 0.75
    elif "_crackmin" in d or "_crackwindows" in d:
        dmg = 0.5
    else:
        dmg = 0
    cws = (felt_v * 5 + _dyfi_int(j.get("fldExperience_shaking", ""))
           + _dyfi_int(j.get("fldExperience_reaction", ""))
           + _dyfi_int(j.get("fldExperience_stand", "")) * 2 + _dyfi_int(j.get("fldEffects_shelved", "")) * 5
           + _dyfi_int(j.get("fldEffects_pictures", "")) * 2 + _dyfi_int(j.get("fldEffects_furniture", "")) * 3
           + dmg * 5)
    i = 3.3996 * math.log(cws) - 4.3781 if cws > 0 else 1
    if i <= 1:
        i = 1
    elif i < 2:
        i = 2
    return int(i + 0.5)


def _render_dyfi(v: dict[str, str]) -> dict[str, str]:
    j: dict[str, str] = {}

    def put(k, val):
        if val not in (None, ""):
            j[k] = str(val)

    etype = _dyfi_code("eventType", v["eventType"])
    put("eventType", etype)
    if etype == "EXERCISE":
        put("exercise_id", v["exercise_id"])
    else:
        put("eventid", v["eventid"].replace(" ", ""))
    put("fldSituation_felt", _dyfi_code("fldSituation_felt", v["fldSituation_felt"]))
    put("ciim_time", f"{v['quake_date']} {v['quake_time']}")
    put("ciim_mapAddress", v["ciim_mapAddress"])
    put("ciim_mapLat", v["ciim_mapLat"])
    put("ciim_mapLon", v["ciim_mapLon"])
    if v["ciim_mapLat"] or v["ciim_mapLon"]:
        put("location_source", "FORM ENTRY")
    for key, other in (("fldSituation_situation", "fldSituation_situation_other"),
                       ("fldSituation_floor", "ifHigherPleaseDescribe"),
                       ("fldSituation_structureStories", "howTallPleaseDescribe"),
                       ("fldSituation_sleep", None), ("fldSituation_others", None),
                       ("fldExperience_shaking", None), ("fldExperience_reaction", None),
                       ("fldExperience_response", "fldExperience_response_other"),
                       ("fldExperience_stand", None), ("fldEffects_doors", None), ("fldEffects_sounds", None),
                       ("fldEffects_shelved", None), ("fldEffects_pictures", None), ("fldEffects_furniture", None),
                       ("fldEffects_appliances", None), ("fldEffects_walls", None)):
        if v[key]:
            put(key, _dyfi_code(key, v[key]))
        if other:
            put(other, v[other])
    picked = {x.strip().upper() for x in v["damage"].split("|") if x.strip()}
    codes = [code for code, lbl in _DYFI_DAMAGE if lbl.upper() in picked]
    if codes:
        put("d_text", "".join(c + " " for c in codes))      # the form's collect(): "code " per box
    put("mapmmscale", dyfi_intensity(j))
    if codes:
        put("BuildingDamage", "YES" if any(c != "_none" for c in codes) else "NO")
    put("comments", v["comments"])
    put("language", "en")
    put("timestamp", int(time.time() * 1000))
    put("user_id", v["user_id"].upper())
    put("user_id_type", "Callsign")
    put("form_version", DYFI_FORM_VERSION)
    body = ("\n--- BEGIN json ---\n\n" + json.dumps(j, indent=2, ensure_ascii=False)
            + "\n\n--- END json ---\n\n[No changes or editing of this message are allowed]\n")
    return {"subject": f"DYFI Automatic Entry - Winlink {etype}", "body": body, "to": DYFI_TO}


_DYFI_TEMPLATE = Template(
    id="usgs_dyfi",
    name="USGS Did You Feel It? (DYFI)",
    description="Earthquake felt report for the USGS DYFI system, in the same JSON format as Winlink's "
                "standard \"USGS DYFI\" form. USGS only receives it through an Internet-connected gateway "
                "(CMS/Telnet) - not P2P or radio-only. Mark drills as EXERCISE. Send it unedited.",
    to_address=DYFI_TO,
    group="USGS",
    fields=[
        _dyfi_choice("eventType", "This earthquake report is", required=True),
        TemplateField("exercise_id", "Exercise ID (optional, exercises only)", max_len=40,
                      placeholder="e.g. 2026SHAKEOUT"),
        TemplateField("eventid", "USGS Event ID (real events, if known)", max_len=40, kind="pattern",
                      pattern=r"[A-Za-z0-9]+", hint="letters and digits, no spaces (e.g. ci40851816)"),
        _dyfi_choice("fldSituation_felt", "Did you feel it?", required=True),
        TemplateField("quake_date", "Date of the earthquake (local)", required=True, kind="pattern",
                      pattern=r"(0?[1-9]|1[0-2])/(0?[1-9]|[12]\d|3[01])/\d{4}", hint="M/D/YYYY, e.g. 1/31/2026"),
        TemplateField("quake_time", "Time of the earthquake (local, 24 h)", required=True, kind="pattern",
                      pattern=r"([01]?\d|2[0-3]):[0-5]\d", hint="HH:MM, e.g. 09:15"),
        TemplateField("ciim_mapAddress", "Your location: street address, city and state", required=True,
                      max_len=200, placeholder="Street address, city, state (not a P.O. box)"),
        TemplateField("ciim_mapLat", "Latitude (optional)", kind="number", min=-90, max=90,
                      placeholder="e.g. 44.1037"),
        TemplateField("ciim_mapLon", "Longitude (optional)", kind="number", min=-180, max=180,
                      placeholder="e.g. -69.1089"),
        _dyfi_choice("fldSituation_situation", "Your situation during the earthquake"),
        TemplateField("fldSituation_situation_other", "Situation - other", max_len=80),
        _dyfi_choice("fldSituation_floor", "If inside, on what floor?"),
        TemplateField("ifHigherPleaseDescribe", "Floor - if higher, which?", max_len=40),
        _dyfi_choice("fldSituation_structureStories", "How tall is the building?"),
        TemplateField("howTallPleaseDescribe", "Building - if taller, how many stories?", max_len=40),
        _dyfi_choice("fldSituation_sleep", "Were you asleep?"),
        _dyfi_choice("fldSituation_others", "Did others nearby feel it?"),
        _dyfi_choice("fldExperience_shaking", "How would you describe the shaking?"),
        _dyfi_choice("fldExperience_reaction", "How did you react?"),
        _dyfi_choice("fldExperience_response", "How did you respond?"),
        TemplateField("fldExperience_response_other", "Response - other", max_len=80),
        _dyfi_choice("fldExperience_stand", "Was it difficult to stand or walk?"),
        _dyfi_choice("fldEffects_doors", "Did doors, windows or hanging objects swing?"),
        _dyfi_choice("fldEffects_sounds", "Did you hear creaking or other noises?"),
        _dyfi_choice("fldEffects_shelved", "Did objects rattle, topple over or fall off shelves?"),
        _dyfi_choice("fldEffects_pictures", "Did pictures on walls move or get knocked askew?"),
        _dyfi_choice("fldEffects_furniture", "Did furniture or appliances slide, topple or move?"),
        _dyfi_choice("fldEffects_appliances", "Was a heavy appliance (refrigerator, range) affected?"),
        _dyfi_choice("fldEffects_walls", "Were free-standing walls or fences damaged?"),
        TemplateField("damage", "Damage to the building you were in (tick all that apply)", kind="multichoice",
                      choices=[lbl for _c, lbl in _DYFI_DAMAGE]),
        TemplateField("comments", "Comments", kind="multiline", max_len=1000),
        # Optional in the UI: the render API fills the station's Winlink callsign.
        TemplateField("user_id", "Your callsign (blank = this station's Winlink callsign)", kind="callsign"),
    ],
)
TEMPLATES[_DYFI_TEMPLATE.id] = _DYFI_TEMPLATE


def list_templates() -> list[dict]:
    return [
        {
            "id": t.id,
            "name": t.name,
            "group": t.group,
            "description": t.description,
            "to_address": t.to_address,
            "fields": [
                {"key": f.key, "label": f.label, "required": f.required,
                 "placeholder": f.placeholder, "options": f.options,
                 "kind": f.kind, "choices": f.choices, "hint": f.hint, "max_len": f.max_len}
                for f in t.fields
            ],
        }
        for t in TEMPLATES.values()
    ]


class TemplateValidationError(ValueError):
    """Raised by render_template with every field error at once."""

    def __init__(self, errors: dict[str, str], labels: dict[str, str]):
        self.errors = errors
        first_key = next(iter(errors))
        msg = f"'{labels.get(first_key, first_key)}' {errors[first_key]}"
        if len(errors) > 1:
            msg += f" (and {len(errors) - 1} more field{'s' if len(errors) > 2 else ''})"
        super().__init__(msg)


def render_template(template_id: str, values: dict[str, str]) -> dict[str, str]:
    """Render a filled-in template to {"subject": str, "body": str, "to": str|None}.
    Raises KeyError if template_id is unknown, TemplateValidationError (a
    ValueError, with .errors per field) if any field is missing or invalid."""
    tmpl = TEMPLATES[template_id]
    errors = validate_template(template_id, values)
    if errors:
        raise TemplateValidationError(errors, {f.key: f.label for f in tmpl.fields})

    v = {f.key: str(values.get(f.key) or "").strip() for f in tmpl.fields}
    now = _utc_now_str()

    strip_name = getattr(tmpl, "_strip", None)
    if strip_name:
        answers = {}
        for f, (qkey, _label) in zip(tmpl.fields, guided_fields(strip_name)):
            val = v[f.key]
            if f.kind == "mgrs" and val and val.upper() != "NA":
                val = resolve_mgrs_answer(val)
            if f.kind == "choice" and val:
                val = next((c for c in f.choices if c.upper() == val.upper()), val)
            answers[extract_question_key(_label)] = val
        strip = build_response_strip(strip_name, answers)   # byte-compatible with Response Creator
        return {"subject": strip_name, "body": strip + "\n", "to": tmpl.to_address}

    if template_id == "usgs_dyfi":
        return _render_dyfi(v)

    if template_id == "ai_help":
        return {"subject": "AI", "body": v["query"], "to": tmpl.to_address}

    if template_id == "catalog_request":
        return {"subject": "REQUEST", "body": v["inquiry_id"], "to": tmpl.to_address}

    if template_id == "ics213":
        subject = f"ICS-213 {v['subject']}"
        body = (
            f"ICS-213 GENERAL MESSAGE\n"
            f"Date/Time: {now}\n"
            f"To: {v['to_name']}" + (f" ({v['to_position']})" if v['to_position'] else "") + "\n"
            f"From: {v['from_name']}" + (f" ({v['from_position']})" if v['from_position'] else "") + "\n"
            f"Subject: {v['subject']}\n"
            f"\n{v['message']}\n"
        )
        return {"subject": subject, "body": body, "to": tmpl.to_address}

    if template_id == "ics214a":
        lines = [ln.strip() for ln in v["activity_log"].splitlines() if ln.strip()]
        rows = []
        for ln in lines:
            m = re.match(r"(\d{4})Z?\s+(.*)", ln)
            rows.append(f"  {m.group(1)}  {m.group(2)}" if m else f"        {ln}")
        log_rows = "\n".join(rows)
        body = (
            "ICS 214a — INDIVIDUAL LOG\n"
            f"1. Incident Name: {v['incident_name']}\n"
            f"2. Operational Period: {v['period_from']} to {v['period_to']}\n"
            f"3. Individual Name: {v['individual_name']}\n"
            f"4. ICS Section: {v['ics_section'] or '—'}\n"
            f"5. Assignment/Location: {v['assignment'] or '—'}\n"
            "6. Activity Log:\n"
            "  TIME  MAJOR EVENTS\n"
            f"{log_rows}\n"
            f"7. Prepared By: {v['prepared_by']}    Date/Time: {now}\n"
        )
        return {"subject": f"ICS-214A {v['incident_name']} — {v['individual_name']}", "body": body,
                "to": tmpl.to_address}

    if template_id == "ota_report":
        prog = v["program"]
        mhz = float(v["frequency_khz"]) / 1000.0
        short = prog.split(" ")[0]
        kind = {"Spot": "SPOT", "Self-spot": "SELF-SPOT", "Hunter": "HUNTER QSO"}[v["report_type"].split(" ")[0]]
        rst = " ".join(x for x in (f"sent {v['rst_sent']}" if v["rst_sent"] else "",
                                    f"rcvd {v['rst_rcvd']}" if v["rst_rcvd"] else "") if x)
        line = (f"{short} {v['reference'].upper()} {v['activator'].upper()} {mhz:.3f} {v['mode']} "
                f"{v['time_utc'].upper().rstrip('Z')}Z" + (f" {rst}" if rst else "")
                + (f" — {v['comments']}" if v["comments"] else ""))
        body = (f"{kind}\n{line}\n\nProgram: {prog}\nReference: {v['reference'].upper()}\n"
                f"Activator: {v['activator'].upper()}\nFrequency: {mhz:.3f} MHz {v['mode']}\n"
                f"Time: {v['time_utc'].upper().rstrip('Z')}Z\n"
                + (f"Signal reports: {rst}\n" if rst else "")
                + (f"Comments: {v['comments']}\n" if v["comments"] else "")
                + f"Sent: {now}\n")
        return {"subject": f"{short} {kind} {v['reference'].upper()} {v['activator'].upper()}",
                "body": body, "to": tmpl.to_address}

    if template_id == "radiogram":
        subject = f"Radiogram #{v['number']}" if v["number"] else "Radiogram"
        preamble = " ".join(
            x for x in [
                v["number"], v["precedence"], v["station_of_origin"],
                f"{len(v['text'].split())}" if v["text"] else "",
                v["place_of_origin"], now,
            ] if x
        )
        body = (
            f"RADIOGRAM\n"
            f"{preamble}\n"
            f"TO: {v['to_address']}\n"
            f"\n{v['text']}\n"
            f"\n{v['signature']}\n"
        )
        return {"subject": subject, "body": body, "to": tmpl.to_address}

    if template_id == "sitrep":
        subject = f"SITREP — {v['location']}"
        body = (
            f"SITUATION REPORT\n"
            f"Date/Time: {now}\n"
            f"Location: {v['location']}\n"
            f"Situation: {v['situation']}\n"
            + (f"Actions Taken: {v['actions_taken']}\n" if v["actions_taken"] else "")
            + (f"Resources Needed: {v['resources_needed']}\n" if v["resources_needed"] else "")
            + f"Reported By: {v['reported_by']}\n"
        )
        return {"subject": subject, "body": body, "to": tmpl.to_address}

    raise KeyError(template_id)
