"""
tests/test_weather_warnings_geometry.py
------------------------------------------
Covers O94: the map's "Weather warnings" layer draws real NWS alert
polygons. This tests the data side — that WeatherService._poll_alerts()
actually captures the GeoJSON geometry NWS sends per alert (previously
discarded), including the real case where county/zone-based alerts have
no polygon at all (geometry: null) and must not be guessed at.
"""

import httpx
import pytest

from ech.core.weather import WeatherService


def _mock_client_factory(handler):
    class _MockAsyncClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)
    return _MockAsyncClient


def _make_feature(alert_id, geometry):
    return {
        "properties": {
            "id": alert_id, "status": "Actual", "severity": "Severe",
            "urgency": "Immediate", "event": "Severe Thunderstorm Warning",
            "headline": "Severe Thunderstorm Warning issued", "areaDesc": "Test County",
            "effective": "2026-10-09T12:00:00-04:00", "expires": "2026-10-09T13:00:00-04:00",
        },
        "geometry": geometry,
    }


@pytest.mark.asyncio
async def test_poll_alerts_captures_polygon_geometry(monkeypatch):
    polygon = {"type": "Polygon", "coordinates": [[[-70.0, 44.0], [-69.0, 44.0], [-69.0, 45.0], [-70.0, 44.0]]]}
    features = [_make_feature("alert-1", polygon)]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"features": features})

    ws = WeatherService({"weather_service": {"enabled": True, "nws_lat": 44.1, "nws_lon": -69.1}})
    ws._client = _mock_client_factory(handler)(headers={}, timeout=10.0)
    await ws._poll_alerts()

    assert len(ws._active_alerts) == 1
    assert ws._active_alerts[0]["geometry"] == polygon


@pytest.mark.asyncio
async def test_poll_alerts_handles_null_geometry_for_zone_based_alerts(monkeypatch):
    """Most 'Winter Storm Warning'-type alerts are county/zone-based and
    carry geometry: null in the real NWS feed — must be stored as None,
    never fabricated into a shape."""
    features = [_make_feature("alert-2", None)]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"features": features})

    ws = WeatherService({"weather_service": {"enabled": True, "nws_lat": 44.1, "nws_lon": -69.1}})
    ws._client = _mock_client_factory(handler)(headers={}, timeout=10.0)
    await ws._poll_alerts()

    assert len(ws._active_alerts) == 1
    assert ws._active_alerts[0]["geometry"] is None


@pytest.mark.asyncio
async def test_weather_alerts_endpoint_passes_geometry_through(monkeypatch):
    """GET /api/weather/alerts spreads each stored alert dict verbatim
    (**a) — confirm geometry survives that pass-through, since that's the
    exact endpoint the map's Weather Warnings layer consumes."""
    from ech.core.database import Database
    from ech.core.router import Router
    from ech.core.anomaly import AnomalyEngine
    from ech.api.app import create_app
    from httpx import AsyncClient, ASGITransport

    polygon = {"type": "Polygon", "coordinates": [[[-70.0, 44.0], [-69.0, 44.0], [-69.0, 45.0], [-70.0, 44.0]]]}

    db = Database(":memory:")
    await db.connect()
    router = Router(db, anomaly_engine=AnomalyEngine({}))
    await router.start()

    ws = WeatherService({"weather_service": {"enabled": True}})
    ws._active_alerts = [{
        "id": "alert-1", "severity": "Severe", "urgency": "Immediate",
        "event": "Tornado Warning", "headline": "Tornado Warning issued",
        "area": "Test County", "expires": "2026-10-09T13:00:00-04:00",
        "geometry": polygon,
    }]

    app = create_app(router, db, wx_service=ws)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/weather/alerts")
        assert r.status_code == 200
        body = r.json()
        assert body["alerts"][0]["geometry"] == polygon

    await router.stop()
    await db.close()
