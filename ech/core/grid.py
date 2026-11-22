"""
ech/core/grid.py
----------------
Maidenhead locator → latitude/longitude (centre of the square).
"""

from __future__ import annotations


def latlon_to_grid(lat: float, lon: float, chars: int = 6) -> str:
    """Maidenhead locator (4 or 6 characters) for a position. 44.104, -69.125 -> FN54kc."""
    lon = min(max(float(lon), -180.0), 179.9999) + 180.0
    lat = min(max(float(lat), -90.0), 89.9999) + 90.0
    g = chr(65 + int(lon // 20)) + chr(65 + int(lat // 10))
    g += str(int(lon % 20 // 2)) + str(int(lat % 10))
    if chars >= 6:
        g += chr(97 + int(lon % 2 * 12)) + chr(97 + int(lat % 1 * 24))
    return g


def grid_to_latlon(grid: str) -> tuple[float | None, float | None]:
    """Centre of a 4- or 6-character Maidenhead square; (None, None) if invalid.

    FN54 → 44.5, -69.0 · FN54KC → 44.1042, -69.125
    """
    g = (grid or "").strip().upper()
    try:
        if len(g) < 4 or not ("A" <= g[0] <= "R" and "A" <= g[1] <= "R"):
            return None, None
        lon = (ord(g[0]) - 65) * 20 - 180 + int(g[2]) * 2
        lat = (ord(g[1]) - 65) * 10 - 90 + int(g[3])
        if len(g) >= 6 and "A" <= g[4] <= "X" and "A" <= g[5] <= "X":
            lon += (ord(g[4]) - 65 + 0.5) * 2 / 24
            lat += (ord(g[5]) - 65 + 0.5) / 24
        else:
            lon += 1.0
            lat += 0.5
        return round(lat, 4), round(lon, 4)
    except (ValueError, IndexError):
        return None, None
