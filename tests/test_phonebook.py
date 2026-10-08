"""
tests/test_phonebook.py
------------------------
Covers the Yealink remote-phonebook endpoint (/phonebook.xml) pulling its
HOIP conference-bridge/RF-link/audio-feed/test-number entries from
ech/core/radio_directory.py (single source of truth, shared with
GET /api/pbx/directory) instead of a small hand-maintained duplicate list.
"""

import pytest


@pytest.mark.asyncio
async def test_phonebook_includes_roster_and_hoip_entries():
    from ech.core.database import Database
    from ech.core.router import Router
    from ech.core.anomaly import AnomalyEngine
    from ech.api.app import create_app
    from httpx import AsyncClient, ASGITransport

    db = Database(":memory:")
    await db.connect()
    router = Router(db, anomaly_engine=AnomalyEngine({}))
    await router.start()
    app = create_app(router, db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/phonebook.xml")
        assert r.status_code == 200
        body = r.text

    assert "<YealinkIPPhoneDirectory>" in body
    # Local extension roster still present
    assert "<Telephone>101</Telephone>" in body
    # HOIP entries, category-tagged for on-phone scanning
    assert "[HOIP TEST] Echo Test" in body
    assert "<Telephone>3194</Telephone>" in body
    assert "[AUDIO] TOP 80's" in body
    assert "<Telephone>90009</Telephone>" in body
    assert "[CONF-US] PUBLIC 1" in body
    assert "[RF-EU] G8PY ALLSTAR 54775" in body

    await router.stop()
    await db.close()


@pytest.mark.asyncio
async def test_phonebook_is_valid_xml():
    import xml.etree.ElementTree as ET
    from ech.core.database import Database
    from ech.core.router import Router
    from ech.core.anomaly import AnomalyEngine
    from ech.api.app import create_app
    from httpx import AsyncClient, ASGITransport

    db = Database(":memory:")
    await db.connect()
    router = Router(db, anomaly_engine=AnomalyEngine({}))
    await router.start()
    app = create_app(router, db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/phonebook.xml")
        body = r.text

    root = ET.fromstring(body)  # raises if malformed
    entries = root.findall("DirectoryEntry")
    assert len(entries) > 150  # roster + feature codes + full HOIP directory

    await router.stop()
    await db.close()
