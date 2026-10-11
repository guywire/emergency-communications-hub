"""
tests/test_modem_api.py
-----------------------
GET/POST /api/adapters/{name}/modem — live tuning of sound-card modems from
the messages page, optionally persisted to config.yaml.
"""

from unittest.mock import MagicMock

import yaml
from starlette.testclient import TestClient

from ech.adapters.cw_audio import CWAudioAdapter


def _app(tmp_path, adapter):
    from ech.api.app import create_app
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump({"adapters": [
        {"type": "cw_audio", "name": adapter.name, "input_device": "browser",
         "wpm": 20, "min_snr_db": 6}]}))
    router = MagicMock()
    router._adapters = {adapter.name: adapter}
    return create_app(router, MagicMock(), config_path=str(cfg)), cfg


def test_modem_get_set_and_persist(tmp_path):
    cw = CWAudioAdapter({"type": "cw_audio", "name": "cw-api", "input_device": "browser"})
    app, cfg = _app(tmp_path, cw)
    with TestClient(app) as c:
        r = c.get("/api/adapters/cw-api/modem")
        assert r.status_code == 200 and r.json()["mode"] == "CW"

        r = c.post("/api/adapters/cw-api/modem", json={"tx_wpm": 14, "sensitivity": 4})
        assert r.json()["settings"]["tx_wpm"] == 14 and not r.json()["persisted"]
        assert cw._wpm == 14
        assert yaml.safe_load(cfg.read_text())["adapters"][0]["wpm"] == 20   # not saved

        r = c.post("/api/adapters/cw-api/modem", json={"freq": 650, "persist": True})
        assert r.json()["persisted"] is True
        saved = yaml.safe_load(cfg.read_text())["adapters"][0]
        assert saved["freq"] == 650 and saved["wpm"] == 14 and saved["sensitivity"] == 4
        assert "min_snr_db" not in saved

        assert c.post("/api/adapters/cw-api/modem", json={"tx_wpm": "fast"}).status_code == 400
        assert c.get("/api/adapters/nope/modem").status_code == 404
