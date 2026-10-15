"""O139: per-user saved map layer profiles (server-side, viewer-writable)."""
from unittest.mock import MagicMock

from starlette.testclient import TestClient


class _KV:
    def __init__(self):
        self.d = {}

    async def get_kv(self, k):
        return self.d.get(k)

    async def set_kv(self, k, v):
        self.d[k] = v


def _client():
    from ech.api import app as app_mod
    db = MagicMock()
    kv = _KV()
    db.get_kv, db.set_kv = kv.get_kv, kv.set_kv
    return TestClient(app_mod.create_app(MagicMock(), db)), kv


def test_roundtrip_default_and_validation():
    c, kv = _client()
    with c:
        assert c.get("/api/user/map-profiles").json() == {"default": None, "profiles": {}}
        st = {"buttons": {"adsb-btn": False, "pota-btn": True}, "wx": {"nws_api": True}, "trackMinutes": 60}
        r = c.put("/api/user/map-profiles", json={"default": "Field day", "profiles": {"Field day": st}})
        assert r.status_code == 200 and r.json()["default"] == "Field day"
        got = c.get("/api/user/map-profiles").json()
        assert got["profiles"]["Field day"]["buttons"]["pota-btn"] is True
        # unknown default is dropped, not stored
        r = c.put("/api/user/map-profiles", json={"default": "nope", "profiles": {"A": st}})
        assert r.json()["default"] is None
        # bad payloads rejected
        assert c.put("/api/user/map-profiles", json={"profiles": {"": st}}).status_code == 400
        assert c.put("/api/user/map-profiles", json={"profiles": {"x": "notadict"}}).status_code == 400
        too_many = {f"p{i}": st for i in range(21)}
        assert c.put("/api/user/map-profiles", json={"profiles": too_many}).status_code == 400
