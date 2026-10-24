from __future__ import annotations

import math

from sim.constants import (
    TX_POWER,
    PATH_LOSS_EXPONENT,
    MIN_DISPLAY_SPREAD_M,
    MAX_DISPLAY_DISTANCE_M,
    UNKNOWN_SIGNAL_SPREAD_M,
    RSSI_1M_DBM,
    GROUND_BREAKPOINT_M,
    GROUND_FAR_EXPONENT,
    A2G_GRAZING_LOSS_DB,
    LINK_RSSI_MIN,
    _GROUND_BREAKPOINT_RSSI,
    RSSI_GOOD_DBM,
    LINK_COST_HOP,
    LINK_COST_REF_DBM,
    LINK_COST_SCALE_DB,
    PDR_LOGISTIC_CENTER_DBM,
    PDR_LOGISTIC_SCALE_DB,
    MAC_RETRIES,
    RELAY_LINK_MAX_M,
)

# ------------------------------------------------------------------ #
# Radio helpers
# ------------------------------------------------------------------ #
def rssi_to_meters(rssi):
    """
    Estimated distance in metres from a measured hardware RSSI (log-distance
    path loss). Returns None when RSSI is unavailable/invalid.
    """
    try:
        if rssi is None:
            return None
        val = float(rssi)
    except (ValueError, TypeError):
        return None
    if val >= 0:
        return None
    dist = 10 ** ((TX_POWER - val) / (10 * PATH_LOSS_EXPONENT))
    return min(dist, MAX_DISPLAY_DISTANCE_M)


def display_distance(dist_m, node_id: int) -> float:
    """Distance used only for map projection, with a minimum spread."""
    if dist_m is None:
        return UNKNOWN_SIGNAL_SPREAD_M + (node_id % 40)
    return max(dist_m, MIN_DISPLAY_SPREAD_M)


def hop_success(rssi: float) -> float:
    """Per-hop delivery probability including MAC retries."""
    p = 1.0 / (1.0 + math.exp(-(rssi - PDR_LOGISTIC_CENTER_DBM) / PDR_LOGISTIC_SCALE_DB))
    return 1.0 - (1.0 - p) ** (1 + MAC_RETRIES)


def link_cost(rssi: float) -> float:
    """Dijkstra edge weight: grows steeply as a link approaches the -88 dBm floor."""
    return LINK_COST_HOP + math.exp((LINK_COST_REF_DBM - rssi) / LINK_COST_SCALE_DB)


def ground_rssi_nominal(d: float) -> float:
    if d <= GROUND_BREAKPOINT_M:
        return RSSI_1M_DBM - 20 * math.log10(max(d, 1.0))
    return _GROUND_BREAKPOINT_RSSI - 10 * GROUND_FAR_EXPONENT * math.log10(d / GROUND_BREAKPOINT_M)


def air_rssi_nominal(slant_m: float, dz_m: float, air_to_air: bool) -> float:
    rssi = RSSI_1M_DBM - 20 * math.log10(max(slant_m, 1.0))
    if not air_to_air:
        rssi -= A2G_GRAZING_LOSS_DB * (1.0 - min(1.0, dz_m / max(slant_m, 1.0)))
    return rssi


def a2g_cover_radius_m(alt_m: float, rssi_dbm: float = RSSI_GOOD_DBM) -> float:
    """Horizontal radius under a relay at alt_m where the air-to-ground link stays above rssi_dbm."""
    if air_rssi_nominal(alt_m, alt_m, False) < rssi_dbm:
        return 0.0
    lo, hi = 0.0, RELAY_LINK_MAX_M
    for _ in range(30):
        mid = (lo + hi) / 2
        if air_rssi_nominal(math.hypot(mid, alt_m), alt_m, False) >= rssi_dbm:
            lo = mid
        else:
            hi = mid
    return lo


def link_quality(rssi: float) -> float:
    """0 at the -88 dBm link threshold, 1 at -70 dBm and above."""
    return max(0.0, min(1.0, (rssi - LINK_RSSI_MIN) / (-70.0 - LINK_RSSI_MIN)))


def _round(v, nd=2):
    return None if v is None else round(v, nd)
