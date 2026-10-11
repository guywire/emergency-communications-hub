"""
tests/test_contact_email.py
---------------------------
Settings → PSKReporter contact email: each operator sets their own; it is
validated, applied live to the PSKReporter request header, and saved to
config.yaml (pskreporter.contact_email).
"""

from unittest.mock import MagicMock

import yaml
from starlette.testclient import TestClient


def test_contact_email_set_live_and_persisted(tmp_path):
    from ech.api import app as app_mod
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump({"operator": {"callsign": "KN0O"}}))
    app = app_mod.create_app(MagicMock(), MagicMock(), config_path=str(cfg))
    with TestClient(app) as c:
        assert c.get("/api/system/contact-email").json()["contact_email"] == ""
        assert c.post("/api/system/contact-email", json={"contact_email": "not-an-email"}).status_code == 400
        r = c.post("/api/system/contact-email", json={"contact_email": "op@example.org"}).json()
        assert r["persisted"] is True
        assert c.get("/api/system/contact-email").json()["contact_email"] == "op@example.org"
    from ech.core import identity
    assert identity.user_agent() == "(SignalMatrix/KN0O, op@example.org)"
    assert yaml.safe_load(cfg.read_text())["operator"]["contact_email"] == "op@example.org"
