"""Log lines must never carry credentials: httpx logs full request URLs, and
MeshMapper/AirNow put API keys in the query string while FIRMS puts its key in
the URL path (2026-10-16: both were in the live journal)."""

import logging

from ech.main import RedactSecretsFilter


def _msg(f, text, *args):
    r = logging.LogRecord("httpx", logging.INFO, "", 0, text, args or None, None)
    f.filter(r)
    return r.getMessage()


def test_query_string_keys_masked():
    f = RedactSecretsFilter()
    out = _msg(f, 'HTTP Request: %s %s', "GET", "https://meshmapper.net/coverage.php?key=0a46abcd&x=1")
    assert out == "HTTP Request: GET https://meshmapper.net/coverage.php?key=***&x=1"
    assert _msg(f, "API_KEY=abc token=t password=p keyboard=ok monkey=1") == \
        "API_KEY=*** token=*** password=*** keyboard=ok monkey=1"


def test_configured_secret_values_masked_anywhere():
    f = RedactSecretsFilter({"air_quality_service": {"firms_api_key": "56938f9bdeadbeef", "enabled": True},
                             "adapters": [{"name": "dapnet", "password": "hunter2hunter2"}],
                             "station": {"name": "a-long-public-name"}})
    assert _msg(f, "GET /api/area/csv/56938f9bdeadbeef/VIIRS") == "GET /api/area/csv/***/VIIRS"
    assert _msg(f, "login hunter2hunter2 ok") == "login *** ok"
    assert _msg(f, "station a-long-public-name") == "station a-long-public-name"   # not a secret key
