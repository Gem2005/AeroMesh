from __future__ import annotations

import itertools
import math
import time
from dataclasses import dataclass

import networkx as nx

from sim.constants import (
    LINK_RSSI_MIN,
    RSSI_GOOD_DBM,
    SIM_TICK_S,
    SHEPHERD_BURNTHROUGH_SLANT_M,
    SHEPHERD_PLAN_RADIUS_M,
    MAST_ALT_M,
    FLANK_MARGIN_M,
    UAV_ID_PREFIX,
    UAV_DEFAULT_TYPE,
    UAV_SOFT_LIMIT,
    UAV_MAX_SYSID,
    UAV_BASE_ALT_M,
    UAV_ALT_LAYER_STEP_M,
    UAV_ALT_LAYERS,
    UAV_ALT_MIN_M,
    UAV_MESH_MIN_ALT_M,
    UAV_ARRIVAL_RADIUS_M,
    UAV_PAD_BASE_RADIUS_M,
    UAV_PAD_RING_STEP_M,
    UAV_PADS_PER_RING,
    UAV_PAD_START_BEARING,
    UAV_RECHARGE_PCT_PER_S,
    UAV_RELIEF_BATTERY_PCT,
    UAV_LOW_BATTERY_PCT,
    UAV_CRITICAL_BATTERY_PCT,
    UAV_DISPATCH_MIN_BATTERY_PCT,
    UAV_LOST_LINK_S,
    UAV_A2A_MAX_M,
    GEOFENCE_RADIUS_M,
    GEOFENCE_CEILING_M,
    RELAY_RTB_HOLD_S,
    RELAY_WAYPOINT_DEADBAND_M,
    RELAY_DISPATCH_CONFIRM_TICKS,
    RELAY_LINK_MAX_M,
    RELAY_CHAIN_SPACING_FACTOR,
    RELAY_MIN_STATION_S,
    ALLOCATOR_REPORT_MIN_INTERVAL_S,
    COVER_CONFIRM_TICKS,
    COVER_HYSTERESIS_DB,
    COVER_PLAN_ALT_M,
    COVER_RADIUS_MARGIN,
    COVER_MIN_GROUP,
    COVER_MATCH_FACTOR,
    COVER_RELEASE_HOLD_S,
    COVER_MIN_STATION_S,
    COVER_ENDURANCE_WEIGHT,
    COVER_REPORT_MIN_INTERVAL_S,
    UAV_ARM_DELAY_S,
    MODE_AUTO,
    MODE_GUIDED,
    MODE_LOITER,
    MODE_RTL,
    MODE_LAND,
    COPTER_CUSTOM_MODE,
    PLANE_CUSTOM_MODE,
    AIRFRAME_MULTIROTOR,
    AIRFRAME_VTOL,
    FS_LANDED,
    FS_TAKING_OFF,
    FS_TRANSIT,
    FS_ON_STATION,
    FS_HOLDING,
    FS_RETURNING,
    FS_LANDING,
    TASK_RELAY,
    TASK_RELIEF,
    TASK_COVER,
    TASK_GOTO,
    TASK_SHEPHERD,
    TASK_MAST,
    SLOT_RELAY,
    SLOT_COVER,
    SLOT_SHEPHERD,
    SLOT_MAST,
    SLOT_TASK,
    FAILSAFE_LOW_BATTERY,
    FAILSAFE_CRITICAL_BATTERY,
    FAILSAFE_LOST_LINK,
    FAILSAFE_GEOFENCE,
    MAV_CMD_NAV_WAYPOINT,
    STATUS_JAMMED,
    STATUS_OFFLINE,
)
from sim.geo import haversine_m, destination, calculate_bearing, move_toward
from sim.radio import a2g_cover_radius_m, _round
from sim.nodes import MeshNode


@dataclass(frozen=True)
class UavType:
    """Airframe profile. Every type carries the identical relay radio payload."""
    key: str
    name: str
    airframe: str
    mav_type: int
    cruise_speed_mps: float
    min_speed_mps: float
    max_speed_mps: float
    climb_mps: float
    descent_mps: float
    endurance_min: float
    speed_drain_factor: float       # extra drain at cruise relative to the base rate
    hover_drain_factor: float       # drain multiplier while hovering (VTOL lift motors)
    loiter_radius_m: float          # 0 = hovers in place on station

    @property
    def endurance_s(self) -> float:
        return self.endurance_min * 60.0

    @property
    def custom_modes(self) -> dict[str, int]:
        return PLANE_CUSTOM_MODE if self.airframe == AIRFRAME_VTOL else COPTER_CUSTOM_MODE

    def to_dict(self) -> dict:
        return {
            "key": self.key, "name": self.name, "airframe": self.airframe,
            "cruise_speed_mps": self.cruise_speed_mps, "min_speed_mps": self.min_speed_mps,
            "max_speed_mps": self.max_speed_mps, "climb_mps": self.climb_mps,
            "endurance_min": self.endurance_min, "loiter_radius_m": self.loiter_radius_m,
        }


UAV_TYPES: dict[str, UavType] = {t.key: t for t in (
    UavType("QUAD-S", "Light quadcopter", AIRFRAME_MULTIROTOR, 2,
            cruise_speed_mps=15.0, min_speed_mps=3.0, max_speed_mps=20.0, climb_mps=3.0, descent_mps=2.0,
            endurance_min=25.0, speed_drain_factor=0.35, hover_drain_factor=1.0, loiter_radius_m=0.0),
    UavType("HEXA-H", "Heavy-lift hexacopter", AIRFRAME_MULTIROTOR, 13,
            cruise_speed_mps=12.0, min_speed_mps=3.0, max_speed_mps=16.0, climb_mps=2.5, descent_mps=2.0,
            endurance_min=40.0, speed_drain_factor=0.3, hover_drain_factor=1.0, loiter_radius_m=0.0),
    UavType("VTOL-FW", "VTOL fixed-wing", AIRFRAME_VTOL, 22,
            cruise_speed_mps=22.0, min_speed_mps=16.0, max_speed_mps=28.0, climb_mps=3.5, descent_mps=2.5,
            endurance_min=90.0, speed_drain_factor=0.2, hover_drain_factor=3.0, loiter_radius_m=60.0),
)}


def parse_uav_mix(text: str) -> list[str]:
    """'QUAD-S:2,VTOL-FW:1' -> ['QUAD-S', 'QUAD-S', 'VTOL-FW']."""
    out: list[str] = []
    for part in filter(None, (p.strip() for p in text.split(","))):
        key, _, count = part.partition(":")
        key = key.strip().upper()
        if key not in UAV_TYPES:
            raise ValueError(f"unknown UAV type {key!r} (known: {', '.join(UAV_TYPES)})")
        out.extend([key] * (int(count) if count.strip() else 1))
    return out

# ------------------------------------------------------------------ #
# Relay UAV fleet
# ------------------------------------------------------------------ #
def uav_id(index: int) -> str:
    return f"{UAV_ID_PREFIX}{index:02d}"


def _coord(p: tuple[float, float] | None) -> list[float] | None:
    return [round(p[0], 7), round(p[1], 7)] if p else None


class UavAgent:
    """One relay UAV: 3D point-mass kinematics, per-airframe battery model and flight modes."""

    def __init__(self, index: int, home: tuple[float, float], battery_pct: float, utype: UavType):
        self.index = index
        self.id = uav_id(index)
        self.utype = utype
        self.sysid: int | None = index + 1 if index + 1 <= UAV_MAX_SYSID else None
        self.home = home
        self.node = MeshNode(id=self.id, type="uav", lat=home[0], lng=home[1])
        self.default_cruise_alt_m = UAV_BASE_ALT_M + UAV_ALT_LAYER_STEP_M * ((index - 1) % UAV_ALT_LAYERS)
        self.cruise_alt_m = self.default_cruise_alt_m
        self.cruise_speed_mps = utype.cruise_speed_mps
        self.mode = MODE_AUTO
        self.flight_state = FS_LANDED
        self.armed = False
        self.battery_pct = battery_pct
        self.target: tuple[float, float] | None = None
        self.hold_pos: tuple[float, float] | None = None
        self.task: str | None = None
        self.relieving: str | None = None   # id of the low-battery UAV this one is replacing
        self.relief_by: str | None = None   # id of the UAV inbound to replace this one
        self.slot_label: str | None = None
        self.unmatched_since: float | None = None
        self.auto_after_land = True
        self.remove_on_land = False
        self.failsafe: str | None = None
        self.ground_speed_mps = 0.0
        self.climb_mps = 0.0
        self.bearing_deg = 0.0
        self.eta_s: float | None = None
        self.mission_time_s = 0.0
        self.unreachable_since: float | None = None
        self.waypoint_seq = 0
        self.bridged_count = 0
        self.last_command: str | None = None

    @property
    def airborne(self) -> bool:
        return self.flight_state != FS_LANDED

    @property
    def in_mesh(self) -> bool:
        return self.airborne and self.node.alt_m >= UAV_MESH_MIN_ALT_M

    @property
    def loiters(self) -> bool:
        return self.utype.loiter_radius_m > 0

    @property
    def arrival_radius_m(self) -> float:
        return self.utype.loiter_radius_m + UAV_ARRIVAL_RADIUS_M if self.loiters else UAV_ARRIVAL_RADIUS_M

    def distance_to(self, p: tuple[float, float]) -> float:
        return haversine_m(self.node.lat, self.node.lng, p[0], p[1])

    # -- flight primitives (mode bookkeeping lives in FleetManager) --
    def set_target(self, target: tuple[float, float], keep_station: bool = False) -> None:
        self.target = target
        self.hold_pos = None
        if self.flight_state == FS_LANDED:
            self.armed = True
            self.flight_state = FS_TAKING_OFF
        elif keep_station and self.flight_state == FS_ON_STATION:
            pass  # station keeping: drift with the slot without leaving it
        elif self.flight_state != FS_TAKING_OFF:
            self.flight_state = FS_ON_STATION if self.distance_to(target) <= self.arrival_radius_m else FS_TRANSIT

    def takeoff(self) -> None:
        self.target = None
        self.armed = True
        self.flight_state = FS_TAKING_OFF

    def hold(self) -> None:
        self.target = None
        self.hold_pos = (self.node.lat, self.node.lng)
        if self.flight_state != FS_TAKING_OFF:
            self.flight_state = FS_HOLDING

    def rtl(self) -> None:
        self.target = None
        self.hold_pos = None
        self.flight_state = FS_RETURNING

    def land(self) -> None:
        self.target = None
        self.hold_pos = None
        self.flight_state = FS_LANDING

    def _loiter_point(self, center: tuple[float, float], dt: float) -> tuple[float, float]:
        """Next point on the loiter circle around center (a fixed-wing cannot hover)."""
        r = self.utype.loiter_radius_m
        n = self.node
        d = haversine_m(center[0], center[1], n.lat, n.lng)
        ang = calculate_bearing(center[0], center[1], n.lat, n.lng) if d >= 1.0 else self.bearing_deg
        if d < 0.7 * r:
            return destination(center[0], center[1], r, ang)
        return destination(center[0], center[1], r, ang + math.degrees(self.utype.min_speed_mps * dt / r))

    def step(self, dt: float) -> list[str]:
        """Advances one tick and returns flight events (TAKEOFF_COMPLETE, ARRIVED, OVER_HOME, LANDED)."""
        n = self.node
        t = self.utype
        if self.flight_state == FS_LANDED:
            n.alt_m = 0.0
            self.ground_speed_mps = self.climb_mps = 0.0
            self.eta_s = None
            if not self.armed:
                self.battery_pct = min(100.0, self.battery_pct + UAV_RECHARGE_PCT_PER_S * dt)
            return []

        fs = self.flight_state
        target_alt = 0.0 if fs == FS_LANDING else self.cruise_alt_m
        dz = target_alt - n.alt_m
        rate = t.climb_mps if dz > 0 else t.descent_mps
        step_z = max(-rate * dt, min(rate * dt, dz))
        n.alt_m = max(0.0, n.alt_m + step_z)
        self.climb_mps = step_z / dt

        goal = None
        if fs in (FS_TRANSIT, FS_ON_STATION) and self.target is not None:
            goal = self.target
        elif fs == FS_TAKING_OFF and self.target is not None and n.alt_m >= UAV_MESH_MIN_ALT_M:
            goal = self.target  # clear of the pad: climb diagonally toward the slot instead of vertically
        elif fs == FS_HOLDING and self.hold_pos is not None:
            goal = self.hold_pos
        elif fs == FS_RETURNING:
            goal = self.home

        remaining = 0.0
        if goal is not None and self.loiters and fs in (FS_ON_STATION, FS_HOLDING):
            off_circle = self.distance_to(goal) > self.arrival_radius_m + UAV_ARRIVAL_RADIUS_M
            p = goal if off_circle else self._loiter_point(goal, dt)
            d = self.distance_to(p)
            if d > 0.5:
                self.bearing_deg = calculate_bearing(n.lat, n.lng, p[0], p[1])
            speed = self.cruise_speed_mps if off_circle else t.min_speed_mps
            n.lat, n.lng, left = move_toward(n.lat, n.lng, p[0], p[1], speed * dt)
            self.ground_speed_mps = (d - left) / dt
            self.eta_s = None
        elif goal is not None:
            d = self.distance_to(goal)
            if d > 0.5:
                self.bearing_deg = calculate_bearing(n.lat, n.lng, goal[0], goal[1])
            n.lat, n.lng, remaining = move_toward(n.lat, n.lng, goal[0], goal[1], self.cruise_speed_mps * dt)
            self.ground_speed_mps = (d - remaining) / dt
            self.eta_s = remaining / self.cruise_speed_mps
        else:
            self.ground_speed_mps = 0.0
            self.eta_s = None

        events = []
        if fs == FS_TAKING_OFF and abs(self.cruise_alt_m - n.alt_m) < 0.5:
            if self.target is not None:
                self.flight_state = FS_TRANSIT
            else:
                self.hold_pos = (n.lat, n.lng)
                self.flight_state = FS_HOLDING
            events.append("TAKEOFF_COMPLETE")
        elif fs == FS_TRANSIT and remaining <= self.arrival_radius_m:
            self.flight_state = FS_ON_STATION
            events.append("ARRIVED")
        elif fs == FS_RETURNING and remaining <= 1.0:
            self.flight_state = FS_LANDING
            events.append("OVER_HOME")
        elif fs == FS_LANDING and n.alt_m <= 0.01:
            n.alt_m = 0.0
            self.flight_state = FS_LANDED
            self.armed = False
            self.ground_speed_mps = self.climb_mps = 0.0
            self.eta_s = None
            events.append("LANDED")

        if self.flight_state != FS_LANDED:
            self.mission_time_s += dt
            self.battery_pct = max(0.0, self.battery_pct - (100.0 / t.endurance_s) * self._load(fs) * dt)
        return events

    def _load(self, fs: str) -> float:
        """Drain relative to the rated endurance (multirotor: hover, fixed-wing: cruise)."""
        t = self.utype
        speed_ratio = min(1.5, self.ground_speed_mps / t.cruise_speed_mps)
        if t.airframe == AIRFRAME_VTOL:
            if fs in (FS_TAKING_OFF, FS_LANDING) or self.ground_speed_mps < 0.5 * t.min_speed_mps:
                return t.hover_drain_factor
            return (1.0 - t.speed_drain_factor) + t.speed_drain_factor * speed_ratio
        return t.hover_drain_factor + t.speed_drain_factor * speed_ratio

    def endurance_remaining_s(self) -> float:
        return self.utype.endurance_s * self.battery_pct / 100.0

    def usable_endurance_s(self) -> float:
        """Flight time left before the low-battery RTL failsafe fires."""
        return self.utype.endurance_s * max(0.0, self.battery_pct - UAV_LOW_BATTERY_PCT) / 100.0


class FleetManager:
    """Relay UAV fleet: add/remove, operator commands, failsafes and the relay allocator."""

    def __init__(self, bridge: "Bridge", types: list[str]):
        self.bridge = bridge
        self.uavs: dict[str, UavAgent] = {}
        self.isolation_streak = 0
        self.clusters: list[dict] = []
        self.slots: list[dict] = []
        self.cover_groups: list[dict] = []
        self.coverage_enabled = True
        self._cover_labels: list[tuple[str, float, float]] = []
        self.weak_streak: dict[str, int] = {}
        self.weak_nodes: set[str] = set()
        self.shortfall = 0
        self.assigned = 0
        self.cover_assigned = 0
        self.shepherd_assigned = 0
        self.mast_assigned = 0
        self.mission_slot_count = 0
        self._last_report: tuple | None = None
        self._last_report_at = -1e9
        self._last_cover_report: tuple | None = None
        self._last_cover_report_at = -1e9
        self.gateway_in_zone = False
        self.cover_plan_radius_m = COVER_RADIUS_MARGIN * a2g_cover_radius_m(COVER_PLAN_ALT_M)
        for key in types:
            self.add(1, key, announce=False)

    # -- bookkeeping --
    def now(self) -> float:
        return self.bridge.tick_count * SIM_TICK_S

    def pad_position(self, index: int) -> tuple[float, float]:
        ring, slot = divmod(index - 1, UAV_PADS_PER_RING)
        radius = UAV_PAD_BASE_RADIUS_M + UAV_PAD_RING_STEP_M * ring
        bearing = UAV_PAD_START_BEARING + slot * (360.0 / UAV_PADS_PER_RING) + ring * 15.0
        gw_lat, gw_lng = self.bridge.gateway_pos
        return destination(gw_lat, gw_lng, radius, bearing)

    def update_homes(self) -> None:
        for u in self.uavs.values():
            u.home = self.pad_position(u.index)
            if u.flight_state == FS_LANDED:
                u.node.lat, u.node.lng = u.home

    def mesh_nodes(self) -> list[MeshNode]:
        return [u.node for u in self.uavs.values() if u.in_mesh]

    def _free_index(self) -> int:
        used = {u.index for u in self.uavs.values()}
        idx = 1
        while idx in used:
            idx += 1
        return idx

    def add(self, count: int, uav_type: str | None = None, announce: bool = True) -> list[str]:
        count = max(0, int(count))
        key = (uav_type or UAV_DEFAULT_TYPE).upper()
        utype = UAV_TYPES.get(key)
        if utype is None:
            raise ValueError(f"unknown UAV type {key} (known: {', '.join(UAV_TYPES)})")
        before = len(self.uavs)
        ids = []
        for _ in range(count):
            idx = self._free_index()
            u = UavAgent(idx, self.pad_position(idx), self.bridge.rng.uniform(92.0, 100.0), utype)
            self.uavs[u.id] = u
            ids.append(u.id)
        self.uavs = dict(sorted(self.uavs.items(), key=lambda kv: kv[1].index))
        if announce and ids:
            span = ids[0] if len(ids) == 1 else f"{ids[0]}..{ids[-1]}"
            self.bridge.log_event(
                "FLEET", "ok",
                f"Added {len(ids)} {utype.key} {utype.name} relay UAV(s) ({span}) on home pads; fleet size {len(self.uavs)}",
            )
        if before <= UAV_SOFT_LIMIT < len(self.uavs):
            self.bridge.log_event(
                "FLEET", "warn",
                f"Fleet above {UAV_SOFT_LIMIT} UAVs: link computation grows with the square of node count; expect slower ticks",
            )
        return ids

    def remove(self, uav_id: str) -> tuple[bool, str]:
        u = self.uavs.get(uav_id)
        if u is None:
            return False, f"unknown UAV {uav_id}"
        if u.airborne:
            self.release(u)
            u.mode = MODE_RTL
            u.auto_after_land = False
            u.remove_on_land = True
            u.rtl()
            return True, f"{u.id} returning to pad; it will be removed after landing"
        self.release(u)
        del self.uavs[uav_id]
        return True, f"{uav_id} removed from fleet ({len(self.uavs)} remaining)"

    def release(self, u: UavAgent) -> None:
        """Drops any relay/relief/shepherd/mast assignment held by this UAV."""
        if u.task == TASK_MAST:
            u.cruise_alt_m = u.default_cruise_alt_m  # leave the high-mast layer
        if u.relieving:
            other = self.uavs.get(u.relieving)
            if other is not None:
                other.relief_by = None
            u.relieving = None
        if u.relief_by:
            reliever = self.uavs.get(u.relief_by)
            if reliever is not None:
                reliever.relieving = None
                reliever.task = TASK_RELAY
                reliever.slot_label = u.slot_label
            u.relief_by = None
        u.task = None
        u.slot_label = None
        u.unmatched_since = None

    def event(self, u: UavAgent, severity: str, text: str) -> None:
        self.bridge.uav_event(u, severity, text)

    # -- operator / GCS commands --
    def by_sysid(self, sysid: int) -> UavAgent | None:
        return next((u for u in self.uavs.values() if u.sysid == sysid), None)

    def is_own_sysid(self, sysid: int) -> bool:
        return self.by_sysid(sysid) is not None

    def command(self, uav_id: str, action: str, source: str = "DASHBOARD", **args) -> tuple[bool, str]:
        u = self.uavs.get(uav_id)
        if u is None:
            return False, f"unknown UAV {uav_id}"
        action = str(action).upper()
        if action == "LOITER":
            action = "HOLD"
        try:
            ok, text = self._apply_command(u, action, args)
        except (KeyError, TypeError, ValueError) as exc:
            ok, text = False, f"bad parameters ({exc})"
        if ok:
            u.last_command = f"{action} via {source}"
            self.bridge.log_event("FLEET", "info", f"{u.id} {action} accepted from {source}: {text}")
        else:
            self.bridge.log_event("FLEET", "warn", f"{u.id} {action} rejected from {source}: {text}")
        return ok, text

    def _validate_alt(self, alt) -> float:
        alt = float(alt)
        if not UAV_ALT_MIN_M <= alt <= GEOFENCE_CEILING_M:
            raise ValueError(f"altitude {alt:.0f} m outside {UAV_ALT_MIN_M:.0f}-{GEOFENCE_CEILING_M:.0f} m AGL")
        return alt

    def _validate_speed(self, u: UavAgent, speed) -> float:
        speed = float(speed)
        t = u.utype
        if not t.min_speed_mps <= speed <= t.max_speed_mps:
            raise ValueError(f"speed {speed:.1f} m/s outside the {t.key} envelope {t.min_speed_mps:.0f}-{t.max_speed_mps:.0f} m/s")
        return speed

    def inside_geofence(self, lat: float, lng: float) -> bool:
        gw_lat, gw_lng = self.bridge.gateway_pos
        return haversine_m(gw_lat, gw_lng, lat, lng) <= GEOFENCE_RADIUS_M

    def _apply_command(self, u: UavAgent, action: str, args: dict) -> tuple[bool, str]:
        alt = args.get("alt_m")
        speed = args.get("speed_mps")
        if action == "TAKEOFF":
            if u.airborne:
                return False, "already airborne"
            if u.battery_pct <= UAV_LOW_BATTERY_PCT:
                return False, f"battery {u.battery_pct:.0f}% too low for takeoff"
            if alt is not None:
                u.cruise_alt_m = self._validate_alt(alt)
            self.release(u)
            u.mode = MODE_GUIDED
            u.failsafe = None
            u.auto_after_land = False
            u.takeoff()
            return True, f"climbing to {u.cruise_alt_m:.0f} m, then holding over pad"
        if action == "HOLD":
            if not u.airborne:
                return False, "on the ground"
            if u.battery_pct <= UAV_CRITICAL_BATTERY_PCT:
                return False, "battery critical; landing cannot be overridden"
            self.release(u)
            u.mode = MODE_LOITER
            u.failsafe = None
            u.hold()
            return True, f"holding at {u.node.lat:.5f}, {u.node.lng:.5f}"
        if action == "RTL":
            if not u.airborne:
                return False, "on the ground"
            self.release(u)
            u.mode = MODE_RTL
            u.auto_after_land = False
            u.rtl()
            return True, "returning to home pad"
        if action == "LAND":
            if not u.airborne:
                return False, "already landed"
            self.release(u)
            u.mode = MODE_LAND
            u.auto_after_land = False
            u.land()
            return True, "landing in place"
        if action == "AUTO":
            if u.task in (TASK_GOTO, None):
                self.release(u)
            u.mode = MODE_AUTO
            u.auto_after_land = True
            u.unmatched_since = None
            if u.airborne and u.flight_state not in (FS_RETURNING, FS_LANDING):
                u.hold()
            return True, "relay allocator in control"
        if action == "GUIDED":
            if u.battery_pct <= UAV_CRITICAL_BATTERY_PCT and u.airborne:
                return False, "battery critical; landing cannot be overridden"
            self.release(u)
            u.mode = MODE_GUIDED
            u.failsafe = None
            u.auto_after_land = False
            if u.airborne and u.flight_state != FS_TAKING_OFF:
                u.hold()
                return True, "guided; holding position for fly-to commands"
            return True, "guided; awaiting commands"
        if action == "GOTO":
            lat, lng = float(args["lat"]), float(args["lng"])
            if not self.inside_geofence(lat, lng):
                return False, f"target outside {GEOFENCE_RADIUS_M:.0f} m geofence"
            if u.battery_pct <= UAV_LOW_BATTERY_PCT:
                return False, f"battery {u.battery_pct:.0f}% too low"
            if alt is not None:
                u.cruise_alt_m = self._validate_alt(alt)
            if speed is not None:
                u.cruise_speed_mps = self._validate_speed(u, speed)
            self.release(u)
            u.mode = MODE_GUIDED
            u.failsafe = None
            u.auto_after_land = False
            u.task = TASK_GOTO
            self._send_to(u, (lat, lng))
            return True, f"flying to {lat:.5f}, {lng:.5f} at {u.cruise_alt_m:.0f} m"
        if action == "SET_ALT":
            u.cruise_alt_m = self._validate_alt(alt)
            return True, f"cruise altitude {u.cruise_alt_m:.0f} m"
        if action == "SET_SPEED":
            u.cruise_speed_mps = self._validate_speed(u, speed)
            return True, f"cruise speed {u.cruise_speed_mps:.1f} m/s"
        if action == "ARM":
            if u.airborne:
                return True, "already armed"
            u.armed = True
            return True, "armed on pad"
        if action == "DISARM":
            if u.airborne:
                return False, "cannot disarm in flight"
            u.armed = False
            return True, "disarmed"
        return False, f"unsupported action {action}"

    def fleet_command(self, action: str) -> tuple[int, int]:
        """Applies one action to every eligible UAV. Returns (accepted, eligible)."""
        action = str(action).upper()
        per_uav = {"LAUNCH_ALL": "TAKEOFF", "AUTO_ALL": "AUTO", "RTL_ALL": "RTL", "LAND_ALL": "LAND"}.get(action)
        if per_uav is None:
            raise ValueError(f"unsupported fleet action {action}")
        if per_uav == "TAKEOFF":
            eligible = [u for u in self.uavs.values() if not u.airborne]
        elif per_uav == "AUTO":
            eligible = list(self.uavs.values())
        else:
            eligible = [u for u in self.uavs.values() if u.airborne]
        accepted = 0
        for u in eligible:
            ok, _ = self._apply_command(u, per_uav, {})
            if ok:
                u.last_command = f"{action} via DASHBOARD"
                accepted += 1
        self.bridge.log_event("FLEET", "info" if accepted else "warn", f"{action}: {accepted}/{len(eligible)} UAV(s) accepted")
        return accepted, len(eligible)

    def _send_to(self, u: UavAgent, point: tuple[float, float], keep_station: bool = False) -> None:
        u.set_target(point, keep_station)
        u.waypoint_seq += 1
        u.last_command = "MAV_CMD_NAV_WAYPOINT"
        if u.sysid is not None:
            self.bridge.mavlink.send_waypoint(u, point[0], point[1], u.cruise_alt_m)

    # -- per-tick --
    def step(self, dt: float) -> None:
        for u in list(self.uavs.values()):
            for ev in u.step(dt):
                self._on_flight_event(u, ev)

    def _on_flight_event(self, u: UavAgent, ev: str) -> None:
        if ev == "ARRIVED":
            if u.task in (TASK_RELAY, TASK_RELIEF):
                self.event(u, "ok", f"{u.id} on station at {u.slot_label or 'relay slot'} ({u.node.lat:.5f}, {u.node.lng:.5f}, {u.cruise_alt_m:.0f} m); BRIDGING_ACTIVE")
            elif u.task == TASK_COVER:
                how = f"loitering r={u.utype.loiter_radius_m:.0f} m" if u.loiters else "hovering"
                self.event(u, "ok", f"{u.id} on station over coverage {u.slot_label} at {u.cruise_alt_m:.0f} m, {how}")
            elif u.task == TASK_MAST:
                self.event(
                    u, "ok",
                    f"{u.id} HIGH-MAST on station {u.cruise_alt_m:.0f} m above the gateway; vertical burn-through link up, "
                    f"acting as airborne gateway proxy",
                )
            elif u.task == TASK_SHEPHERD:
                self.event(
                    u, "ok",
                    f"{u.id} shepherd over {u.slot_label} LKGP at {u.cruise_alt_m:.0f} m; beaming CH {self.bridge.active_channel} "
                    f"handoff to stranded nodes within {SHEPHERD_BURNTHROUGH_SLANT_M:.0f} m slant",
                )
            elif u.task == TASK_GOTO:
                self.event(u, "ok", f"{u.id} reached Go-To target; holding position")
        elif ev == "LANDED":
            self.event(u, "info", f"{u.id} landed on pad ({u.battery_pct:.0f}% battery); disarmed and charging")
            if u.remove_on_land:
                self.release(u)
                del self.uavs[u.id]
                self.bridge.log_event("FLEET", "info", f"{u.id} removed from fleet ({len(self.uavs)} remaining)")
                return
            if u.auto_after_land:
                u.mode = MODE_AUTO
            u.failsafe = None

    def evaluate(self) -> None:
        """Runs after the topology pass: bridged counts, failsafes, then the allocator."""
        b = self.bridge
        now = self.now()
        for u in self.uavs.values():
            u.bridged_count = sum(1 for n in b.all_nodes() if u.id in n.hop_path)
            if u.in_mesh and not u.node.reachable:
                u.unreachable_since = u.unreachable_since if u.unreachable_since is not None else now
            else:
                u.unreachable_since = None
        for u in list(self.uavs.values()):
            self._check_failsafes(u, now)
        self.allocate(now)

    def _trigger_failsafe(self, u: UavAgent, kind: str, land: bool, text: str) -> None:
        u.failsafe = kind
        u.auto_after_land = u.mode == MODE_AUTO
        self.release(u)
        if land:
            u.mode = MODE_LAND
            u.land()
        else:
            u.mode = MODE_RTL
            u.rtl()
        self.event(u, "critical" if land else "warn", f"{u.id} FAILSAFE {kind}: {text}")

    def _check_failsafes(self, u: UavAgent, now: float) -> None:
        if not u.airborne or u.flight_state == FS_LANDING:
            return
        if u.battery_pct <= UAV_CRITICAL_BATTERY_PCT:
            self._trigger_failsafe(u, FAILSAFE_CRITICAL_BATTERY, True, f"battery {u.battery_pct:.0f}%; landing in place")
            return
        if u.flight_state == FS_RETURNING:
            return
        if u.battery_pct <= UAV_LOW_BATTERY_PCT:
            self._trigger_failsafe(u, FAILSAFE_LOW_BATTERY, False, f"battery {u.battery_pct:.0f}%; returning to pad")
        elif not self.inside_geofence(u.node.lat, u.node.lng):
            self._trigger_failsafe(u, FAILSAFE_GEOFENCE, False, f"left the {GEOFENCE_RADIUS_M:.0f} m geofence; returning to pad")
        elif u.unreachable_since is not None and now - u.unreachable_since >= UAV_LOST_LINK_S:
            # While the gateway itself sits in the interference the loss of C2 is the gateway's
            # fault, not the airframe's: the fleet keeps flying its AUTO mission (HIGH-MAST
            # especially must be allowed to climb through the jammed layer to reach station).
            if u.task == TASK_MAST or self.bridge.gateway_in_zone():
                return
            self._trigger_failsafe(u, FAILSAFE_LOST_LINK, False, f"no C2 route to gateway for {UAV_LOST_LINK_S:.0f} s; returning to pad")

    # -- relay allocator --
    def _available(self, u: UavAgent) -> bool:
        return (
            u.mode == MODE_AUTO
            and u.failsafe is None
            and u.task is None
            and not u.remove_on_land
            and u.flight_state != FS_LANDING
            and u.battery_pct >= UAV_DISPATCH_MIN_BATTERY_PCT
        )

    @staticmethod
    def _known_pos(n: MeshNode) -> tuple[float, float]:
        """Coordinates the gateway may legitimately plan with: live if routed, else the LKGP."""
        return (n.lat, n.lng) if n.reachable else (n.lkgp_lat, n.lkgp_lng)

    def _flank(self, lat: float, lng: float) -> tuple[float, float, tuple[float, float] | None]:
        """Pushes a waypoint inside the active kill radius out to the safe flanking arc.

        A relay at normal altitude inside the drop zone (on the interfered channel, or in a
        wideband barrage on any channel) would be jammed itself, so the slot is projected
        radially from the zone centre to kill_radius_m + FLANK_MARGIN_M.
        """
        zone = self.bridge.zone
        if zone is None or not zone.affects_channel(self.bridge.active_channel):
            return lat, lng, None
        d = haversine_m(zone.lat, zone.lng, lat, lng)
        if d > zone.kill_radius_m:
            return lat, lng, None
        gw_lat, gw_lng = self.bridge.gateway_pos
        bearing = calculate_bearing(zone.lat, zone.lng, lat, lng) if d >= 1.0 else calculate_bearing(zone.lat, zone.lng, gw_lat, gw_lng)
        flat, flng = destination(zone.lat, zone.lng, zone.kill_radius_m + FLANK_MARGIN_M, bearing)
        return flat, flng, (lat, lng)

    def _plan_shepherd_slots(self, stranded: list[MeshNode]) -> list[dict]:
        """Greedy LKGP discs: one Shepherd UAV over each disc burns the handoff through."""
        b = self.bridge
        gw_lat, gw_lng = b.gateway_pos

        def xy(lat: float, lng: float) -> tuple[float, float]:
            return (
                haversine_m(gw_lat, gw_lng, gw_lat, lng) * (1 if lng >= gw_lng else -1),
                haversine_m(gw_lat, gw_lng, lat, gw_lng) * (1 if lat >= gw_lat else -1),
            )

        pts = {n.id: xy(n.lkgp_lat, n.lkgp_lng) for n in stranded}
        by_id = {n.id: n for n in stranded}
        r = SHEPHERD_PLAN_RADIUS_M
        left = set(pts)
        near = {a: {c for c in left if math.hypot(pts[a][0] - pts[c][0], pts[a][1] - pts[c][1]) <= r} for a in left}
        groups: list[list[str]] = []
        while left:
            seed = max(left, key=lambda a: (len(near[a] & left), -math.hypot(*pts[a]), a))
            members = sorted(near[seed] & left)
            left -= set(members)
            groups.append(members)
        groups.sort(key=len, reverse=True)
        slots = []
        for gi, members in enumerate(groups, start=1):
            lat = sum(by_id[m].lkgp_lat for m in members) / len(members)
            lng = sum(by_id[m].lkgp_lng for m in members) / len(members)
            if not self.inside_geofence(lat, lng):
                lat, lng = destination(gw_lat, gw_lng, GEOFENCE_RADIUS_M * 0.95, calculate_bearing(gw_lat, gw_lng, lat, lng))
            slots.append({
                "kind": SLOT_SHEPHERD, "lat": lat, "lng": lng, "alt_m": None, "cluster": gi, "index": 1, "of": 1,
                "size": len(members), "label": f"SH-{gi}", "members": members, "flanked": False, "flank_from": None,
            })
        return slots

    def _plan_slots(self) -> list[dict]:
        """Mission slots in priority order: HIGH-MAST over the gateway, Shepherd discs over
        stranded LKGPs, then relay points (midpoint or chain) for every isolated cluster."""
        b = self.bridge
        by_id = {n.id: n for n in b.all_nodes()}
        gw_lat, gw_lng = b.gateway_pos
        zone = b.zone
        slots: list[dict] = []
        self.clusters = []

        # 1. Protocol HIGH-MAST: the gateway itself is inside the interference.
        if b.gateway_in_zone():
            slots.append({
                "kind": SLOT_MAST, "lat": gw_lat, "lng": gw_lng, "alt_m": MAST_ALT_M, "cluster": 0, "index": 1, "of": 1,
                "size": 1, "label": "MAST-GW", "flanked": False, "flank_from": None,
            })

        # 2. Shepherd slots for nodes stuck on an old channel. Inside a barrage kill radius no
        #    channel is clean, so those nodes cannot be retuned until the zone clears.
        stranded = [
            n for n in b.all_nodes()
            if not n.is_gateway and n.channel != b.active_channel
            and not (n.type == "hardware" and n.reported_status == STATUS_OFFLINE)
        ]
        if zone is not None and zone.barrage:
            stranded = [n for n in stranded if haversine_m(n.lkgp_lat, n.lkgp_lng, zone.lat, zone.lng) > zone.kill_radius_m]
        slots += self._plan_shepherd_slots(stranded)

        # 3. Relay slots. While the gateway radio is fully jammed every node looks isolated,
        #    so relay planning waits for the mast (or a channel evacuation) to bring it back.
        if b.gateway.rf_state == STATUS_JAMMED:
            return slots
        isolated = [nid for nid in b.isolated_ground if nid in by_id and by_id[nid].channel == b.active_channel]
        components = nx.connected_components(b.mesh_graph.subgraph(isolated).to_undirected())
        clusters = sorted((list(c) for c in components), key=len, reverse=True)
        anchors = [n for n in b.all_nodes() if n.id in b.ground_paths and n.status != STATUS_JAMMED]
        hop_m = RELAY_CHAIN_SPACING_FACTOR * RELAY_LINK_MAX_M
        a2a_hop_m = RELAY_CHAIN_SPACING_FACTOR * UAV_A2A_MAX_M
        pos = {nid: self._known_pos(by_id[nid]) for nid in isolated}
        for ci, members in enumerate(clusters, start=1):
            c_lat = sum(pos[m][0] for m in members) / len(members)
            c_lng = sum(pos[m][1] for m in members) / len(members)
            # Bridge the shortest gap between the connected mesh and the cluster; the
            # cluster is internally connected, so one bridged member reconnects all of it.
            dist, anchor, entry = min(
                (haversine_m(a.lat, a.lng, pos[m][0], pos[m][1]), a, by_id[m])
                for a in (anchors or [b.gateway]) for m in members
            )
            entry_pos = pos[entry.id]
            # End relays sit one air-to-ground hop from each side; any middle
            # relays only need the longer air-to-air hop between UAVs.
            if dist <= 2 * hop_m:
                fractions = [0.5]
            else:
                middle = max(0, math.ceil((dist - 2 * hop_m) / a2a_hop_m) - 1)
                k = 2 + middle
                start, end = hop_m / dist, 1.0 - hop_m / dist
                fractions = [start + (end - start) * j / (k - 1) for j in range(k)]
            k = len(fractions)
            self.clusters.append({
                "id": ci, "size": len(members), "centroid": _coord((c_lat, c_lng)),
                "anchor_id": anchor.id, "anchor": _coord((anchor.lat, anchor.lng)),
                "entry_id": entry.id, "entry": _coord(entry_pos),
                "distance_m": round(dist, 1), "relays_needed": k,
            })
            for i, f in enumerate(fractions, start=1):
                lat = anchor.lat + (entry_pos[0] - anchor.lat) * f
                lng = anchor.lng + (entry_pos[1] - anchor.lng) * f
                if not self.inside_geofence(lat, lng):
                    lat, lng = destination(gw_lat, gw_lng, GEOFENCE_RADIUS_M * 0.95, calculate_bearing(gw_lat, gw_lng, lat, lng))
                lat, lng, flank_from = self._flank(lat, lng)
                label = f"C{ci}-R{i}" if k > 1 else f"C{ci}"
                slots.append({
                    "kind": SLOT_RELAY, "lat": lat, "lng": lng, "alt_m": None, "cluster": ci, "index": i, "of": k,
                    "size": len(members), "label": label, "flanked": flank_from is not None, "flank_from": flank_from,
                })
        return slots

    def _update_weak(self) -> None:
        """Tracks virtual nodes whose ground-only next hop sits below the good band (with hysteresis)."""
        b = self.bridge
        score = {}
        for n in b.all_nodes():
            if n.is_gateway or n.type != "virtual" or n.rf_state == STATUS_JAMMED:
                continue
            r = b.ground_link_rssi.get(n.id)
            if r is None:
                continue
            k = self.weak_streak.get(n.id, 0)
            if r < RSSI_GOOD_DBM:
                k = min(2 * COVER_CONFIRM_TICKS, k + 1)
            elif r > RSSI_GOOD_DBM + COVER_HYSTERESIS_DB:
                k -= 1
            if k > 0:
                score[n.id] = k
        self.weak_streak = score
        self.weak_nodes = {nid for nid, k in score.items() if k >= COVER_CONFIRM_TICKS}

    def _plan_cover_slots(self) -> list[dict]:
        """Sticky disc cover of the weak nodes; one UAV over each disc lifts them to the good band.

        Areas from the previous tick (UAV-held ones first) re-claim the weak nodes still inside
        their disc and keep their label, so slots drift with the orbit instead of re-forming.
        Only the remaining weak nodes are grouped fresh, greedily by disc population.
        """
        b = self.bridge
        by_id = {n.id: n for n in b.all_nodes()}
        gw_lat, gw_lng = b.gateway_pos

        def xy(lat: float, lng: float) -> tuple[float, float]:
            return (
                haversine_m(gw_lat, gw_lng, gw_lat, lng) * (1 if lng >= gw_lng else -1),
                haversine_m(gw_lat, gw_lng, lat, gw_lng) * (1 if lat >= gw_lat else -1),
            )

        pts = {nid: xy(by_id[nid].lat, by_id[nid].lng) for nid in self.weak_nodes if nid in by_id}
        r = self.cover_plan_radius_m
        left = set(pts)
        groups: list[tuple[str | None, list[str]]] = []
        seen: set[str] = set()
        previous = [
            (u.slot_label, u.target[0], u.target[1]) for u in self.uavs.values()
            if u.task == TASK_COVER and u.slot_label and u.target is not None
        ] + self._cover_labels
        for label, lat, lng in previous:
            if label in seen:
                continue
            seen.add(label)
            c = xy(lat, lng)
            members = sorted(a for a in left if math.hypot(pts[a][0] - c[0], pts[a][1] - c[1]) <= r)
            if members:
                groups.append((label, members))
                left -= set(members)
        near = {a: {c for c in left if math.hypot(pts[a][0] - pts[c][0], pts[a][1] - pts[c][1]) <= r} for a in left}
        while left:
            seed = max(left, key=lambda a: (len(near[a] & left), math.hypot(*pts[a]), a))
            members = sorted(near[seed] & left)
            left -= set(members)
            groups.append((None, members))
        groups = [g for g in groups if len(g[1]) >= COVER_MIN_GROUP]
        groups.sort(key=lambda g: len(g[1]), reverse=True)

        slots = []
        self.cover_groups = []
        self._cover_labels = []
        taken = {g[0] for g in groups if g[0]} | {
            u.slot_label for u in self.uavs.values() if u.task == TASK_COVER and u.slot_label
        }
        free = (f"W{k}" for k in itertools.count(1) if f"W{k}" not in taken)
        for gi, (label, members) in enumerate(groups, start=1):
            lat = sum(by_id[m].lat for m in members) / len(members)
            lng = sum(by_id[m].lng for m in members) / len(members)
            if not self.inside_geofence(lat, lng):
                lat, lng = destination(gw_lat, gw_lng, GEOFENCE_RADIUS_M * 0.95, calculate_bearing(gw_lat, gw_lng, lat, lng))
            if label is None:
                label = next(free)
            worst = min(b.ground_link_rssi.get(m, LINK_RSSI_MIN) for m in members)
            self._cover_labels.append((label, lat, lng))
            self.cover_groups.append({
                "id": gi, "label": label, "size": len(members), "centroid": _coord((lat, lng)),
                "worst_rssi": round(worst, 1), "members": members,
            })
            slots.append({
                "kind": SLOT_COVER, "lat": lat, "lng": lng, "cluster": gi, "index": 1, "of": 1,
                "size": len(members), "label": label, "worst_rssi": worst,
            })
        return slots

    def _dispatch_cost(self, u: UavAgent, point: tuple[float, float], kind: str) -> float | None:
        """Lower is better; None when the airframe cannot fly out, hold the slot and return."""
        t = u.utype
        eta = u.distance_to(point) / u.cruise_speed_mps
        if not u.airborne:
            eta += UAV_ARM_DELAY_S + u.cruise_alt_m / t.climb_mps
        back = haversine_m(point[0], point[1], u.home[0], u.home[1]) / u.cruise_speed_mps + u.cruise_alt_m / t.descent_mps
        station = u.usable_endurance_s() - eta - back
        if station < (COVER_MIN_STATION_S if kind == SLOT_COVER else RELAY_MIN_STATION_S):
            return None
        if kind == SLOT_MAST:
            # The mast hovers over the gateway for as long as the zone lasts: the healthiest
            # multirotor wins (a fixed-wing cannot hold a 25 m vertical station).
            if u.utype.airframe != AIRFRAME_MULTIROTOR:
                return None
            return -u.battery_pct + eta / 1000.0
        if kind in (SLOT_RELAY, SLOT_SHEPHERD):
            return eta  # partitions and stranded nodes are urgent: fastest arrival wins
        return eta - COVER_ENDURANCE_WEIGHT * station  # persistent duty favours long-endurance airframes

    def _pick(self, pool: list[UavAgent], s: dict) -> UavAgent | None:
        point = (s["lat"], s["lng"])
        scored = [(c, u.index, u) for u in pool if (c := self._dispatch_cost(u, point, s["kind"])) is not None]
        return min(scored)[2] if scored else None

    def _handoff_low_cover(self) -> None:
        for u in self.uavs.values():
            if u.task == TASK_COVER and u.battery_pct <= UAV_RELIEF_BATTERY_PCT:
                label = u.slot_label
                self.release(u)
                u.auto_after_land = True
                u.rtl()
                self.event(u, "info", f"{u.id} handing off coverage {label} at {u.battery_pct:.0f}% battery; returning to pad")

    MISSION_TASKS = (TASK_RELAY, TASK_SHEPHERD, TASK_MAST)

    def allocate(self, now: float) -> None:
        b = self.bridge
        gateway_in_zone = b.gateway_in_zone()
        # Stranded nodes (old channel / jammed) are invisible to the ground-only Dijkstra pass,
        # so they and a gateway inside the zone must trigger planning on their own.
        need = bool(b.isolated_ground) or bool(b.stranded_nodes) or gateway_in_zone
        self.isolation_streak = self.isolation_streak + 1 if need else 0
        self._update_weak()
        if need and self.isolation_streak < RELAY_DISPATCH_CONFIRM_TICKS:
            return  # wait for the partition to persist before committing aircraft
        if gateway_in_zone != self.gateway_in_zone:
            self.gateway_in_zone = gateway_in_zone
            if gateway_in_zone:
                b.log_event(
                    "FLEET", "critical",
                    f"Protocol HIGH-MAST engaged: gateway radio is {b.gateway.rf_state} inside the interference zone; "
                    f"dispatching a multirotor to {MAST_ALT_M:.0f} m AGL over the gateway as airborne proxy",
                )
            else:
                b.log_event("FLEET", "ok", "Protocol HIGH-MAST stood down: gateway radio clear of interference")
        if need:
            mission_slots = self._plan_slots()
        else:
            mission_slots, self.clusters = [], []
        if b.gateway.rf_state == STATUS_JAMMED or not self.coverage_enabled:
            cover_slots, self.cover_groups = [], []
        else:
            cover_slots = self._plan_cover_slots()
        self.slots = mission_slots + cover_slots
        self._handoff_low_cover()

        holders = [
            u for u in self.uavs.values()
            if u.task in (*self.MISSION_TASKS, TASK_COVER) and u.mode == MODE_AUTO and u.failsafe is None
        ]
        prior_relay_holders = sum(1 for u in holders if u.task == TASK_RELAY)
        claimed: dict[int, UavAgent] = {}
        matched: set[str] = set()
        # Sticky-label slots: coverage areas keep their W-label, the mast slot is always MAST-GW.
        by_label = {s["label"]: si for si, s in enumerate(self.slots) if s["kind"] in (SLOT_COVER, SLOT_MAST)}
        for u in holders:
            si = by_label.get(u.slot_label) if u.task in (TASK_COVER, TASK_MAST) else None
            if si is not None and si not in claimed and SLOT_TASK[self.slots[si]["kind"]] == u.task:
                claimed[si] = u
                matched.add(u.id)
        match_m = {
            SLOT_RELAY: 3 * RELAY_WAYPOINT_DEADBAND_M,
            SLOT_COVER: COVER_MATCH_FACTOR * self.cover_plan_radius_m,
            SLOT_SHEPHERD: SHEPHERD_PLAN_RADIUS_M,
            SLOT_MAST: 3 * RELAY_WAYPOINT_DEADBAND_M,
        }
        pairs = sorted(
            (haversine_m(u.target[0], u.target[1], s["lat"], s["lng"]), u.id, si)
            for u in holders if u.target is not None
            for si, s in enumerate(self.slots) if SLOT_TASK[s["kind"]] == u.task
        )
        for d, uid, si in pairs:
            if uid in matched or si in claimed or d > match_m[self.slots[si]["kind"]]:
                continue
            claimed[si] = self.uavs[uid]
            matched.add(uid)

        newly_relay = 0
        for si, s in enumerate(self.slots):
            if si in claimed:
                continue
            pool = [u for u in self.uavs.values() if u.id not in matched and self._available(u)]
            pool += [u for u in holders if u.id not in matched]  # holders whose slot vanished
            if s["kind"] != SLOT_COVER:
                # Isolation / stranding / gateway loss outrank weak coverage: a covering UAV may be pulled.
                pool += [u for u in holders if u.task == TASK_COVER and u.id in matched]
            best = self._pick(pool, s)
            if best is None:
                continue
            if best.id in matched:
                lost = next(sj for sj, v in claimed.items() if v is best)
                del claimed[lost]
                self.event(best, "warn", f"{best.id} pulled off coverage {self.slots[lost]['label']} for {s['kind'].lower()} slot {s['label']}")
            elif best.task is not None:
                self.release(best)  # stale holder re-tasked: drop the old assignment cleanly
            claimed[si] = best
            matched.add(best.id)
            if s["kind"] == SLOT_RELAY:
                newly_relay += 1

        for si, u in claimed.items():
            s = self.slots[si]
            point = (s["lat"], s["lng"])
            task = SLOT_TASK[s["kind"]]
            first = u.task != task
            u.task = task
            u.slot_label = s["label"]
            u.unmatched_since = None
            if task == TASK_MAST and u.cruise_alt_m != MAST_ALT_M:
                u.cruise_alt_m = MAST_ALT_M
            if first or u.target is None or haversine_m(u.target[0], u.target[1], *point) > RELAY_WAYPOINT_DEADBAND_M:
                self._send_to(u, point, keep_station=not first and task == TASK_COVER)
                if not first:
                    if task in self.MISSION_TASKS:
                        self.bridge.log_event("FLEET", "info", f"{u.id} retargeted {s['label']} -> {point[0]:.5f}, {point[1]:.5f}")
                elif task == TASK_RELAY:
                    self.event(
                        u, "warn",
                        f"{u.id} ({u.utype.key}) -> {s['label']} ({point[0]:.5f}, {point[1]:.5f}, {u.cruise_alt_m:.0f} m) "
                        f"bridging cluster of {s['size']} isolated node(s)"
                        + (f"; relay chain hop {s['index']}/{s['of']}" if s["of"] > 1 else "")
                        + ("; slot pushed to the perimeter flanking arc around the drop zone" if s.get("flanked") else ""),
                    )
                elif task == TASK_MAST:
                    self.event(
                        u, "critical",
                        f"{u.id} ({u.utype.key}) -> {s['label']}: climbing to {MAST_ALT_M:.0f} m AGL over the gateway "
                        f"({point[0]:.5f}, {point[1]:.5f}) for vertical burn-through",
                    )
                elif task == TASK_SHEPHERD:
                    self.event(
                        u, "warn",
                        f"{u.id} ({u.utype.key}) -> shepherd {s['label']} over the LKGP of {s['size']} node(s) stranded on "
                        f"the old channel ({point[0]:.5f}, {point[1]:.5f}, {u.cruise_alt_m:.0f} m)",
                    )
                else:
                    self.event(
                        u, "info",
                        f"{u.id} ({u.utype.key}, {u.usable_endurance_s() / 60:.0f} min usable) -> coverage {s['label']} "
                        f"over {s['size']} node(s) with weak next hop (worst {s['worst_rssi']:.0f} dBm)",
                    )
        if newly_relay and prior_relay_holders == 0:
            b.notify_phones(f"[RELAY] {newly_relay} relay UAV(s) dispatched to bridge isolated nodes")

        self._run_reliefs()

        # Assignments no longer needed (partition healed, weak area recovered): release after a hold.
        for u in self.uavs.values():
            idle_auto = u.mode == MODE_AUTO and u.task is None and u.relieving is None and u.airborne \
                and u.flight_state not in (FS_RETURNING, FS_LANDING, FS_TAKING_OFF)
            stale_holder = u.task in (*self.MISSION_TASKS, TASK_COVER) and u.id not in matched and u.relieving is None
            if not (idle_auto or stale_holder):
                if u.task is not None:
                    u.unmatched_since = None
                continue
            u.unmatched_since = u.unmatched_since if u.unmatched_since is not None else now
            hold_s = COVER_RELEASE_HOLD_S if u.task == TASK_COVER else RELAY_RTB_HOLD_S
            if now - u.unmatched_since > hold_s:
                reason = {
                    TASK_RELAY: "relay slot no longer needed", TASK_COVER: "coverage area recovered",
                    TASK_SHEPHERD: "stranded nodes retuned; shepherd duty complete",
                    TASK_MAST: "gateway clear of interference; HIGH-MAST released",
                }.get(u.task, "idle")
                self.release(u)
                u.auto_after_land = True
                u.rtl()
                self.event(u, "info", f"{u.id} {reason}; returning to pad")

        kind_of = {si: self.slots[si]["kind"] for si in claimed}
        relay_claimed = sum(1 for k in kind_of.values() if k == SLOT_RELAY)
        self.shepherd_assigned = sum(1 for k in kind_of.values() if k == SLOT_SHEPHERD)
        self.mast_assigned = sum(1 for k in kind_of.values() if k == SLOT_MAST)
        self.assigned = relay_claimed + self.shepherd_assigned + self.mast_assigned
        self.cover_assigned = len(claimed) - self.assigned
        self.mission_slot_count = len(mission_slots)
        self.shortfall = len(mission_slots) - self.assigned
        self._report_coverage(now, len(cover_slots))
        report = (len(self.clusters), len(mission_slots), self.shortfall)
        last = self._last_report or (0, 0, 0)
        state_flip = (bool(report[1]) != bool(last[1])) or (bool(report[2]) != bool(last[2]))
        due = now - self._last_report_at >= ALLOCATOR_REPORT_MIN_INTERVAL_S
        if report != self._last_report and (state_flip or due):
            self._last_report_at = now
            if mission_slots:
                n_relay = sum(1 for s in mission_slots if s["kind"] == SLOT_RELAY)
                n_shep = sum(1 for s in mission_slots if s["kind"] == SLOT_SHEPHERD)
                n_mast = sum(1 for s in mission_slots if s["kind"] == SLOT_MAST)
                parts = []
                if n_relay:
                    parts.append(f"{len(self.clusters)} isolated cluster(s) need {n_relay} relay slot(s)")
                if n_shep:
                    parts.append(f"{len(b.stranded_nodes)} stranded node(s) need {n_shep} shepherd slot(s)")
                if n_mast:
                    parts.append("gateway needs a HIGH-MAST proxy")
                text = f"Allocator: {'; '.join(parts)}; {self.assigned} assigned"
                if self.shortfall:
                    text += f"; need {self.shortfall} more UAV(s) (add UAVs or recharge the fleet)"
                b.log_event("FLEET", "warn" if self.shortfall else "info", text)
            elif self._last_report and self._last_report[1]:
                b.log_event("FLEET", "ok", "Allocator: no isolated clusters or stranded nodes; mission slots cleared")
            self._last_report = report

    def _report_coverage(self, now: float, areas: int) -> None:
        """Rate-limited: weak links near the band edge flicker tick to tick."""
        report = (areas, self.cover_assigned)
        if report == (self._last_cover_report or (0, 0)) or now - self._last_cover_report_at < COVER_REPORT_MIN_INTERVAL_S:
            return
        self._last_cover_report_at = now
        self._last_cover_report = report
        if areas:
            backlog = areas - self.cover_assigned
            self.bridge.log_event(
                "FLEET", "info",
                f"Coverage: {len(self.weak_nodes)} node(s) with weak next hop (< {RSSI_GOOD_DBM:.0f} dBm) in {areas} area(s); "
                f"{self.cover_assigned} UAV(s) covering" + (f", {backlog} area(s) waiting for a UAV" if backlog else ""),
            )
        else:
            self.bridge.log_event("FLEET", "ok", "Coverage: every next hop at good or better; coverage slots cleared")

    def _run_reliefs(self) -> None:
        for u in list(self.uavs.values()):
            if u.task == TASK_RELAY and u.relief_by is None and u.battery_pct <= UAV_RELIEF_BATTERY_PCT and u.target:
                candidates = [c for c in self.uavs.values() if self._available(c)]
                c = self._pick(candidates, {"lat": u.target[0], "lng": u.target[1], "kind": SLOT_RELAY})
                if c is None:
                    continue
                c.task = TASK_RELIEF
                c.relieving = u.id
                c.slot_label = u.slot_label
                u.relief_by = c.id
                self._send_to(c, u.target)
                self.event(c, "warn", f"{c.id} launched to relieve {u.id} at {u.slot_label} ({u.battery_pct:.0f}% battery)")
        for c in list(self.uavs.values()):
            if c.task != TASK_RELIEF:
                continue
            u = self.uavs.get(c.relieving) if c.relieving else None
            if u is None or u.task != TASK_RELAY:
                c.task, c.relieving = TASK_RELAY, None
                continue
            if u.target and (c.target is None or haversine_m(c.target[0], c.target[1], *u.target) > RELAY_WAYPOINT_DEADBAND_M):
                self._send_to(c, u.target)
            if c.flight_state == FS_ON_STATION:
                label = u.slot_label
                u.relief_by = None
                c.relieving = None
                c.task = TASK_RELAY
                c.slot_label = label
                self.release(u)
                u.auto_after_land = True
                u.rtl()
                self.event(c, "ok", f"{c.id} took over {label}; {u.id} returning to pad to recharge")

    # -- snapshot --
    def uav_dict(self, u: UavAgent) -> dict:
        n = u.node
        t = u.utype
        return {
            "id": u.id,
            "sysid": u.sysid,
            "type": t.key,
            "type_name": t.name,
            "airframe": t.airframe,
            "speed_min_mps": t.min_speed_mps,
            "speed_max_mps": t.max_speed_mps,
            "endurance_min": t.endurance_min,
            "loiter_radius_m": t.loiter_radius_m,
            "cover_radius_m": round(a2g_cover_radius_m(max(n.alt_m, UAV_MESH_MIN_ALT_M)), 1) if u.in_mesh else None,
            "mode": u.mode,
            "flight_state": u.flight_state,
            "armed": u.armed,
            "coords": _coord((n.lat, n.lng)),
            "alt_m": round(n.alt_m, 1),
            "cruise_alt_m": round(u.cruise_alt_m, 1),
            "speed_mps": round(u.ground_speed_mps, 1),
            "cruise_speed_mps": round(u.cruise_speed_mps, 1),
            "climb_mps": round(u.climb_mps, 1),
            "bearing_deg": round(u.bearing_deg, 1),
            "eta_s": round(u.eta_s, 1) if u.eta_s is not None else None,
            "target": _coord(u.target),
            "home": _coord(u.home),
            "task": u.task,
            "slot_label": u.slot_label,
            "relieving": u.relieving,
            "relief_by": u.relief_by,
            "battery_pct": round(u.battery_pct, 1),
            "endurance_remaining_s": round(u.endurance_remaining_s()),
            "usable_endurance_s": round(u.usable_endurance_s()),
            "failsafe": u.failsafe,
            "in_mesh": u.in_mesh,
            "reachable": n.reachable,
            "status": n.status if u.in_mesh else ("STANDBY" if not u.airborne else "OFFLINE"),
            "parent_id": n.parent_id,
            "link_rssi": _round(n.rssi, 1),
            "hop_count": max(0, len(n.hop_path) - 1),
            "bridged_count": u.bridged_count,
            "dist_home_m": round(u.distance_to(u.home), 1),
            "mission_time_s": round(u.mission_time_s),
            "c2_lost_s": round(self.now() - u.unreachable_since, 1) if u.unreachable_since is not None else None,
            "waypoint_seq": u.waypoint_seq,
            "last_command": u.last_command,
            "remove_on_land": u.remove_on_land,
        }

    def to_dict(self) -> dict:
        uavs = list(self.uavs.values())
        gw_lat, gw_lng = self.bridge.gateway_pos
        holder_of = {u.slot_label: u.id for u in uavs if u.slot_label and u.task in (*self.MISSION_TASKS, TASK_COVER)}
        b = self.bridge
        return {
            "uavs": [self.uav_dict(u) for u in uavs],
            "counts": {
                "total": len(uavs),
                "airborne": sum(1 for u in uavs if u.airborne),
                "bridging": sum(1 for u in uavs if u.task in (TASK_RELAY, TASK_RELIEF) and u.flight_state == FS_ON_STATION),
                "covering": sum(1 for u in uavs if u.task == TASK_COVER and u.flight_state == FS_ON_STATION),
                "shepherding": sum(1 for u in uavs if u.task == TASK_SHEPHERD),
                "mast": sum(1 for u in uavs if u.task == TASK_MAST),
                "by_type": {k: sum(1 for u in uavs if u.utype.key == k) for k in UAV_TYPES},
                "landed": sum(1 for u in uavs if not u.airborne),
                "low_battery": sum(1 for u in uavs if u.battery_pct <= UAV_RELIEF_BATTERY_PCT),
                "failsafe": sum(1 for u in uavs if u.failsafe),
            },
            "allocator": {
                "clusters": self.clusters,
                "isolated": len(b.isolated_ground),
                "needed": self.mission_slot_count,
                "assigned": self.assigned,
                "shortfall": self.shortfall,
                "stranded_count": len(b.stranded_nodes),
                "shepherd_assigned": self.shepherd_assigned,
                "mast_assigned": self.mast_assigned,
                "mast_active": b.mast_overhead_uav() is not None,
                "gateway_in_zone": self.gateway_in_zone,
                "slots": [
                    {
                        "coords": _coord((s["lat"], s["lng"])), "label": s["label"], "cluster": s["cluster"],
                        "kind": s["kind"], "size": s["size"], "assigned_to": holder_of.get(s["label"]),
                        "alt_m": s.get("alt_m"), "flanked": bool(s.get("flanked")), "flank_from": _coord(s.get("flank_from")),
                    }
                    for s in self.slots
                ],
                "coverage": {
                    "enabled": self.coverage_enabled,
                    "weak_nodes": len(self.weak_nodes),
                    "areas": self.cover_groups,
                    "assigned": self.cover_assigned,
                    "plan_radius_m": round(self.cover_plan_radius_m, 1),
                    "threshold_dbm": RSSI_GOOD_DBM,
                },
            },
            "geofence": {"center": [gw_lat, gw_lng], "radius_m": GEOFENCE_RADIUS_M, "ceiling_m": GEOFENCE_CEILING_M},
            "config": {
                "types": [t.to_dict() for t in UAV_TYPES.values()],
                "default_type": UAV_DEFAULT_TYPE,
                "soft_limit": UAV_SOFT_LIMIT,
                "alt_min_m": UAV_ALT_MIN_M,
                "alt_max_m": GEOFENCE_CEILING_M,
                "low_battery_pct": UAV_LOW_BATTERY_PCT,
                "critical_battery_pct": UAV_CRITICAL_BATTERY_PCT,
                "relief_battery_pct": UAV_RELIEF_BATTERY_PCT,
                "lost_link_s": UAV_LOST_LINK_S,
                "relay_range_m": RELAY_LINK_MAX_M,
                "a2a_range_m": UAV_A2A_MAX_M,
                "shepherd_slant_m": SHEPHERD_BURNTHROUGH_SLANT_M,
                "mast_alt_m": MAST_ALT_M,
                "flank_margin_m": FLANK_MARGIN_M,
            },
            "gcs": self.bridge.mavlink.status_dict(),
        }
