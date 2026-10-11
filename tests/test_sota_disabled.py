"""
tests/test_sota_disabled.py
---------------------------
2026-10-11: SOTA deprecated the spots endpoint the map used, and its API
terms require prior approval for AI-generated software. The SOTA spots
layer must stay OFF (no outbound request) unless `sota.api_enabled` is set.
"""

from unittest.mock import MagicMock

import yaml
from starlette.testclient import TestClient


def test_sota_spots_off_by_default_makes_no_request(tmp_path, monkeypatch):
    import httpx
    from ech.api.app import create_app

    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump({"adapters": []}))
    calls = []
    monkeypatch.setattr(httpx.AsyncClient, "get",
                        lambda self, url, *a, **k: calls.append(url))
    state = MagicMock(_base_lat=44.16, _base_lon=-69.12)
    app = create_app(MagicMock(), MagicMock(), ech_state=state, config_path=str(cfg))
    with TestClient(app) as c:
        d = c.get("/api/sota/spots").json()
    assert d["disabled"] is True and d["spots"] == []
    assert calls == []
