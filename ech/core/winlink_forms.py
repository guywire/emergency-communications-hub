"""
ech/core/winlink_forms.py
--------------------------
Winlink message templates (O82).

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
real, standard field layouts (FEMA ICS-213, ARRL Radiogram) rendered as
ordinary message text with a clear Subject line — readable by ANY Winlink
client or plain email, at the cost of not being a binary-identical rendered
form on the receiving end.

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

from dataclasses import dataclass, field
from datetime import datetime, timezone


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


def _utc_now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%MZ")


TEMPLATES: dict[str, Template] = {
    "ics213": Template(
        id="ics213",
        name="ICS-213 General Message",
        description="FEMA/ICS standard general message form — the plain-text layout "
                     "operators use when sending without the interactive Winlink form.",
        fields=[
            TemplateField("to_name", "To (Name)", required=True),
            TemplateField("to_position", "To (Position/Agency)"),
            TemplateField("from_name", "From (Name)", required=True),
            TemplateField("from_position", "From (Position/Agency)"),
            TemplateField("subject", "Subject", required=True),
            TemplateField("message", "Message", required=True, placeholder="Multi-line message body"),
        ],
    ),
    "radiogram": Template(
        id="radiogram",
        name="ARRL Radiogram",
        description="Standard ARRL/NTS radiogram layout: preamble (number, precedence, "
                     "station of origin, check, place/time filed) + address + text + signature.",
        fields=[
            TemplateField("number", "Message Number"),
            TemplateField("precedence", "Precedence", placeholder="R(outine) / W(elfare) / P(riority) / E(mergency)"),
            TemplateField("station_of_origin", "Station of Origin"),
            TemplateField("place_of_origin", "Place of Origin"),
            TemplateField("to_address", "To (Name/Address)", required=True),
            TemplateField("text", "Text", required=True, placeholder="Radiogram message text"),
            TemplateField("signature", "Signature", required=True),
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
        fields=[
            TemplateField(
                "inquiry_id", "Inquiry ID", required=True,
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
        fields=[
            TemplateField("query", "Question", required=True,
                          placeholder="e.g. Which hospitals in Maine share bed status, and what is it right now?"),
        ],
    ),
    "sitrep": Template(
        id="sitrep",
        name="Situation Report (SITREP)",
        description="Free-form emergency situation report — not a Winlink-standard "
                     "template, but a common ARES/RACES field-report layout.",
        fields=[
            TemplateField("location", "Location", required=True),
            TemplateField("situation", "Situation", required=True, placeholder="What's happening"),
            TemplateField("actions_taken", "Actions Taken"),
            TemplateField("resources_needed", "Resources Needed"),
            TemplateField("reported_by", "Reported By", required=True),
        ],
    ),
}


def list_templates() -> list[dict]:
    return [
        {
            "id": t.id,
            "name": t.name,
            "description": t.description,
            "to_address": t.to_address,
            "fields": [
                {"key": f.key, "label": f.label, "required": f.required,
                 "placeholder": f.placeholder, "options": f.options}
                for f in t.fields
            ],
        }
        for t in TEMPLATES.values()
    ]


def render_template(template_id: str, values: dict[str, str]) -> dict[str, str]:
    """Render a filled-in template to {"subject": str, "body": str, "to": str|None}.
    Raises KeyError if template_id is unknown, ValueError if a required
    field is missing."""
    tmpl = TEMPLATES[template_id]
    for f in tmpl.fields:
        if f.required and not (values.get(f.key) or "").strip():
            raise ValueError(f"'{f.label}' is required")

    v = {f.key: (values.get(f.key) or "").strip() for f in tmpl.fields}
    now = _utc_now_str()

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
