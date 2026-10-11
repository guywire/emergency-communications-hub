"""
tests/test_bridge_api.py
------------------------
POST /api/bridge-rules validates + normalises O117 rules and applies them
live; GET /api/bridge-rules/status reports per-rule counters and the
decision log; invalid rules are refused without touching the live set.
"""

import pytest
import yaml


@pytest.mark.asyncio
async def test_bridge_rules_save_validate_status(tmp_path, monkeypatch):
    from httpx import ASGITransport, AsyncClient

    from ech.api.app import create_app
    from ech.core.anomaly import AnomalyEngine
    from ech.core.database import Database
    from ech.core.router import Router

    monkeypatch.chdir(tmp_path)                      # endpoint falls back to ./config.yaml
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"adapters": []}))
    db = Database(":memory:")
    await db.connect()
    router = Router(db, anomaly_engine=AnomalyEngine({}))
    app = create_app(router, db)

    rule = {"name": "net", "a": {"adapter": "mesh", "channel": "2"},
            "b": {"adapter": "aprs", "to": "emcomm"}, "direction": "both", "dry_run": True}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = (await c.post("/api/bridge-rules", json={"rules": [rule]})).json()
        assert r["status"] == "ok"
        saved = r["rules"][0]
        assert saved["a"] == {"adapter": "mesh", "channel": 2}       # normalised
        assert saved["b"]["to"] == "EMCOMM"
        assert yaml.safe_load((tmp_path / "config.yaml").read_text())["bridge_rules"][0]["name"] == "net"

        st = (await c.get("/api/bridge-rules/status")).json()
        assert st["rules"][0]["name"] == "net" and st["rules"][0]["counters"]["forwarded"] == 0

        bad = (await c.post("/api/bridge-rules",
                            json={"rules": [{**rule, "types": ["position"]}]})).json()
        assert bad["status"] == "error" and "position" in bad["detail"]
        # live set unchanged by the refused save
        assert router._bridge_rules[0]["types"] == ["text"]
