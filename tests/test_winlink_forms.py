"""
tests/test_winlink_forms.py
-----------------------------
Covers O82: plain-text Winlink message templates (ICS-213, Radiogram,
SITREP) and the explicit-subject passthrough that lets a rendered
template's subject reach Pat distinct from the body.
"""

import pytest

from ech.core.winlink_forms import list_templates, render_template, TEMPLATES


def test_list_templates_has_expected_ids():
    ids = {t["id"] for t in list_templates()}
    assert {"ics213", "radiogram", "sitrep", "ai_help", "catalog_request"} <= ids


def test_render_catalog_request_known_id():
    out = render_template("catalog_request", {"inquiry_id": "WX_ATLANTIC"})
    assert out["to"] == "INQUIRY"
    assert out["subject"] == "REQUEST"
    assert out["body"] == "WX_ATLANTIC"


def test_render_catalog_request_list():
    out = render_template("catalog_request", {"inquiry_id": "LIST"})
    assert out["body"] == "LIST"


def test_render_catalog_request_free_text_id_allowed():
    """Options are suggestions, not an enforced enum — a typed ID not in the
    preset list (e.g. a specific airport's METAR) must still work."""
    out = render_template("catalog_request", {"inquiry_id": "METAR KPWM"})
    assert out["body"] == "METAR KPWM"


def test_render_catalog_request_requires_inquiry_id():
    with pytest.raises(ValueError):
        render_template("catalog_request", {})


def test_catalog_request_field_has_preset_options():
    t = next(t for t in list_templates() if t["id"] == "catalog_request")
    opts = t["fields"][0]["options"]
    assert "LIST" in opts and "METAR" in opts and "USWXRAD.GIF" in opts


def test_render_ai_help_matches_confirmed_real_message_shape():
    """O82: confirmed against a real sent message (MID OJELPRVWCCRG,
    2026-10-08) — To: AIHELP, Subject: AI, body is the raw question."""
    out = render_template("ai_help", {
        "query": "Which hospitals in Maine share bed status for current or emergency purposes?",
    })
    assert out["to"] == "AIHELP"
    assert out["subject"] == "AI"
    assert out["body"] == "Which hospitals in Maine share bed status for current or emergency purposes?"


def test_render_ai_help_requires_query():
    with pytest.raises(ValueError):
        render_template("ai_help", {})


def test_ai_help_listed_with_fixed_to_address():
    t = next(t for t in list_templates() if t["id"] == "ai_help")
    assert t["to_address"] == "AIHELP"


def test_list_templates_includes_field_schema():
    t = next(t for t in list_templates() if t["id"] == "ics213")
    keys = {f["key"] for f in t["fields"]}
    assert "subject" in keys and "message" in keys
    required = {f["key"] for f in t["fields"] if f["required"]}
    assert "message" in required


def test_render_ics213_requires_fields():
    with pytest.raises(ValueError):
        render_template("ics213", {"to_name": "W1ABC"})


def test_render_ics213_success():
    out = render_template("ics213", {
        "to_name": "W1ABC", "from_name": "KN0O", "subject": "Shelter status",
        "message": "Red Cross shelter open at the high school.",
    })
    assert out["subject"] == "ICS-213 Shelter status"
    assert "Red Cross shelter open" in out["body"]
    assert "To: W1ABC" in out["body"]
    assert "From: KN0O" in out["body"]


def test_render_radiogram_success():
    out = render_template("radiogram", {
        "number": "12", "to_address": "KN0O", "text": "all clear here", "signature": "W1XYZ",
    })
    assert out["subject"] == "Radiogram #12"
    assert "TO: KN0O" in out["body"]
    assert "all clear here" in out["body"]


def test_render_sitrep_success():
    out = render_template("sitrep", {
        "location": "Main St bridge", "situation": "Washed out", "reported_by": "KN0O",
    })
    assert "Main St bridge" in out["subject"]
    assert "Washed out" in out["body"]


def test_render_unknown_template_raises_keyerror():
    with pytest.raises(KeyError):
        render_template("not_a_template", {})


def test_every_template_renders_with_only_required_fields():
    """Guards against a template referencing a field key in render_template()
    that isn't actually declared (and therefore never defaults to '') —
    would KeyError on .format/f-string access instead of ValueError."""
    for tid, tmpl in TEMPLATES.items():
        values = {f.key: "x" for f in tmpl.fields if f.required}
        out = render_template(tid, values)
        assert out["subject"]
        assert out["body"]
