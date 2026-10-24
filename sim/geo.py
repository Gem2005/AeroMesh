from __future__ import annotations

import math
from datetime import datetime, timezone

from sim.constants import EARTH_RADIUS_M

# ------------------------------------------------------------------ #
# Geometry helpers
# ------------------------------------------------------------------ #
def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def offset_m(lat: float, lng: float, east_m: float, north_m: float) -> tuple[float, float]:
    """Equirectangular offset: valid for the sub-kilometre spans used here."""
    dlat = north_m / EARTH_RADIUS_M
    dlng = east_m / (EARTH_RADIUS_M * math.cos(math.radians(lat)))
    return lat + math.degrees(dlat), lng + math.degrees(dlng)


def to_local_m(lat0: float, lng0: float, lat: float, lng: float) -> tuple[float, float]:
    """(east, north) metres of (lat, lng) relative to (lat0, lng0)."""
    north = math.radians(lat - lat0) * EARTH_RADIUS_M
    east = math.radians(lng - lng0) * EARTH_RADIUS_M * math.cos(math.radians(lat0))
    return east, north


def destination(lat: float, lng: float, meters: float, bearing_deg: float) -> tuple[float, float]:
    b = math.radians(bearing_deg)
    return offset_m(lat, lng, meters * math.sin(b), meters * math.cos(b))


def calculate_bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Forward azimuth in degrees from (lat1, lon1) to (lat2, lon2)."""
    dlon = math.radians(lon2 - lon1)
    lat1_r = math.radians(lat1)
    lat2_r = math.radians(lat2)
    x = math.sin(dlon) * math.cos(lat2_r)
    y = math.cos(lat1_r) * math.sin(lat2_r) - math.sin(lat1_r) * math.cos(lat2_r) * math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def move_toward(lat: float, lng: float, tlat: float, tlng: float, step_m: float) -> tuple[float, float, float]:
    """Moves at most step_m toward the target. Returns (lat, lng, remaining_m)."""
    d = haversine_m(lat, lng, tlat, tlng)
    if d <= step_m or d < 1e-6:
        return tlat, tlng, 0.0
    f = step_m / d
    return lat + (tlat - lat) * f, lng + (tlng - lng) * f, d - step_m


def point_in_polygon(x: float, y: float, poly: list[tuple[float, float]]) -> bool:
    """Ray casting (Jordan curve theorem) point-in-polygon test."""
    inside = False
    j = len(poly) - 1
    for i in range(len(poly)):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y):
            x_cross = (xj - xi) * (y - yi) / (yj - yi) + xi
            if x < x_cross:
                inside = not inside
        j = i
    return inside


def polygon_area(poly: list[tuple[float, float]]) -> float:
    s = 0.0
    for i in range(len(poly)):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % len(poly)]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


def channel_freq_mhz(channel: int) -> int:
    return 2407 + 5 * channel


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
