"""Satellite layer: TLE parsing, catalog matching, SGP4 positions, Starlink train detection."""
import asyncio
from datetime import datetime, timezone

from ech.core import satellites as sat

ISS = ("ISS (ZARYA)",
       "1 25544U 98067A   08264.51782528 -.00002182  00000-0 -11606-4 0  2927",
       "2 25544  51.6416 247.4627 0006703 130.5360 325.0288 15.72125391563537")


def test_parse_and_catalog():
    text = "\n".join(ISS) + "\n" + "\n".join(("JUNK", ISS[1], ISS[2])) + "\n"
    t = sat.parse_tle_text(text)
    assert [x[0] for x in t] == ["ISS (ZARYA)", "JUNK"]
    assert sat.catalog_match("OSCAR 7 (AO-7)")[0] == "AO-7"
    assert sat.catalog_match("ES'HAIL 2")[0] == "QO-100"
    assert sat.catalog_match("STARLINK-1234") is None
    assert sat.launch_key(ISS[1]) == "98067"


def test_iss_position_is_plausible():
    when = datetime(2008, 9, 20, 12, 25, 40, tzinfo=timezone.utc)
    kept, ecef = sat.SatelliteService._positions([ISS], when)
    assert len(kept) == 1
    lat, lon, alt = sat._ecef_to_geodetic(*[float(v) for v in ecef[0]])
    assert abs(lat) <= 52 and -180 <= lon <= 180
    assert 300 < alt < 450


def test_starlink_train_detected():
    # five satellites of one launch spaced along the same orbit + a lone other
    base = "1 {n:05d}U 24069{p}   08264.51782528 -.00002182  00000-0 -11606-4 0  2927"
    tles = []
    for i in range(6):
        l2 = f"2 {45000 + i:05d}  53.0000 247.4627 0001000 130.5360 {325.0 + i * 0.3:8.4f} 15.05000000563537"
        tles.append((f"STARLINK-{i}", base.format(n=45000 + i, p="ABCDEF"[i]), l2))
    svc = sat.SatelliteService()
    svc._tles["starlink"] = tles
    when = datetime(2008, 9, 20, 12, 25, 40, tzinfo=timezone.utc)
    info = svc._starlink(None, when)
    assert info["total"] == 6
    assert len(info["trains"]) == 1 and info["trains"][0]["count"] >= 5
