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
    from ech.core.winlink_forms import _check
    # Fields are validated now (O127), so pick, per field, the first sample
    # that its own validator accepts — still exercises every render path.
    samples = ["x", "W1AW", "14244", "1640", "2026-10-11 0800", "111630Z", "111630Z OCT 2026",
               "10-11-2026", "1430L", "ME", "ME GYX", "GYX", "44.1620N", "069.1210W", "19TDJ9172",
               "0800 Checked in at EOC", "NNE 7 21G", "2.6", "a@example.org", "US-2664", "29.92",
               "10/16/2026", "09:15"]

    def valid(f):
        if f.choices:
            return f.choices[0]
        return next(s for s in samples if _check(f, s) is None)

    for tid, tmpl in TEMPLATES.items():
        values = {f.key: valid(f) for f in tmpl.fields if f.required}
        if tid == "ota_report":
            values["reference"] = "US-2664"     # must match the program (first choice: POTA)
        out = render_template(tid, values)
        assert out["subject"]
        assert out["body"]


# ── O127: validation + new templates ──────────────────────────────────────

def test_validation_reports_every_bad_field():
    from ech.core.winlink_forms import TemplateValidationError, validate_template
    errs = validate_template("ota_report", {"report_type": "Spot (activator on the air)", "program": "POTA",
                                            "reference": "US-2664", "activator": "not a call!",
                                            "frequency_khz": "99", "mode": "SSB", "time_utc": "2561"})
    assert set(errs) == {"activator", "frequency_khz", "time_utc"}
    with pytest.raises(TemplateValidationError) as ei:
        render_template("ota_report", {"program": "POTA"})
    assert "activator" in ei.value.errors and "reference" in ei.value.errors


def test_ota_report_renders_spot_line_and_checks_reference_format():
    from ech.core.winlink_forms import TemplateValidationError
    base = {"report_type": "Spot (activator on the air)", "program": "SOTA", "reference": "w1/ha-126",
            "activator": "N0TAR", "frequency_khz": "146520", "mode": "FM", "time_utc": "1443",
            "comments": "No HF today"}
    out = render_template("ota_report", base)
    assert out["subject"] == "SOTA SPOT W1/HA-126 N0TAR"
    assert "SOTA W1/HA-126 N0TAR 146.520 FM 1443Z — No HF today" in out["body"]
    with pytest.raises(TemplateValidationError) as ei:
        render_template("ota_report", {**base, "reference": "US-2664"})   # POTA ref given for SOTA
    assert "reference" in ei.value.errors


def test_ics214a_activity_log_validated_and_rendered():
    from ech.core.winlink_forms import TemplateValidationError
    v = {"incident_name": "Ice Storm", "period_from": "2026-10-11 0800", "period_to": "2026-10-11 2000",
         "individual_name": "Guy KN0O", "ics_section": "Logistics",
         "activity_log": "0800 Checked in at EOC\n0815Z Net control on 147.000", "prepared_by": "KN0O"}
    out = render_template("ics214a", v)
    assert out["subject"] == "ICS-214A Ice Storm — Guy KN0O"
    assert "  0800  Checked in at EOC" in out["body"] and "  0815  Net control on 147.000" in out["body"]
    with pytest.raises(TemplateValidationError) as ei:
        render_template("ics214a", {**v, "activity_log": "Checked in, no time\n2575 bad time"})
    assert "activity_log" in ei.value.errors


def test_shares_local_wx_is_byte_compatible_strip_and_rejects_slash():
    from ech.core.strip_templates import build_response_strip
    from ech.core.winlink_forms import TemplateValidationError, TEMPLATES
    t = TEMPLATES["local_wx_report"]
    vals = {"call_sign": "KN0O", "city": "Rockland", "state": "ME", "nws_cwa": "GYX",
            "date_time": "111630Z", "wind_direction": "nne", "wind_speed": "7", "clouds": "OVC",
            "temperature": "52", "barometric_pressure": "29.92", "barometric_pressure_3_hour_trend": "Falling"}
    out = render_template("local_wx_report", vals)
    assert out["subject"] == "LOCALWX"
    expected = build_response_strip("LOCALWX", {
        "CALL SIGN": "KN0O", "CITY": "Rockland", "STATE": "ME", "NWS CWA": "GYX", "DATE-TIME": "111630Z",
        "WIND DIRECTION": "NNE", "WIND SPEED": "7", "CLOUDS": "OVC", "TEMPERATURE": "52",
        "BAROMETRIC PRESSURE": "29.92", "BAROMETRIC PRESSURE 3 HOUR TREND": "Falling"})
    assert out["body"].strip() == expected                      # same bytes as Response Creator
    with pytest.raises(TemplateValidationError) as ei:
        render_template("local_wx_report", {**vals, "city": "Rockland/Owls Head", "barometric_pressure": "120"})
    assert set(ei.value.errors) == {"city", "barometric_pressure"}
    assert any(tid.startswith("shares_") for tid in TEMPLATES) and len(t.fields) == 19


def test_shares_sitrep_yes_no_fields_enforced():
    from ech.core.winlink_forms import validate_template
    errs = validate_template("shares_sitrep", {"date_time": "111630Z OCT 2026", "call_sign": "KN0O",
                                               "commercial_power": "maybe", "comments": "x" * 251})
    assert set(errs) == {"commercial_power", "comments"}


def test_render_endpoint_returns_per_field_errors():
    from unittest.mock import MagicMock
    from starlette.testclient import TestClient
    from ech.api.app import create_app
    with TestClient(create_app(MagicMock(), MagicMock())) as c:
        d = c.post("/api/winlink/templates/ics214a/render",
                   json={"values": {"incident_name": "Ice Storm", "activity_log": "no time here"}}).json()
        assert d["status"] == "error"
        assert {"period_from", "period_to", "individual_name", "activity_log", "prepared_by"} <= set(d["errors"])
        groups = {t["group"] for t in c.get("/api/winlink/templates").json()["templates"]}
        assert {"ICS", "SHARES Region 1", "On the Air", "Winlink services", "General"} <= groups


def test_usgs_dyfi_matches_winlink_standard_form_json():
    """USGS's dyfi_reports_automated mailbox ingests the JSON between the
    BEGIN/END markers of Winlink's standard "USGS DYFI" form and drops anything
    else - keys, codes (incl. trailing spaces) and wrapper must match it."""
    import json as _json
    from ech.core.winlink_forms import render_template, dyfi_intensity
    r = render_template("usgs_dyfi", {
        "eventType": "EXERCISE", "fldSituation_felt": "Yes", "quake_date": "10/16/2026", "quake_time": "09:15",
        "ciim_mapAddress": "1 Main St, Rockland, ME", "fldSituation_others": "Most felt it",
        "fldExperience_shaking": "Moderate", "fldEffects_shelved": "Rattled loudly",
        "fldEffects_pictures": "Yes, but did not fall", "damage": "Hairline cracks in walls|Cracks in chimney",
        "eventid": "", "user_id": "kn0o"})
    assert r["to"] == "dyfi_reports_automated@usgs.gov"
    assert r["subject"] == "DYFI Automatic Entry - Winlink EXERCISE"
    assert r["body"].startswith("\n--- BEGIN json ---\n\n{\n  \"eventType\": \"EXERCISE\",")
    assert r["body"].endswith("\n\n--- END json ---\n\n[No changes or editing of this message are allowed]\n")
    j = _json.loads(r["body"].split("--- BEGIN json ---")[1].split("--- END json ---")[0])
    assert j["fldSituation_felt"] == "1" and j["ciim_time"] == "10/16/2026 09:15"
    assert j["fldEffects_shelved"] == "0 rattled_loudly " and j["fldEffects_pictures"] == "1 did_not_fall "
    assert j["d_text"] == "_crackmin _crackchim " and j["BuildingDamage"] == "YES"
    assert j["mapmmscale"] == "5"           # cws 15 -> 3.3996*ln(15)-4.3781 = 4.83 -> 5
    assert j["user_id"] == "KN0O" and j["form_version"] == "DYFI Winlink form Ver 11.0.5 en"
    assert "eventid" not in j and "ciim_mapLat" not in j and "location_source" not in j   # blanks omitted
    assert all(isinstance(x, str) for x in j.values())
    assert dyfi_intensity({}) == 1


def test_usgs_dyfi_validation():
    from ech.core.winlink_forms import validate_template
    errs = validate_template("usgs_dyfi", {"eventType": "EXERCISE", "fldSituation_felt": "Yes",
                                           "quake_date": "2/30/2026", "quake_time": "25:00",
                                           "ciim_mapAddress": "x", "user_id": "KN0O", "eventid": "ci123",
                                           "damage": "Nope"})
    assert set(errs) == {"quake_date", "quake_time", "eventid", "damage"}
