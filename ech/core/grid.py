"""
ech/core/grid.py
----------------
Maidenhead locator → latitude/longitude (centre of the square).
"""

from __future__ import annotations


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
