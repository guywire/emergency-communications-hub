"""
tests/test_air_quality.py
---------------------------
Covers the 2026-10-01 AirNow API migration: the old
/aq/observation/latLong/current/ endpoint now returns HTTP 410 "This web
service has been retired" (confirmed live 2026-10-08); the replacement
/aq/observation/current/ziplatlong/ endpoint returns lowerCamelCase fields
(nowcastAQI, aqiCategoryName, parameterName, reportingAreaName) instead of
the old PascalCase shape (AQI, nested Category.Name, ParameterName,
ReportingArea).
"""

import httpx
import pytest

from ech.core.air_quality import AirQualityService

# Real response shape captured live from the new endpoint (2026-10-08).
_NEW_SHAPE_RESPONSE = [
    {
        "dateObserved": "2026-10-07", "hourObserved": "22:00", "localTimeZone": "EDT",
        "reportingAreaName": "Mid-Coast", "siteID": "230090103", "siteName": "Acadia NP - McFarland",
        "parameterName": "PM2.5", "nowcastAQI": 18, "aqiCategoryName": "Good",
        "reportingAgency": "Maine Dept. of Environmental Protection",
    },
    {
        "dateObserved": "2026-10-07", "hourObserved": "22:00", "localTimeZone": "EDT",
        "reportingAreaName": "Mid-Coast", "siteID": "230090103", "siteName": "Acadia NP - McFarland",
        "parameterName": "OZONE", "nowcastAQI": 31, "aqiCategoryName": "Good",
        "reportingAgency": "Maine Dept. of Environmental Protection",
    },
]


def _service_with_mock_transport(handler) -> AirQualityService:
    svc = AirQualityService({"air_quality_service": {"enabled": True, "airnow_api_key": "TESTKEY"}})
    svc._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return svc


@pytest.mark.asyncio
async def test_fetch_current_uses_new_ziplatlong_endpoint():
    requested_url = {}

    def handler(request: httpx.Request) -> httpx.Response:
        requested_url["path"] = request.url.path
        return httpx.Response(200, json=_NEW_SHAPE_RESPONSE)

    svc = _service_with_mock_transport(handler)
    await svc._fetch_current_at(44.162, -69.121)

    assert requested_url["path"] == "/aq/observation/current/ziplatlong/"


@pytest.mark.asyncio
async def test_fetch_current_parses_new_field_names():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_NEW_SHAPE_RESPONSE)

    svc = _service_with_mock_transport(handler)
    result = await svc._fetch_current_at(44.162, -69.121)

    assert result is not None
    # OZONE (31) is the worse reading, so it should win over PM2.5 (18)
    assert result["aqi"] == 31
    assert result["category"] == "Good"
    assert result["parameter"] == "OZONE"
    assert result["reporting_area"] == "Mid-Coast"


@pytest.mark.asyncio
async def test_fetch_current_raises_on_retired_endpoint_410():
    """Guards against silently regressing back to the dead endpoint — a 410
    from the real retired URL should still surface as an HTTPStatusError the
    caller's try/except can log, not get swallowed."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(410, json={"WebServiceError": [{"Message": "retired"}]})

    svc = _service_with_mock_transport(handler)
    with pytest.raises(httpx.HTTPStatusError):
        await svc._fetch_current_at(44.162, -69.121)
