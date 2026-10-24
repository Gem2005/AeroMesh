from __future__ import annotations

from dataclasses import dataclass, field

from sim.constants import (
    DEFAULT_CHANNEL,
    ZONE_MODE_SPOT,
    ZONE_MODE_BARRAGE,
    STATUS_ONLINE,
)

# ------------------------------------------------------------------ #
# Node model
# ------------------------------------------------------------------ #
@dataclass
class MeshNode:
    id: str
    type: str  # "hardware" | "virtual"
    lat: float
    lng: float
    channel: int = DEFAULT_CHANNEL
    status: str = STATUS_ONLINE
    rssi: float | None = None
    snr: float | None = None
    battery_mv: float | None = None
    parent_id: str | None = None
    hop_path: list[str] = field(default_factory=list)
    reachable: bool = False
    # Hardware bookkeeping
    hw_id: int | None = None
    is_gateway: bool = False
    reported_status: str = STATUS_ONLINE
    reported_rssi: float | None = None
    distance_m: float | None = None
    proj_distance_m: float | None = None
    last_seen: float = 0.0
    offline_since: float | None = None
    null_streak: int = 0
    # Virtual kinematics
    index: int = 0
    ring: int = 0
    theta0: float = 0.0
    psi: float = 0.0
    target: tuple[float, float] | None = None  # polygon infill slot
    alt_m: float = 0.0  # AGL; non-zero only for airborne relay UAVs
    # Lost-link position hold
    hold_pos: tuple[float, float] | None = None
    hold_reason: str | None = None
    lost_link_since: float | None = None
    reconnect_streak: int = 0
    # RF attenuation state (set by the interference engine)
    rf_state: str = "CLEAR"  # CLEAR | DEGRADED | JAMMED
    rf_penalty_db: float = 0.0
    # Last Known Good Position: the only coordinates the gateway may use for a silent node.
    lkgp_lat: float = 0.0
    lkgp_lng: float = 0.0
    lkgp_ts: float = 0.0
    last_reachable_parent_id: str | None = None
    orphan_since: float | None = None      # sim time the node last lost its route
    stranded_since: float | None = None    # sim time a channel switch left it on the old channel
    hunt_jitter_s: float = 0.0             # per-node jitter on the orphan beacon-hunt watchdog

    def __post_init__(self) -> None:
        self.lkgp_lat, self.lkgp_lng = self.lat, self.lng

    @property
    def role(self) -> str:
        if self.is_gateway:
            return "gateway"
        if self.type == "uav":
            return "uav"
        return "edge" if self.type == "hardware" else "virtual"


@dataclass
class InterferenceZone:
    """A simulated high-attenuation / interference area on one Wi-Fi channel."""
    lat: float
    lng: float
    kill_radius_m: float
    bleed_radius_m: float
    channel: int
    freq_mhz: int
    deployed_at: float
    mode: str = ZONE_MODE_SPOT  # SPOT | BARRAGE | SWEEPER
    last_sweep_at: float = 0.0

    @property
    def barrage(self) -> bool:
        return self.mode == ZONE_MODE_BARRAGE

    def affects_channel(self, channel: int) -> bool:
        return self.barrage or channel == self.channel


def virtual_id(index: int) -> str:
    return f"0xV_{index:03d}"
