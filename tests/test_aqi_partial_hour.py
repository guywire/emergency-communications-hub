"""AirNow posts the current hour state-by-state: a partial current hour must
not hide stations that only the previous hour has (2026-10-14: map showed only
Rhode Island at 00:08Z while Maine hadn't posted yet)."""
import asyncio

from ech.core.air_quality import AirQualityService


def _svc():
    return AirQualityService({"air_quality_service": {
        "enabled": True, "airnow_api_key": "k", "lat": 44.16, "lon": -69.12}})


def _rec(lat, lon, aqi, utc):
    return {"Latitude": lat, "Longitude": lon, "AQI": aqi, "UTC": utc, "Parameter": "PM2.5"}


def test_previous_hour_fills_in_states_not_yet_posted():
    s = _svc()
    calls = []

    async def fake(bbox, hour):
        calls.append(hour)
        if len(calls) == 1:   # previous hour: Maine + RI
            return [_rec(44.5, -69.8, 10, "h1"), _rec(41.6, -71.7, 30, "h1")]
        return [_rec(41.6, -71.7, 45, "h2")]   # current hour: RI only so far
    s._fetch_bbox_hour = fake
    asyncio.run(s._poll_stations())
    by_lat = {round(x["lat"], 1): x["aqi"] for x in s.stations()}
    assert by_lat == {44.5: 10, 41.6: 45}      # Maine kept, RI takes newest value
    assert all("_utc" not in x for x in s.stations())


def test_current_hour_failure_keeps_previous_hour():
    import httpx
    s = _svc()
    n = {"i": 0}

    async def fake(bbox, hour):
        n["i"] += 1
        if n["i"] == 1:
            return [_rec(44.5, -69.8, 10, "h1")]
        raise httpx.ConnectError("boom")
    s._fetch_bbox_hour = fake
    asyncio.run(s._poll_stations())
    assert len(s.stations()) == 1
