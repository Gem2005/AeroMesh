"""
AeroMesh Bridge -- Hardware-in-the-Loop (HIL) Wireless Mesh Digital Twin.

Combines the physical painlessMesh ESP32 nodes (read from the Gateway over USB
serial) with a configurable virtual mesh swarm inside a single NetworkX
directed graph, and streams a unified 1 Hz telemetry snapshot to every
connected dashboard over WebSocket.

Subsystems:
    * SwarmSimulator     -- configurable orbit rings / polygon infill with lost-link position hold
    * RF attenuation     -- primary drop zone + fringe penalty, weighted centroid estimate
    * Adaptive Channel Switching (ACS) -- confirmation timer, CH_SWITCH over serial
    * FleetManager       -- any number of mixed-airframe relay UAVs (same radio payload):
                            relay chains for isolated clusters, coverage for weak next-hop
                            links, config-aware dispatch, relief on station, battery /
                            lost-link / geofence failsafes
    * MavlinkGcsLink     -- two-way MAVLink v2 on udp:127.0.0.1:14550: one vehicle per UAV
                            system id; QGroundControl / Mission Planner can arm, take off,
                            change mode, fly-to, RTL and land any relay UAV

All node movement and signal behaviour of the virtual nodes is a purely
mathematical simulation for educational wireless sensor network research.

Setup:
    pip install pyserial websockets networkx

Run:
    python bridge.py                       # defaults: COM9 @ 115200, ws://localhost:8765
    python bridge.py --port COM5           # custom serial port
    python bridge.py --port COM9 --baud 115200 --ws-port 8765 --swarm 40
    python bridge.py --uav-mix QUAD-S:3,VTOL-FW:2      # custom mixed relay fleet
    python bridge.py --uavs 6                          # 6 UAVs cycling through every type
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import logging
import math
import random
import re
import socket
import struct
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import networkx as nx
import serial  # pyserial
import websockets

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("bridge")

# ------------------------------------------------------------------ #
# Serial link
# ------------------------------------------------------------------ #
# Seconds to wait before retrying a failed/lost serial connection.
SERIAL_RETRY_DELAY = 3.0
# Warn after this many seconds without serial data.
SILENCE_WARN_S = 10.0
# After this many silent seconds, cycle the port (DTR pulse resets the ESP32).
SILENCE_RESET_S = 30.0
# Minimum spacing between commands written to the Gateway (its UART buffer is small).
SERIAL_WRITE_SPACING_S = 0.15
SERIAL_WRITE_QUEUE_MAX = 32
SERIAL_MESSAGE_MAX_LEN = 120

# ------------------------------------------------------------------ #
# Geospatial anchor
# ------------------------------------------------------------------ #
GATEWAY_LAT = 12.8354
GATEWAY_LON = 80.1380
GATEWAY_ID = 1693866525  # IMPORTANT: Replace with your actual Gateway Node ID
GATEWAY_KEY = str(GATEWAY_ID)
EARTH_RADIUS_M = 6371008.8

# Keep the gateway marker visually clear of the operator marker.
GATEWAY_OFFSET_M = 35
GATEWAY_OFFSET_BEARING = 90  # due east of the operator

# ------------------------------------------------------------------ #
# Hardware RSSI -> distance (indoor / obstructed ESP32 measurements)
# ------------------------------------------------------------------ #
TX_POWER = -40            # A: measured RSSI at 1 m for ESP32 (dBm)
PATH_LOSS_EXPONENT = 2.7  # n: indoor/obstructed environment

MIN_DISPLAY_SPREAD_M = 45      # keep hardware markers visually clear of the gateway
MAX_DISPLAY_DISTANCE_M = 500   # garbage RSSI must not fling nodes off the map
UNKNOWN_SIGNAL_SPREAD_M = 45   # placement radius when a node has no valid RSSI
HW_DISTANCE_SMOOTHING = 0.35   # EMA factor so markers don't jump on every RSSI sample

# ------------------------------------------------------------------ #
# Simulated link model (outdoor line-of-sight virtual swarm)
# ------------------------------------------------------------------ #
# Every node and every UAV type carries the same ESP32-class radio (20 dBm, 2 dBi):
# -16 dBm at 1 m, so link quality differs only by geometry.
RSSI_1M_DBM = -16.0
# Ground-to-ground: two-ray dual slope. Free space up to the breakpoint 4*h1*h2/lambda
# (~25 m for 0.9 m handheld antennas at 2.4 GHz), then 40 dB/decade ground reflection loss.
GROUND_BREAKPOINT_M = 25.0
GROUND_FAR_EXPONENT = 4.0
# Air-to-ground: free space plus elevation-angle excess loss (LOS probability falls at
# grazing angles): 0 dB straight down, A2G_GRAZING_LOSS_DB at the horizon.
A2G_GRAZING_LOSS_DB = 15.0
RSSI_SIGMA = 1.5
LINK_RSSI_MIN = -88.0
_GROUND_BREAKPOINT_RSSI = RSSI_1M_DBM - 20 * math.log10(GROUND_BREAKPOINT_M)
LINK_MAX_M = GROUND_BREAKPOINT_M * 10 ** ((_GROUND_BREAKPOINT_RSSI - LINK_RSSI_MIN) / (10 * GROUND_FAR_EXPONENT))  # ~315 m
# Signal bands shared with the dashboard legend / histogram.
RSSI_EXCELLENT_DBM = -55.0
RSSI_GOOD_DBM = -75.0
# Routing cost per hop = LINK_COST_HOP + exp((LINK_COST_REF - rssi) / LINK_COST_SCALE):
# one -84 dBm hop costs more than two -75 dBm hops, so parents are strong next hops,
# while the flat per-hop term stops routes from chaining through many short links.
LINK_COST_HOP = 10.0
LINK_COST_REF_DBM = -60.0
LINK_COST_SCALE_DB = 4.0
DEFAULT_CHANNEL = 6
WIFI_CHANNELS = tuple(range(1, 12))
BASE_NOISE_FLOOR_DBM = -92.0

# Packet delivery / latency model
PDR_LOGISTIC_CENTER_DBM = -90.0
PDR_LOGISTIC_SCALE_DB = 1.5
MAC_RETRIES = 1
HOP_BASE_LATENCY_MS = 4.0
HOP_RETRY_LATENCY_MS = 12.0

# ------------------------------------------------------------------ #
# Virtual swarm kinematics
# ------------------------------------------------------------------ #
SIM_TICK_S = 1.0
SWARM_MIN = 10
SWARM_MAX = 100
SWARM_DEFAULT = 40
ORBIT_RADII_DEFAULT_M = (120.0, 260.0, 420.0)
ORBIT_MAX_RINGS = 5
ORBIT_RADIUS_MIN_M = 50.0
ORBIT_RADIUS_MAX_M = 1500.0
ORBIT_MIN_RING_GAP_M = 40.0
ORBIT_TANGENTIAL_SPEED_MPS = 3.0
ORBIT_SPEED_MIN_MPS = 1.0
ORBIT_SPEED_MAX_MPS = 10.0
ORBIT_RADIAL_AMPLITUDE_M = 15.0
ORBIT_RADIAL_FREQ = 0.05
NODE_TRANSIT_SPEED_MPS = 12.0
POLYGON_GRID_SHRINK = 0.9
POLYGON_GRID_MAX_ITER = 60
VIRTUAL_BATTERY_DRAIN_MV_PER_S = 0.08
VIRTUAL_BATTERY_FLOOR_MV = 3300.0

PLACEMENT_ORBIT = "ORBIT_PATROL"
PLACEMENT_POLYGON = "POLYGON_INFILL"

# Lost-link position hold: a disconnected node freezes where it lost the link
# and only resumes its orbit after this many consecutive reachable ticks.
HOLD_RELEASE_TICKS = 3
HOLD_LOST_LINK = "LOST_LINK"
HOLD_RECONNECTING = "RECONNECTING"

# ------------------------------------------------------------------ #
# Hardware topology watchdog
# ------------------------------------------------------------------ #
STALE_OFFLINE_S = 7.0
WATCHDOG_INTERVAL_S = 1.0
OFFLINE_REMOVE_S = 10.0

# Consecutive telemetry packets with rssi=None before we treat a node that
# previously had a valid RSSI as "link lost" (force it offline immediately).
NULL_RSSI_STREAK_LIMIT = 3

LOG_HISTORY_MAX = 60

# ------------------------------------------------------------------ #
# RF attenuation zone & Adaptive Channel Switching (ACS)
# ------------------------------------------------------------------ #
ZONE_DEFAULT_KILL_M = 200.0
ZONE_DEFAULT_BLEED_M = 450.0
ZONE_MAX_RADIUS_M = 2000.0
FRINGE_MAX_PENALTY_DB = 35.0
FRINGE_RSSI_FLOOR_DBM = -92.0
JAMMED_RSSI_DBM = -95.0
JAMMED_SNR_DB = -12.0
INTERFERED_NOISE_DBM = -45.0
ACS_TRIGGER_COUNT = 2
ACS_CONFIRM_S = 3.0
ACS_SCAN_DELAY_S = 1.5
ACS_NON_OVERLAPPING = (1, 6, 11)
ACS_MIN_SEPARATION = 5  # channels closer than this overlap on 2.4 GHz (20 MHz width)
ACS_CLEAN_MARGIN_DB = 6.0  # noise within this margin of the base floor counts as clean
ACS_GATEWAY_EMERGENCY_CONFIRM_S = 2.0  # the gateway's own CCA saturates: evacuate the channel faster

RF_ALERT_MIN_INTERVAL_S = 5.0

# Interference modes. SPOT: one 20 MHz channel (+/- 1 adjacent). BARRAGE: wideband across
# CH 1..11, so no channel is clean at ground level. SWEEPER: hops CH 1 -> 6 -> 11 every 12 s.
ZONE_MODE_SPOT = "SPOT"
ZONE_MODE_BARRAGE = "BARRAGE"
ZONE_MODE_SWEEPER = "SWEEPER"
ZONE_MODES = (ZONE_MODE_SPOT, ZONE_MODE_BARRAGE, ZONE_MODE_SWEEPER)
SWEEPER_INTERVAL_S = 12.0
SWEEPER_CYCLE = (1, 6, 11)

# Causal node recovery after a channel switch. A node that could not hear the CH_SWITCH
# broadcast stays on the old channel at its Last Known Good Position (LKGP) until either a
# Shepherd UAV flies within burn-through slant range on the clean channel, or its own
# orphan watchdog sweeps the channels and finds a beacon within link range.
SHEPHERD_BURNTHROUGH_SLANT_M = 140.0   # steep elevation angle beats the local ground noise
ORPHAN_BEACON_HUNT_S = 45.0            # sim watchdog counted from stranded_since; firmware uses 14 s
ORPHAN_HUNT_JITTER_MAX_S = 3.0         # per-node random jitter so sweeps don't synchronise
MISSING_EDGE_WEIGHT_DB = 15.0          # tomography weight of a surviving-parent / silent-child midpoint
SHEPHERD_PLAN_RADIUS_M = 0.8 * math.sqrt(SHEPHERD_BURNTHROUGH_SLANT_M ** 2 - 60.0 ** 2)  # ~100 m LKGP disc

# Protocol HIGH-MAST: a multirotor climbs over the gateway so the short vertical link keeps
# the gateway alive inside a kill zone (airborne gateway proxy).
MAST_ALT_M = 95.0
MAST_HORIZONTAL_RADIUS_M = 25.0
MAST_MIN_ALT_M = 80.0
MAST_BURNTHROUGH_PENALTY_DB = 12.0
# Wideband barrage: a UAV almost directly overhead gives a ground node a high-angle LOS bonus.
BARRAGE_VERTICAL_MIN_ALT_M = 85.0
BARRAGE_VERTICAL_BONUS_DB = 22.0
BARRAGE_VERTICAL_MIN_ELEV_DEG = 60.0
# Relay slots inside an active kill radius are pushed out to a safe flanking arc.
FLANK_MARGIN_M = 40.0

# ------------------------------------------------------------------ #
# Relay UAV fleet
# ------------------------------------------------------------------ #
UAV_ID_PREFIX = "UAV-"
UAV_SOFT_LIMIT = 60                 # pairwise link search is O(n^2); warn, don't cap
UAV_MAX_SYSID = 255                 # MAVLink system ids are one byte; sysid 1 is the bridge
UAV_BASE_ALT_M = 50.0
UAV_ALT_LAYER_STEP_M = 10.0
UAV_ALT_LAYERS = 5                  # 50/60/70/80/90 m keeps neighbours vertically separated
UAV_ALT_MIN_M = 20.0
UAV_MESH_MIN_ALT_M = 10.0           # below this the relay radio is not part of the mesh
UAV_ARRIVAL_RADIUS_M = 10.0
UAV_PAD_BASE_RADIUS_M = 40.0
UAV_PAD_RING_STEP_M = 15.0
UAV_PADS_PER_RING = 12
UAV_PAD_START_BEARING = 225.0
UAV_RECHARGE_PCT_PER_S = 1.0        # accelerated pad recharge so demos don't stall
UAV_RELIEF_BATTERY_PCT = 40.0       # on-station UAV requests a relief at this level
UAV_LOW_BATTERY_PCT = 30.0          # failsafe: RTL
UAV_CRITICAL_BATTERY_PCT = 15.0     # failsafe: land in place
UAV_DISPATCH_MIN_BATTERY_PCT = 50.0
UAV_LOST_LINK_S = 8.0               # airborne without a route to the gateway -> RTL
UAV_A2A_MAX_M = 800.0               # air-to-air line-of-sight relay range
GEOFENCE_RADIUS_M = 1500.0
GEOFENCE_CEILING_M = 120.0          # common regulatory ceiling (400 ft AGL)

RELAY_RTB_HOLD_S = 10.0             # partition must stay healed this long before returning
RELAY_WAYPOINT_DEADBAND_M = 40.0    # ignore centroid drift smaller than this
RELAY_DISPATCH_CONFIRM_TICKS = 2    # isolation must persist this many ticks before dispatch
RELAY_LINK_MAX_M = 450.0
RELAY_CHAIN_SPACING_FACTOR = 0.8    # chain hops at 80% of slant range for fade margin
RELAY_MIN_STATION_S = 120.0         # dispatch only if the airframe can hold a relay slot this long
ALLOCATOR_REPORT_MIN_INTERVAL_S = 5.0

# Coverage duty: ground nodes whose own next hop (ground-only tree) is below the good
# band get a UAV overhead. Demand comes from the ground tree, so a UAV's own coverage
# never cancels the demand that placed it.
COVER_CONFIRM_TICKS = 4             # weak next hop must persist this long to count
COVER_HYSTERESIS_DB = 2.0           # a weak node clears only once its next hop is this far into the good band
COVER_PLAN_ALT_M = UAV_BASE_ALT_M
COVER_RADIUS_MARGIN = 0.8           # plan groups inside 80% of the good-RSSI footprint
COVER_MIN_GROUP = 1
COVER_MATCH_FACTOR = 1.0            # holders keep a slot that moved less than one plan radius
COVER_RELEASE_HOLD_S = 30.0
COVER_MIN_STATION_S = 300.0
COVER_ENDURANCE_WEIGHT = 0.1        # s of ETA traded per s of extra on-station time
COVER_REPORT_MIN_INTERVAL_S = 20.0
UAV_ARM_DELAY_S = 3.0

MODE_AUTO = "AUTO"
MODE_GUIDED = "GUIDED"
MODE_LOITER = "LOITER"
MODE_RTL = "RTL"
MODE_LAND = "LAND"
UAV_MODES = (MODE_AUTO, MODE_GUIDED, MODE_LOITER, MODE_RTL, MODE_LAND)
COPTER_CUSTOM_MODE = {MODE_AUTO: 3, MODE_GUIDED: 4, MODE_LOITER: 5, MODE_RTL: 6, MODE_LAND: 9}
PLANE_CUSTOM_MODE = {MODE_AUTO: 10, MODE_GUIDED: 15, MODE_LOITER: 12, MODE_RTL: 11, MODE_LAND: 20}  # LAND = QLAND

AIRFRAME_MULTIROTOR = "MULTIROTOR"
AIRFRAME_VTOL = "VTOL_FIXED_WING"


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
UAV_DEFAULT_TYPE = "QUAD-S"
UAV_DEFAULT_MIX = (("QUAD-S", 2), ("HEXA-H", 1), ("VTOL-FW", 1))


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

FS_LANDED = "LANDED"
FS_TAKING_OFF = "TAKING_OFF"
FS_TRANSIT = "TRANSIT"
FS_ON_STATION = "ON_STATION"
FS_HOLDING = "HOLDING"
FS_RETURNING = "RETURNING"
FS_LANDING = "LANDING"

TASK_RELAY = "RELAY"
TASK_RELIEF = "RELIEF"
TASK_COVER = "COVER"
TASK_GOTO = "GOTO"
TASK_SHEPHERD = "SHEPHERD"   # fly over stranded nodes' LKGP and burn the channel handoff through
TASK_MAST = "MAST"           # Protocol HIGH-MAST: hover over the gateway as an airborne proxy

SLOT_RELAY = "RELAY"
SLOT_COVER = "COVER"
SLOT_SHEPHERD = "SHEPHERD"
SLOT_MAST = "MAST"
SLOT_TASK = {SLOT_RELAY: TASK_RELAY, SLOT_COVER: TASK_COVER, SLOT_SHEPHERD: TASK_SHEPHERD, SLOT_MAST: TASK_MAST}

FAILSAFE_LOW_BATTERY = "LOW_BATTERY"
FAILSAFE_CRITICAL_BATTERY = "CRITICAL_BATTERY"
FAILSAFE_LOST_LINK = "LOST_LINK"
FAILSAFE_GEOFENCE = "GEOFENCE"

MAVLINK_HOST = "127.0.0.1"
MAVLINK_PORT = 14550
MAVLINK_COMPID = 1
MAV_TYPE_QUADROTOR = 2
MAV_AUTOPILOT_ARDUPILOTMEGA = 3
MAV_MODE_FLAG_CUSTOM_MODE_ENABLED = 1
MAV_MODE_FLAG_GUIDED_ENABLED = 8
MAV_MODE_FLAG_SAFETY_ARMED = 128
MAV_STATE_STANDBY = 3
MAV_STATE_ACTIVE = 4
MAV_TYPE_GCS = 6
MAV_FRAME_GLOBAL_RELATIVE_ALT_INT = 6
MAV_PARAM_TYPE_REAL32 = 9
MAV_SENSORS_SIMULATED = 0x0000802F      # gyro, accel, mag, baro, GPS, motor outputs
GCS_HEARTBEAT_TIMEOUT_S = 5.0
GCS_POLL_INTERVAL_S = 0.05

MAV_CMD_NAV_WAYPOINT = 16
MAV_CMD_NAV_RETURN_TO_LAUNCH = 20
MAV_CMD_NAV_LAND = 21
MAV_CMD_NAV_TAKEOFF = 22
MAV_CMD_DO_SET_MODE = 176
MAV_CMD_DO_CHANGE_SPEED = 178
MAV_CMD_DO_REPOSITION = 192
MAV_CMD_COMPONENT_ARM_DISARM = 400
MAV_CMD_SET_MESSAGE_INTERVAL = 511
MAV_CMD_REQUEST_MESSAGE = 512
MAV_CMD_REQUEST_PROTOCOL_VERSION = 519
MAV_CMD_REQUEST_AUTOPILOT_CAPABILITIES = 520
MAV_CMD_ACK_ONLY = (
    MAV_CMD_SET_MESSAGE_INTERVAL, MAV_CMD_REQUEST_MESSAGE,
    MAV_CMD_REQUEST_PROTOCOL_VERSION, MAV_CMD_REQUEST_AUTOPILOT_CAPABILITIES,
)

MAV_RESULT_ACCEPTED = 0
MAV_RESULT_DENIED = 2
MAV_RESULT_UNSUPPORTED = 3
MAV_RESULT_FAILED = 4
MAV_MISSION_ACCEPTED = 0
MAV_MISSION_ERROR = 1
MAV_MISSION_DENIED = 14

MAV_SEVERITY = {"critical": 2, "error": 3, "warn": 4, "ok": 5, "info": 6}

# outbound (msg id, CRC_EXTRA)
MAVLINK_MSG_HEARTBEAT = (0, 50)
MAVLINK_MSG_SYS_STATUS = (1, 124)
MAVLINK_MSG_PARAM_VALUE = (22, 220)
MAVLINK_MSG_GLOBAL_POSITION_INT = (33, 104)
MAVLINK_MSG_MISSION_COUNT = (44, 221)
MAVLINK_MSG_MISSION_ACK = (47, 153)
MAVLINK_MSG_MISSION_ITEM_INT = (73, 38)
MAVLINK_MSG_VFR_HUD = (74, 20)
MAVLINK_MSG_COMMAND_ACK = (77, 143)
MAVLINK_MSG_STATUSTEXT = (253, 83)

# inbound msg id -> (CRC_EXTRA, full payload length incl. extensions)
MAVLINK_RX_SPECS = {
    0: (50, 9),      # HEARTBEAT
    11: (89, 6),     # SET_MODE
    20: (214, 20),   # PARAM_REQUEST_READ
    21: (159, 2),    # PARAM_REQUEST_LIST
    43: (132, 3),    # MISSION_REQUEST_LIST
    47: (153, 4),    # MISSION_ACK
    73: (38, 38),    # MISSION_ITEM_INT
    75: (158, 35),   # COMMAND_INT
    76: (152, 33),   # COMMAND_LONG
    86: (5, 53),     # SET_POSITION_TARGET_GLOBAL_INT
}

ACS_IDLE = "IDLE"
ACS_CONFIRMING = "CONFIRMING"
ACS_SWITCHING = "SWITCHING"
ACS_SCANNING = "SCANNING"

STATUS_ONLINE = "ONLINE"
STATUS_DEGRADED = "DEGRADED"
STATUS_JAMMED = "JAMMED"
STATUS_OFFLINE = "OFFLINE"

# painlessMesh gateway debug line announcing a lost mesh connection
DROP_LINE_RE = re.compile(
    r"(?:dropped|lost)\s+connection[^0-9]*(\d{6,})", re.IGNORECASE
)
NEW_CONN_RE = re.compile(r"new\s+connection.*?(\d{6,})", re.IGNORECASE)
FIRMWARE_ACK_PREFIXES = ("[HUB]", "[ACS]", "Sent Reply", "CRITICAL", "Signal lost")


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


# ------------------------------------------------------------------ #
# Virtual swarm simulator
# ------------------------------------------------------------------ #
class SwarmSimulator:
    """Concentric-orbit or polygon-infill kinematics for N synthetic mesh nodes anchored on the gateway."""

    def __init__(self, count: int, rng: random.Random):
        self.rng = rng
        self.mode = PLACEMENT_ORBIT
        self.polygon: list[tuple[float, float]] | None = None
        self.radii: list[float] = list(ORBIT_RADII_DEFAULT_M)
        self.speed_mps = ORBIT_TANGENTIAL_SPEED_MPS
        self.ring_phase: list[float] = [0.0] * len(self.radii)
        self.t = 0.0
        self.nodes: dict[str, MeshNode] = {}
        self._pending_count = max(SWARM_MIN, min(SWARM_MAX, int(count)))

    @property
    def count(self) -> int:
        return len(self.nodes)

    def bootstrap(self, anchor: tuple[float, float], channel: int) -> None:
        self.set_size(self._pending_count, anchor, channel)
        # Start already on the orbit instead of flying out from the gateway.
        for n in self.nodes.values():
            n.lat, n.lng = self._orbit_position(n, anchor)

    def set_size(self, count: int, anchor: tuple[float, float], channel: int) -> int:
        count = max(SWARM_MIN, min(SWARM_MAX, int(count)))
        for idx in range(1, count + 1):
            nid = virtual_id(idx)
            if nid not in self.nodes:
                jitter_e, jitter_n = self.rng.uniform(-8, 8), self.rng.uniform(-8, 8)
                lat, lng = offset_m(anchor[0], anchor[1], jitter_e, jitter_n)
                self.nodes[nid] = MeshNode(
                    id=nid,
                    type="virtual",
                    lat=lat,
                    lng=lng,
                    channel=channel,
                    index=idx,
                    psi=self.rng.uniform(0, 2 * math.pi),
                    battery_mv=self.rng.uniform(3950, 4180),
                    reachable=True,  # spawns beside the gateway; don't latch a lost-link hold
                    hunt_jitter_s=self.rng.uniform(0.0, ORPHAN_HUNT_JITTER_MAX_S),
                )
        for nid in [k for k, n in self.nodes.items() if n.index > count]:
            del self.nodes[nid]
        self.nodes = dict(sorted(self.nodes.items(), key=lambda kv: kv[1].index))
        self._layout_orbit()
        if self.mode == PLACEMENT_POLYGON and self.polygon:
            self._assign_polygon_targets()
        return count

    def set_orbit(self) -> None:
        self.mode = PLACEMENT_ORBIT
        self.release_commandable_holds()
        for n in self.nodes.values():
            n.target = None

    def set_polygon(self, vertices: list[tuple[float, float]]) -> int:
        """Returns the number of target points generated (0 on invalid polygon)."""
        if len(vertices) < 3:
            return 0
        self.polygon = vertices
        self.mode = PLACEMENT_POLYGON
        self.release_commandable_holds()
        return self._assign_polygon_targets()

    @staticmethod
    def validate_orbit_config(radii, speed_mps) -> tuple[list[float], float]:
        """Raises ValueError with an operator-readable reason on an invalid config."""
        radii = [float(r) for r in radii]
        if not 1 <= len(radii) <= ORBIT_MAX_RINGS:
            raise ValueError(f"need 1-{ORBIT_MAX_RINGS} rings, got {len(radii)}")
        radii.sort()
        for r in radii:
            if not ORBIT_RADIUS_MIN_M <= r <= ORBIT_RADIUS_MAX_M:
                raise ValueError(f"ring radius {r:.0f} m outside {ORBIT_RADIUS_MIN_M:.0f}-{ORBIT_RADIUS_MAX_M:.0f} m")
        for inner, outer in zip(radii, radii[1:]):
            if outer - inner < ORBIT_MIN_RING_GAP_M:
                raise ValueError(f"rings {inner:.0f} m and {outer:.0f} m closer than {ORBIT_MIN_RING_GAP_M:.0f} m")
        speed = float(speed_mps)
        if not ORBIT_SPEED_MIN_MPS <= speed <= ORBIT_SPEED_MAX_MPS:
            raise ValueError(f"orbit speed {speed:.1f} m/s outside {ORBIT_SPEED_MIN_MPS:.0f}-{ORBIT_SPEED_MAX_MPS:.0f} m/s")
        return radii, speed

    def set_orbit_config(self, radii, speed_mps) -> None:
        self.radii, self.speed_mps = self.validate_orbit_config(radii, speed_mps)
        self.ring_phase = [0.0] * len(self.radii)
        self._layout_orbit()
        if self.mode == PLACEMENT_ORBIT:
            self.release_commandable_holds()

    def max_ring_gap_m(self) -> float:
        """Largest radial gap a packet must cross, including gateway -> innermost ring."""
        edges = [0.0, *self.radii]
        return max(b - a for a, b in zip(edges, edges[1:]))

    def ring_omega(self, ring: int) -> float:
        direction = 1.0 if ring % 2 == 0 else -1.0
        return direction * self.speed_mps / self.radii[ring]

    def _layout_orbit(self) -> None:
        n = len(self.nodes)
        total_r = sum(self.radii)
        raw = [n * r / total_r for r in self.radii]
        counts = [int(math.floor(x)) for x in raw]
        remainders = sorted(range(len(raw)), key=lambda k: raw[k] - counts[k], reverse=True)
        for k in remainders[: n - sum(counts)]:
            counts[k] += 1
        ordered = list(self.nodes.values())
        pos = 0
        for ring, ring_count in enumerate(counts):
            for j in range(ring_count):
                node = ordered[pos]
                node.ring = ring
                node.theta0 = 2 * math.pi * j / ring_count + ring * 0.35
                pos += 1

    def _orbit_position(self, node: MeshNode, anchor: tuple[float, float]) -> tuple[float, float]:
        r = self.radii[node.ring] + ORBIT_RADIAL_AMPLITUDE_M * math.sin(ORBIT_RADIAL_FREQ * self.t + node.psi)
        theta = node.theta0 + self.ring_phase[node.ring]
        return offset_m(anchor[0], anchor[1], r * math.sin(theta), r * math.cos(theta))

    def release_commandable_holds(self) -> None:
        """A new placement order reaches every node that still has a route (direct or via a relay UAV)."""
        for n in self.nodes.values():
            if n.hold_reason == HOLD_RECONNECTING:
                n.hold_pos = n.hold_reason = n.lost_link_since = None
                n.reconnect_streak = 0

    def holding_count(self) -> int:
        return sum(1 for n in self.nodes.values() if n.hold_pos is not None)

    def _update_hold(self, node: MeshNode) -> bool:
        """Lost-link hold state machine. Returns True while the node must stay put."""
        lost = (not node.reachable) or node.status in (STATUS_OFFLINE, STATUS_JAMMED)
        if lost:
            node.reconnect_streak = 0
            if node.hold_pos is None:
                node.hold_pos = (node.lat, node.lng)
                node.lost_link_since = self.t
            node.hold_reason = HOLD_LOST_LINK
            return True
        if node.hold_pos is None:
            return False
        # Any route counts, including one through a relay UAV: the node has C2 again and
        # resumes its placement order; the allocator re-positions relays if it drifts out.
        node.reconnect_streak += 1
        if node.reconnect_streak < HOLD_RELEASE_TICKS:
            node.hold_reason = HOLD_RECONNECTING
            return True
        node.hold_pos = None
        node.hold_reason = None
        node.lost_link_since = None
        node.reconnect_streak = 0
        return False

    def _polygon_points(self, n: int) -> list[tuple[float, float]]:
        verts = self.polygon or []
        lat0, lng0 = verts[0]
        local = [to_local_m(lat0, lng0, la, ln) for la, ln in verts]
        area = polygon_area(local)
        if area < 1.0:
            return []
        xs = [p[0] for p in local]
        ys = [p[1] for p in local]
        min_x, max_x, min_y, max_y = min(xs), max(xs), min(ys), max(ys)
        spacing = math.sqrt(area / n)
        inside: list[tuple[float, float]] = []
        for _ in range(POLYGON_GRID_MAX_ITER):
            inside = []
            y = min_y + spacing / 2
            while y < max_y:
                x = min_x + spacing / 2
                while x < max_x:
                    if point_in_polygon(x, y, local):
                        inside.append((x, y))
                    x += spacing
                y += spacing
            if len(inside) >= n:
                break
            spacing *= POLYGON_GRID_SHRINK
        if not inside:
            return []
        picked = [inside[int(i * len(inside) / n)] for i in range(n)] if len(inside) >= n else inside
        return [offset_m(lat0, lng0, x, y) for x, y in picked]

    def _assign_polygon_targets(self) -> int:
        nodes = list(self.nodes.values())
        points = self._polygon_points(len(nodes))
        if not points:
            return 0
        pairs = []
        for ni, node in enumerate(nodes):
            for pi, (plat, plng) in enumerate(points):
                pairs.append((haversine_m(node.lat, node.lng, plat, plng), ni, pi))
        pairs.sort()
        node_done: set[int] = set()
        point_done: set[int] = set()
        for _, ni, pi in pairs:
            if ni in node_done or pi in point_done:
                continue
            nodes[ni].target = points[pi]
            node_done.add(ni)
            point_done.add(pi)
        # More nodes than points (degenerate polygon): stack leftovers on points round-robin.
        for ni, node in enumerate(nodes):
            if ni not in node_done:
                node.target = points[ni % len(points)]
        return len(points)

    def step(self, dt: float, anchor: tuple[float, float]) -> None:
        """Advances one tick using the link state from the previous topology pass."""
        self.t += dt
        for ring in range(len(self.ring_phase)):
            self.ring_phase[ring] += self.ring_omega(ring) * dt
        step_m = NODE_TRANSIT_SPEED_MPS * dt
        for node in self.nodes.values():
            if self._update_hold(node):
                node.lat, node.lng = node.hold_pos
            else:
                if self.mode == PLACEMENT_POLYGON and node.target is not None:
                    tlat, tlng = node.target
                else:
                    tlat, tlng = self._orbit_position(node, anchor)
                node.lat, node.lng, _ = move_toward(node.lat, node.lng, tlat, tlng, step_m)
            if node.battery_mv is not None:
                node.battery_mv = max(VIRTUAL_BATTERY_FLOOR_MV, node.battery_mv - VIRTUAL_BATTERY_DRAIN_MV_PER_S * dt)


# ------------------------------------------------------------------ #
# MAVLink v2 ground-station link (UDP, bidirectional)
# ------------------------------------------------------------------ #
def x25_crc(data: bytes, crc: int = 0xFFFF) -> int:
    """CRC-16/MCRF4XX (X.25) as used by MAVLink."""
    for byte in data:
        tmp = byte ^ (crc & 0xFF)
        tmp = (tmp ^ (tmp << 4)) & 0xFF
        crc = ((crc >> 8) ^ (tmp << 8) ^ (tmp << 3) ^ (tmp >> 4)) & 0xFFFF
    return crc


def parse_mavlink_frames(data: bytes) -> tuple[list[tuple[int, int, int, bytes]], int, int]:
    """
    Splits a datagram into MAVLink v1/v2 frames. Returns
    ([(msgid, sysid, compid, payload zero-padded to full length)], crc_errors, ignored).
    Frames whose msg id is not in MAVLINK_RX_SPECS cannot be CRC-checked and are ignored.
    """
    frames: list[tuple[int, int, int, bytes]] = []
    crc_errors = ignored = 0
    i, n = 0, len(data)
    while i < n:
        stx = data[i]
        if stx == 0xFD:
            if i + 10 > n:
                break
            ln, incompat = data[i + 1], data[i + 2]
            end = i + 10 + ln + 2 + (13 if incompat & 0x01 else 0)
            if end > n:
                break
            sysid, compid = data[i + 5], data[i + 6]
            msgid = int.from_bytes(data[i + 7:i + 10], "little")
            payload = data[i + 10:i + 10 + ln]
            crc_input = data[i + 1:i + 10 + ln]
            crc = struct.unpack_from("<H", data, i + 10 + ln)[0]
        elif stx == 0xFE:
            if i + 6 > n:
                break
            ln = data[i + 1]
            end = i + 6 + ln + 2
            if end > n:
                break
            sysid, compid, msgid = data[i + 3], data[i + 4], data[i + 5]
            payload = data[i + 6:i + 6 + ln]
            crc_input = data[i + 1:i + 6 + ln]
            crc = struct.unpack_from("<H", data, i + 6 + ln)[0]
        else:
            i += 1
            continue
        spec = MAVLINK_RX_SPECS.get(msgid)
        if spec is None:
            ignored += 1
        elif x25_crc(bytes([spec[0]]), x25_crc(crc_input)) != crc:
            crc_errors += 1
        else:
            frames.append((msgid, sysid, compid, payload.ljust(spec[1], b"\x00")))
        i = end
    return frames, crc_errors, ignored


class MavlinkGcsLink:
    """
    Two-way MAVLink v2 link between the relay fleet and a ground station
    (QGroundControl / Mission Planner on udp:127.0.0.1:14550). Every UAV is a
    separate vehicle (own system id and sequence counter). Inbound
    COMMAND_LONG/INT, SET_MODE, guided MISSION_ITEM_INT and
    SET_POSITION_TARGET_GLOBAL_INT are routed through FleetManager.command,
    so GCS and dashboard commands share the same validation and logging.

    Uses one non-blocking UDP socket bound to an ephemeral port; the GCS
    replies to that port. poll() drains it without blocking the event loop.
    """

    def __init__(self, host: str = MAVLINK_HOST, port: int = MAVLINK_PORT):
        self.addr = (host, port)
        self.seq: dict[int, int] = {}
        self.packet_count = 0
        self.rx_count = 0
        self.cmd_count = 0
        self.crc_errors = 0
        self.ignored = 0
        self.gcs_addr: tuple[str, int] | None = None
        self.gcs_sysid: int | None = None
        self.gcs_heartbeat_at: float | None = None
        self.last_command: str | None = None
        self.last_command_at: float | None = None
        self.boot = time.monotonic()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setblocking(False)
        if hasattr(socket, "SIO_UDP_CONNRESET"):
            # Windows: an ICMP port-unreachable (no GCS listening) would otherwise
            # surface as ConnectionResetError on every recvfrom.
            self.sock.ioctl(socket.SIO_UDP_CONNRESET, False)
        self.sock.bind(("0.0.0.0", 0))
        self.local_port = self.sock.getsockname()[1]
        self.backend = "stdlib"
        log.info(
            "MAVLink v2 GCS link -> udp:%s:%d (replies on local port %d)", host, port, self.local_port
        )

    @property
    def gcs_connected(self) -> bool:
        return self.gcs_heartbeat_at is not None and time.monotonic() - self.gcs_heartbeat_at <= GCS_HEARTBEAT_TIMEOUT_S

    def status_dict(self) -> dict:
        now = time.monotonic()
        return {
            "host": self.addr[0],
            "port": self.addr[1],
            "local_port": self.local_port,
            "backend": self.backend,
            "packet_count": self.packet_count,
            "rx_count": self.rx_count,
            "cmd_count": self.cmd_count,
            "rx_errors": self.crc_errors,
            "connected": self.gcs_connected,
            "gcs_addr": f"{self.gcs_addr[0]}:{self.gcs_addr[1]}" if self.gcs_addr else None,
            "gcs_sysid": self.gcs_sysid,
            "heartbeat_age_s": _round(now - self.gcs_heartbeat_at, 1) if self.gcs_heartbeat_at else None,
            "last_command": self.last_command,
            "last_command_age_s": _round(now - self.last_command_at, 1) if self.last_command_at else None,
        }

    def frame(self, msg: tuple[int, int], payload: bytes, sysid: int, compid: int = MAVLINK_COMPID) -> bytes:
        msgid, crc_extra = msg
        body = payload.rstrip(b"\x00") or b"\x00"  # MAVLink v2 trailing-zero truncation
        seq = self.seq.get(sysid, 0)
        header = struct.pack("<BBBBBBB", 0xFD, len(body), 0, 0, seq, sysid, compid) + msgid.to_bytes(3, "little")
        crc = x25_crc(header[1:] + body)
        crc = x25_crc(bytes([crc_extra]), crc)
        self.seq[sysid] = (seq + 1) & 0xFF
        return header + body + struct.pack("<H", crc)

    def _send_raw(self, msg: tuple[int, int], payload: bytes, sysid: int) -> None:
        packet = self.frame(msg, payload, sysid)
        targets = {self.addr}
        if self.gcs_addr is not None and self.gcs_connected:
            targets.add(self.gcs_addr)  # a GCS on another host that sent us heartbeats
        for addr in targets:
            try:
                self.sock.sendto(packet, addr)
                self.packet_count += 1
            except OSError as exc:
                log.debug("MAVLink UDP send to %s failed: %s", addr, exc)

    def time_boot_ms(self) -> int:
        return int((time.monotonic() - self.boot) * 1000) & 0xFFFFFFFF

    def send_heartbeat(self, u: "UavAgent") -> None:
        base_mode = MAV_MODE_FLAG_CUSTOM_MODE_ENABLED
        if u.mode in (MODE_GUIDED, MODE_AUTO):
            base_mode |= MAV_MODE_FLAG_GUIDED_ENABLED
        if u.armed:
            base_mode |= MAV_MODE_FLAG_SAFETY_ARMED
        state = MAV_STATE_ACTIVE if u.airborne else MAV_STATE_STANDBY
        payload = struct.pack(
            "<IBBBBB", u.utype.custom_modes[u.mode], u.utype.mav_type, MAV_AUTOPILOT_ARDUPILOTMEGA, base_mode, state, 3
        )
        self._send_raw(MAVLINK_MSG_HEARTBEAT, payload, u.sysid)

    def send_global_position(self, u: "UavAgent") -> None:
        b = math.radians(u.bearing_deg)
        vx = int(max(-32768, min(32767, round(u.ground_speed_mps * math.cos(b) * 100))))
        vy = int(max(-32768, min(32767, round(u.ground_speed_mps * math.sin(b) * 100))))
        vz = int(max(-32768, min(32767, round(-u.climb_mps * 100))))
        hdg = int(round(u.bearing_deg * 100)) % 36000
        lat_e7, lng_e7 = int(round(u.node.lat * 1e7)), int(round(u.node.lng * 1e7))
        alt_mm = int(round(u.node.alt_m * 1000))
        payload = struct.pack("<IiiiihhhH", self.time_boot_ms(), lat_e7, lng_e7, alt_mm, alt_mm, vx, vy, vz, hdg)
        self._send_raw(MAVLINK_MSG_GLOBAL_POSITION_INT, payload, u.sysid)

    def send_waypoint(self, u: "UavAgent", lat: float, lng: float, alt_m: float) -> None:
        """MISSION_ITEM_INT carrying MAV_CMD_NAV_WAYPOINT (current=2: guided 'fly-to')."""
        lat_e7, lng_e7 = int(round(lat * 1e7)), int(round(lng * 1e7))
        hold_s, accept_m, pass_m, yaw = 0.0, UAV_ARRIVAL_RADIUS_M, 0.0, float("nan")
        payload = struct.pack(
            "<ffffiifHHBBBBBB",
            hold_s, accept_m, pass_m, yaw, lat_e7, lng_e7, alt_m,
            u.waypoint_seq & 0xFFFF, MAV_CMD_NAV_WAYPOINT, u.sysid, 1, MAV_FRAME_GLOBAL_RELATIVE_ALT_INT, 2, 1, 0,
        )
        self._send_raw(MAVLINK_MSG_MISSION_ITEM_INT, payload, u.sysid)

    def send_sys_status(self, u: "UavAgent") -> None:
        voltage_mv = int(round((14.0 + 2.8 * u.battery_pct / 100.0) * 1000))  # 4S LiPo
        current_ca = 1500 if u.airborne else 0
        payload = struct.pack(
            "<IIIHHhHHHHHHb",
            MAV_SENSORS_SIMULATED, MAV_SENSORS_SIMULATED, MAV_SENSORS_SIMULATED,
            300 if u.airborne else 100, voltage_mv, current_ca, 0, 0, 0, 0, 0, 0,
            int(round(max(0.0, min(100.0, u.battery_pct)))),
        )
        self._send_raw(MAVLINK_MSG_SYS_STATUS, payload, u.sysid)

    def send_vfr_hud(self, u: "UavAgent") -> None:
        throttle = 50 if u.airborne else 0
        payload = struct.pack(
            "<ffffhH", u.ground_speed_mps, u.ground_speed_mps, u.node.alt_m, u.climb_mps,
            int(round(u.bearing_deg)) % 360, throttle,
        )
        self._send_raw(MAVLINK_MSG_VFR_HUD, payload, u.sysid)

    def send_statustext(self, u: "UavAgent", severity: str, text: str) -> None:
        if u.sysid is None:
            return
        body = f"{u.id}: {text}".encode("ascii", "replace")[:50]
        payload = struct.pack("<B50s", MAV_SEVERITY.get(severity, 6), body)
        self._send_raw(MAVLINK_MSG_STATUSTEXT, payload, u.sysid)

    def send_command_ack(self, sysid: int, command: int, result: int, target_sys: int, target_comp: int) -> None:
        payload = struct.pack("<HBBiBB", command, result, 0, 0, target_sys, target_comp)
        self._send_raw(MAVLINK_MSG_COMMAND_ACK, payload, sysid)

    def send_mission_ack(self, sysid: int, result: int, target_sys: int, target_comp: int) -> None:
        self._send_raw(MAVLINK_MSG_MISSION_ACK, struct.pack("<BBBB", target_sys, target_comp, result, 0), sysid)

    def send_mission_count(self, sysid: int, target_sys: int, target_comp: int) -> None:
        """Relay UAVs fly allocator-generated guided targets, not uploaded missions."""
        self._send_raw(MAVLINK_MSG_MISSION_COUNT, struct.pack("<HBBB", 0, target_sys, target_comp, 0), sysid)

    def send_param_value(self, u: "UavAgent") -> None:
        payload = struct.pack("<fHH16sB", float(u.sysid), 1, 0, b"SYSID_THISMAV", MAV_PARAM_TYPE_REAL32)
        self._send_raw(MAVLINK_MSG_PARAM_VALUE, payload, u.sysid)

    # ---------------- inbound ---------------- #
    def poll(self, fleet: "FleetManager") -> int:
        """Drains the socket and executes GCS messages. Returns the number of state-changing commands."""
        executed = 0
        while True:
            try:
                data, addr = self.sock.recvfrom(4096)
            except (BlockingIOError, InterruptedError):
                break
            except ConnectionResetError:
                continue
            except OSError as exc:
                log.debug("MAVLink UDP receive failed: %s", exc)
                break
            frames, crc_errors, ignored = parse_mavlink_frames(data)
            self.crc_errors += crc_errors
            self.ignored += ignored
            for msgid, sysid, compid, payload in frames:
                if fleet.is_own_sysid(sysid) and compid == MAVLINK_COMPID:
                    continue  # our own telemetry looped back
                self.rx_count += 1
                try:
                    executed += self._handle(fleet, msgid, sysid, compid, payload, addr)
                except (struct.error, ValueError) as exc:
                    self.crc_errors += 1
                    log.debug("MAVLink message %d from %s rejected: %s", msgid, addr, exc)
        return executed

    def _targets(self, fleet: "FleetManager", target_sys: int) -> list["UavAgent"]:
        if target_sys == 0:
            return [u for u in fleet.uavs.values() if u.sysid is not None]
        u = fleet.by_sysid(target_sys)
        return [u] if u is not None else []

    def _record(self, text: str) -> None:
        self.cmd_count += 1
        self.last_command = text
        self.last_command_at = time.monotonic()

    def _handle(self, fleet: "FleetManager", msgid: int, src_sys: int, src_comp: int, p: bytes, addr) -> int:
        if msgid == 0:  # HEARTBEAT
            mav_type = p[4]
            if mav_type == MAV_TYPE_GCS:
                if not self.gcs_connected:
                    fleet.bridge.log_event(
                        "FLEET", "ok", f"Ground station connected over MAVLink (sysid {src_sys}, {addr[0]}:{addr[1]})"
                    )
                self.gcs_heartbeat_at = time.monotonic()
                self.gcs_sysid = src_sys
                self.gcs_addr = addr
            return 0

        if msgid == 76:  # COMMAND_LONG
            *params, command, target_sys, target_comp, _confirm = struct.unpack("<7fHBBB", p)
            return self._run_command(fleet, command, params, None, target_sys, src_sys, src_comp)

        if msgid == 75:  # COMMAND_INT
            p1, p2, p3, p4, x, y, z, command, target_sys, target_comp, _frame, _cur, _auto = struct.unpack(
                "<4fiifHBBBBB", p
            )
            return self._run_command(fleet, command, [p1, p2, p3, p4, 0.0, 0.0, z], (x / 1e7, y / 1e7), target_sys, src_sys, src_comp)

        if msgid == 11:  # SET_MODE
            custom_mode, target_sys, _base = struct.unpack("<IBB", p)
            done = 0
            for u in self._targets(fleet, target_sys):
                result = self._set_mode(fleet, u, custom_mode)
                self.send_command_ack(u.sysid, 11, result, src_sys, src_comp)
                done += result == MAV_RESULT_ACCEPTED
            return done

        if msgid == 73:  # MISSION_ITEM_INT (current=2 -> guided fly-to)
            (_p1, _p2, _p3, _p4, x, y, z, _seq, command, target_sys, target_comp, _frame, current, _auto, _mtype) = (
                struct.unpack("<ffffiifHHBBBBBB", p)
            )
            done = 0
            for u in self._targets(fleet, target_sys):
                if current == 2 and command == MAV_CMD_NAV_WAYPOINT:
                    ok = self._goto(fleet, u, x / 1e7, y / 1e7, z, "MISSION_ITEM_INT")
                    self.send_mission_ack(u.sysid, MAV_MISSION_ACCEPTED if ok else MAV_MISSION_ERROR, src_sys, src_comp)
                    done += ok
                else:
                    self.send_mission_ack(u.sysid, MAV_MISSION_DENIED, src_sys, src_comp)
            return done

        if msgid == 86:  # SET_POSITION_TARGET_GLOBAL_INT
            _tbm, lat, lon, alt, *_rest, type_mask, target_sys, target_comp, _frame = struct.unpack("<Iii9fHBBB", p)
            if type_mask & 0x0007:
                return 0  # position ignored by the sender (velocity/accel-only setpoint)
            done = 0
            for u in self._targets(fleet, target_sys):
                done += self._goto(fleet, u, lat / 1e7, lon / 1e7, alt, "SET_POSITION_TARGET")
            return done

        if msgid in (21, 20):  # PARAM_REQUEST_LIST / PARAM_REQUEST_READ
            target_sys = p[0] if msgid == 21 else p[2]
            for u in self._targets(fleet, target_sys):
                self.send_param_value(u)
            return 0

        if msgid == 43:  # MISSION_REQUEST_LIST
            for u in self._targets(fleet, p[0]):
                self.send_mission_count(u.sysid, src_sys, src_comp)
            return 0
        return 0  # MISSION_ACK and other handshake traffic

    def _goto(self, fleet: "FleetManager", u: "UavAgent", lat: float, lng: float, alt: float, via: str) -> bool:
        args = {"lat": lat, "lng": lng}
        if math.isfinite(alt) and alt > 0:
            args["alt_m"] = alt
        ok, _ = fleet.command(u.id, "GOTO", source="GCS", **args)
        self._record(f"{u.id} GOTO ({via}) {'accepted' if ok else 'rejected'}")
        return ok

    def _set_mode(self, fleet: "FleetManager", u: "UavAgent", custom_mode: int) -> int:
        mode = next((m for m, v in u.utype.custom_modes.items() if v == int(custom_mode)), None)
        if mode is None:
            self._record(f"{u.id} SET_MODE {custom_mode} unsupported")
            return MAV_RESULT_UNSUPPORTED
        action = {MODE_LOITER: "HOLD"}.get(mode, mode)
        ok, _ = fleet.command(u.id, action, source="GCS")
        self._record(f"{u.id} MODE {mode} {'accepted' if ok else 'rejected'}")
        return MAV_RESULT_ACCEPTED if ok else MAV_RESULT_DENIED

    def _run_command(
        self, fleet: "FleetManager", command: int, params: list[float], latlng: tuple[float, float] | None,
        target_sys: int, src_sys: int, src_comp: int,
    ) -> int:
        done = 0
        for u in self._targets(fleet, target_sys):
            result = self._execute(fleet, u, command, params, latlng)
            self.send_command_ack(u.sysid, command, result, src_sys, src_comp)
            done += result == MAV_RESULT_ACCEPTED and command not in MAV_CMD_ACK_ONLY
        return done

    def _execute(
        self, fleet: "FleetManager", u: "UavAgent", command: int, params: list[float], latlng: tuple[float, float] | None
    ) -> int:
        if command in MAV_CMD_ACK_ONLY:
            return MAV_RESULT_ACCEPTED  # streams are fixed-rate; capability probes need no payload

        def finite(v: float) -> bool:
            return math.isfinite(v) and v > 0

        if command == MAV_CMD_DO_SET_MODE:
            return self._set_mode(fleet, u, int(params[1]))
        if command == MAV_CMD_DO_REPOSITION:
            lat, lng = latlng if latlng is not None else (params[4], params[5])
            if not (math.isfinite(lat) and math.isfinite(lng)):
                return MAV_RESULT_FAILED
            args = {"lat": lat, "lng": lng}
            if finite(params[6]):
                args["alt_m"] = params[6]
            if finite(params[0]):
                args["speed_mps"] = params[0]
            action = "GOTO"
        elif command == MAV_CMD_COMPONENT_ARM_DISARM:
            action, args = ("ARM" if params[0] >= 0.5 else "DISARM"), {}
        elif command == MAV_CMD_NAV_TAKEOFF:
            action, args = "TAKEOFF", ({"alt_m": params[6]} if finite(params[6]) else {})
        elif command == MAV_CMD_NAV_LAND:
            action, args = "LAND", {}
        elif command == MAV_CMD_NAV_RETURN_TO_LAUNCH:
            action, args = "RTL", {}
        elif command == MAV_CMD_DO_CHANGE_SPEED:
            if not finite(params[1]):
                return MAV_RESULT_FAILED
            action, args = "SET_SPEED", {"speed_mps": params[1]}
        else:
            self._record(f"{u.id} MAV_CMD {command} unsupported")
            return MAV_RESULT_UNSUPPORTED
        ok, _ = fleet.command(u.id, action, source="GCS", **args)
        self._record(f"{u.id} {action} {'accepted' if ok else 'rejected'}")
        return MAV_RESULT_ACCEPTED if ok else MAV_RESULT_DENIED


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


# ------------------------------------------------------------------ #
# Bridge / Digital Twin engine
# ------------------------------------------------------------------ #
class Bridge:
    def __init__(
        self,
        serial_port: str,
        baud: int,
        ws_host: str,
        ws_port: int,
        swarm_size: int = SWARM_DEFAULT,
        uav_count: int | None = None,
        uav_types: list[str] | None = None,
        mavlink_port: int = MAVLINK_PORT,
    ):
        self.serial_port = serial_port
        self.baud = baud
        self.ws_host = ws_host
        self.ws_port = ws_port
        self.clients = set()
        self.active_ser = None
        self.serial_queue: asyncio.Queue[str] | None = None
        self.rng = random.Random()
        self.started_at = time.monotonic()
        self.tick_count = 0
        self._bg_tasks: set[asyncio.Task] = set()

        self.gateway_pos = (GATEWAY_LAT, GATEWAY_LON)
        self.operator_gps = None
        self.active_channel = DEFAULT_CHANNEL

        self.gateway = MeshNode(
            id=GATEWAY_KEY,
            type="hardware",
            lat=GATEWAY_LAT,
            lng=GATEWAY_LON,
            hw_id=GATEWAY_ID,
            is_gateway=True,
            battery_mv=5000.0,
            reachable=True,
        )
        self.hw_nodes: dict[int, MeshNode] = {}
        self.dropped_ids: set[int] = set()

        self.swarm = SwarmSimulator(swarm_size, self.rng)
        self.swarm.bootstrap(self.gateway_pos, self.active_channel)

        self.mesh_graph = nx.DiGraph()
        self.paths: dict[str, list[str]] = {GATEWAY_KEY: [GATEWAY_KEY]}
        self.ground_paths: dict[str, list[str]] = {GATEWAY_KEY: [GATEWAY_KEY]}
        self.spectrum_noise: dict[str, float] = {}
        self.metrics = {
            "health_score": 0.0,
            "pdr_percent": 0.0,
            "avg_latency_ms": 0.0,
            "graph_diameter": 0,
            "reachable_count": 0,
        }
        self.log_history: list[dict] = []

        # RF attenuation zone + ACS state
        self.zone: InterferenceZone | None = None
        self.interference_reports: list[dict] = []
        self.estimated_epicenter: tuple[float, float] | None = None
        self.acs_auto = True
        self.acs_state = ACS_IDLE
        self.acs_confirm_started: float | None = None
        self.acs_confirm_token = 0
        self.acs_target_channel: int | None = None
        self.self_heal_convergence_ms: float | None = None
        self.acs_confirm_duration_s = ACS_CONFIRM_S
        self._switch_t0: float | None = None       # perf counter of the last channel switch
        self._switch_sim_t = 0.0
        self._switch_old_channel: int | None = None
        self._barrage_acs_notice = False
        self.stranded_nodes: set[str] = set()
        self._last_rf_counts = (0, 0)
        self._last_rf_alert_at = 0.0

        # Relay UAV fleet + MAVLink
        self.isolated_ground: set[str] = set()
        self.ground_link_rssi: dict[str, float] = {}
        self.mavlink = MavlinkGcsLink(MAVLINK_HOST, mavlink_port)
        if uav_types is None:
            if uav_count is None:
                uav_types = [key for key, k in UAV_DEFAULT_MIX for _ in range(k)]
            else:
                order = list(UAV_TYPES)
                uav_types = [order[i % len(order)] for i in range(max(0, uav_count))]
        self.fleet = FleetManager(self, uav_types)

        self.recompute_topology()

    # ------------------------------------------------------------------ #
    # Node collections
    # ------------------------------------------------------------------ #
    def hardware_edges(self) -> list[MeshNode]:
        return list(self.hw_nodes.values())

    def all_nodes(self) -> list[MeshNode]:
        return [self.gateway, *self.hw_nodes.values(), *self.swarm.nodes.values()]

    def find_node(self, node_id: str) -> MeshNode | None:
        for n in self.all_nodes():
            if n.id == node_id:
                return n
        return None

    # ------------------------------------------------------------------ #
    # Background task / event helpers
    # ------------------------------------------------------------------ #
    def _spawn(self, coro) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            coro.close()  # no running loop (startup / offline use)
            return
        task = loop.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    def log_event(self, tag: str, severity: str, text: str) -> None:
        """Streams a LOG_EVENT frame to the dashboard and mirrors it to the console."""
        event = {"type": "LOG_EVENT", "tag": tag, "severity": severity, "text": text, "ts": utc_iso()}
        self.log_history.append(event)
        if len(self.log_history) > LOG_HISTORY_MAX:
            del self.log_history[: len(self.log_history) - LOG_HISTORY_MAX]
        level = {"warn": logging.WARNING, "critical": logging.ERROR}.get(severity, logging.INFO)
        log.log(level, "[%s] %s", tag, text)
        self._spawn(self.broadcast(json.dumps(event)))

    def emit_packet_animation(self, direction: str, path: list[str], message: str = "") -> None:
        if len(path) < 2:
            return
        frame = json.dumps({"type": "PACKET_ANIMATION", "direction": direction, "path": path, "message": message})
        self._spawn(self.broadcast(frame))

    # ------------------------------------------------------------------ #
    # Serial output (to Gateway ESP32)
    # ------------------------------------------------------------------ #
    @staticmethod
    def _sanitize_serial_text(text: str) -> str:
        clean = " ".join(str(text).replace("\r", " ").replace("\n", " ").split())
        return clean[:SERIAL_MESSAGE_MAX_LEN]

    def serial_send(self, line: str) -> bool:
        """Queues one command line for the Gateway. Returns False if it was dropped."""
        if self.serial_queue is None:
            return False
        if self.serial_queue.qsize() >= SERIAL_WRITE_QUEUE_MAX:
            log.warning("Serial write queue full; dropping: %s", line)
            return False
        self.serial_queue.put_nowait(line.rstrip("\n") + "\n")
        return True

    def notify_phones(self, text: str) -> None:
        """Broadcasts a hub message to every phone on http://aeromesh.local."""
        msg = self._sanitize_serial_text(text)
        if msg:
            self.serial_send(f"TARGET:ALL|{msg}")

    def serial_connected(self) -> bool:
        return bool(self.active_ser is not None and self.active_ser.is_open)

    async def serial_writer(self):
        loop = asyncio.get_running_loop()
        assert self.serial_queue is not None
        while True:
            line = await self.serial_queue.get()
            ser = self.active_ser
            if ser is None or not ser.is_open:
                log.info("Gateway serial offline; not forwarded: %s", line.strip())
                continue
            try:
                await loop.run_in_executor(None, ser.write, line.encode("utf-8"))
                log.info("[SERIAL TX] %s", line.strip())
            except (serial.SerialException, OSError) as exc:
                log.error("Serial write failed (%s): %s", exc, line.strip())
            await asyncio.sleep(SERIAL_WRITE_SPACING_S)

    # ------------------------------------------------------------------ #
    # Simulation tick
    # ------------------------------------------------------------------ #
    def link_rssi(self, a: MeshNode, b: MeshNode, d: float) -> float | None:
        """Nominal (pre-attenuation) RSSI for a candidate link, or None if out of range."""
        hw_pair = {a.id, b.id}
        if GATEWAY_KEY in hw_pair:
            other = b if a.is_gateway else a
            if other.type == "hardware" and other.reported_rssi is not None:
                return other.reported_rssi  # real measured hardware link
        if d > LINK_MAX_M:
            return None
        return ground_rssi_nominal(d) + self.rng.gauss(0, RSSI_SIGMA)

    def advance_sweeper(self) -> None:
        """SWEEPER mode: the interference hops CH 1 -> 6 -> 11 every SWEEPER_INTERVAL_S."""
        zone = self.zone
        if zone is None or zone.mode != ZONE_MODE_SWEEPER:
            return
        now = time.monotonic()
        if now - zone.last_sweep_at < SWEEPER_INTERVAL_S:
            return
        idx = SWEEPER_CYCLE.index(zone.channel) if zone.channel in SWEEPER_CYCLE else -1
        prev = zone.channel
        zone.channel = SWEEPER_CYCLE[(idx + 1) % len(SWEEPER_CYCLE)]
        zone.freq_mhz = channel_freq_mhz(zone.channel)
        zone.last_sweep_at = now
        self._last_rf_counts = (0, 0)
        self.log_event(
            "RF-ALERT", "warn",
            f"SWEEPER hopped CH {prev} -> CH {zone.channel} ({zone.freq_mhz} MHz)"
            + ("; mesh channel is now inside the sweep" if zone.channel == self.active_channel else ""),
        )

    def sweeper_next_hop_s(self) -> float | None:
        zone = self.zone
        if zone is None or zone.mode != ZONE_MODE_SWEEPER:
            return None
        return max(0.0, SWEEPER_INTERVAL_S - (time.monotonic() - zone.last_sweep_at))

    def compute_spectrum(self) -> dict[str, float]:
        spectrum = {}
        zone = self.zone
        for ch in WIFI_CHANNELS:
            if zone is not None and (zone.barrage or abs(ch - zone.channel) <= 1):
                spectrum[str(ch)] = INTERFERED_NOISE_DBM + self.rng.uniform(-1.5, 1.5)
            else:
                spectrum[str(ch)] = BASE_NOISE_FLOOR_DBM + self.rng.uniform(-1.0, 1.0)
        return spectrum

    def node_can_link(self, n: MeshNode) -> bool:
        if n.type == "hardware" and not n.is_gateway and n.reported_status in (STATUS_OFFLINE, STATUS_JAMMED):
            return False
        return n.rf_state != STATUS_JAMMED

    def mast_uavs(self) -> list["UavAgent"]:
        fleet = getattr(self, "fleet", None)
        if fleet is None:
            return []
        return [u for u in fleet.uavs.values() if u.task == TASK_MAST and u.in_mesh and u.node.alt_m >= MAST_MIN_ALT_M]

    def mast_overhead_uav(self) -> "UavAgent | None":
        """The HIGH-MAST UAV on station above the gateway (vertical burn-through link), if any."""
        gw_lat, gw_lng = self.gateway_pos
        for u in self.mast_uavs():
            if haversine_m(gw_lat, gw_lng, u.node.lat, u.node.lng) <= MAST_HORIZONTAL_RADIUS_M:
                return u
        return None

    def _overhead_uav_bonus(self, n: MeshNode, uavs: list[MeshNode]) -> bool:
        """BARRAGE: a UAV almost straight overhead at >= 85 m gives a high-angle LOS bonus."""
        for un in uavs:
            if un.alt_m < BARRAGE_VERTICAL_MIN_ALT_M:
                continue
            horiz = haversine_m(n.lat, n.lng, un.lat, un.lng)
            if math.degrees(math.atan2(un.alt_m, max(horiz, 0.1))) >= BARRAGE_VERTICAL_MIN_ELEV_DEG:
                return True
        return False

    def apply_rf_environment(self, nodes: list[MeshNode]) -> None:
        """Dual-zone attenuation physics: primary drop zone + proportional fringe penalty.

        Vertical burn-through exceptions keep a node linked inside the kill radius:
        the HIGH-MAST UAV itself, the gateway under an on-station Mast UAV, and (in a
        wideband barrage) a ground node with a UAV almost directly overhead.
        """
        self.interference_reports = []
        zone = self.zone
        for n in nodes:
            n.rf_state = "CLEAR"
            n.rf_penalty_db = 0.0
        if zone is None:
            self.estimated_epicenter = None
            return
        mast_overhead = self.mast_overhead_uav()
        mast_ids = {u.id for u in self.mast_uavs()}
        high_uavs = [n for n in nodes if n.type == "uav" and n.alt_m >= BARRAGE_VERTICAL_MIN_ALT_M]
        for n in nodes:
            if not zone.affects_channel(n.channel):
                continue
            d = haversine_m(n.lat, n.lng, zone.lat, zone.lng)
            if d <= zone.kill_radius_m:
                if n.id in mast_ids or (n.is_gateway and mast_overhead is not None):
                    n.rf_state = STATUS_DEGRADED
                    n.rf_penalty_db = MAST_BURNTHROUGH_PENALTY_DB
                elif zone.barrage and n.type != "uav" and self._overhead_uav_bonus(n, high_uavs):
                    n.rf_state = STATUS_DEGRADED
                    n.rf_penalty_db = max(0.0, FRINGE_MAX_PENALTY_DB - BARRAGE_VERTICAL_BONUS_DB)
                else:
                    n.rf_state = STATUS_JAMMED
            elif d <= zone.bleed_radius_m:
                penalty = ((zone.bleed_radius_m - d) / (zone.bleed_radius_m - zone.kill_radius_m)) * FRINGE_MAX_PENALTY_DB
                if n.type == "uav" and n.alt_m > 0:
                    # A ground-based source radiates least toward the zenith: an airborne receiver
                    # in the fringe sees the penalty shrink with its elevation angle above the source.
                    penalty *= 1.0 - min(1.0, math.degrees(math.atan2(n.alt_m, max(d, 1.0))) / 90.0)
                if n.is_gateway and mast_overhead is not None:
                    penalty = min(penalty, MAST_BURNTHROUGH_PENALTY_DB)
                n.rf_state = STATUS_DEGRADED
                n.rf_penalty_db = penalty
                if not (n.reachable or n.is_gateway):
                    continue  # a fringe node without a route cannot deliver its report
                noise_floor = BASE_NOISE_FLOOR_DBM + (penalty / FRINGE_MAX_PENALTY_DB) * (INTERFERED_NOISE_DBM - BASE_NOISE_FLOOR_DBM)
                self.interference_reports.append({
                    "type": "INTERFERENCE_REPORT",
                    "node_id": n.id,
                    "coords": (n.lat, n.lng),
                    "noise_floor_dbm": noise_floor,
                    "penalty_db": penalty,
                })
        self.estimated_epicenter = self.estimate_epicenter(nodes)

    def estimate_epicenter(self, nodes: list[MeshNode]) -> tuple[float, float] | None:
        """Strict no-cheat tomography of the interference centre.

        Only observable evidence is used: (1) fringe reports from degraded nodes that still
        have a route, weighted by their attenuation; (2) missing-edge midpoints between a
        surviving parent's live position and its silent child's LKGP; (3) as a last resort
        the centroid of the silent nodes' LKGPs. The live coordinates of JAMMED / OFFLINE
        nodes are never read -- the gateway cannot hear them.
        """
        if self.zone is None:
            return None
        silent_states = (STATUS_JAMMED, STATUS_OFFLINE)
        silent = [
            n for n in nodes
            if not n.is_gateway and n.type != "uav"
            and (n.rf_state == STATUS_JAMMED or n.status in silent_states or not n.reachable)
        ]
        # Fringe reports come from nodes that still have a route; their coords are observable.
        samples = [(r["coords"][0], r["coords"][1], r["penalty_db"]) for r in self.interference_reports]
        # Missing-edge tomography: the link died somewhere between a surviving parent and its
        # last known child position, so the midpoint is evidence of the attenuation boundary.
        by_id = {n.id: n for n in nodes}
        for n in silent:
            parent = by_id.get(n.last_reachable_parent_id or "")
            if parent is None or not parent.reachable or parent.status in silent_states:
                continue
            samples.append(((parent.lat + n.lkgp_lat) / 2, (parent.lng + n.lkgp_lng) / 2, MISSING_EDGE_WEIGHT_DB))
        total_w = sum(w for _, _, w in samples)
        if total_w > 0:
            return sum(la * w for la, _, w in samples) / total_w, sum(ln * w for _, ln, w in samples) / total_w
        if silent:
            return sum(n.lkgp_lat for n in silent) / len(silent), sum(n.lkgp_lng for n in silent) / len(silent)
        return None

    def recompute_topology(self) -> None:
        """Rebuilds the unified directed graph, Dijkstra tree, node states and metrics."""
        gw_lat, gw_lng = self.gateway_pos
        self.gateway.lat, self.gateway.lng = gw_lat, gw_lng
        self.gateway.channel = self.active_channel
        for hw in self.hw_nodes.values():
            if hw.proj_distance_m is not None:
                bearing = (hw.hw_id * 137.5) % 360
                hw.lat, hw.lng = destination(gw_lat, gw_lng, hw.proj_distance_m, bearing)

        nodes = self.all_nodes()
        uav_nodes = self.fleet.mesh_nodes()
        for un in uav_nodes:
            un.channel = self.active_channel
        graph_nodes = [*nodes, *uav_nodes]
        self.advance_sweeper()
        self.spectrum_noise = self.compute_spectrum()
        self.apply_rf_environment(graph_nodes)

        g = nx.DiGraph()
        for n in nodes:
            g.add_node(n.id, type=n.type)
        linkable = [n for n in nodes if self.node_can_link(n)]
        best_neighbor: dict[str, float] = {}
        for i in range(len(linkable)):
            a = linkable[i]
            for j in range(i + 1, len(linkable)):
                self._try_link(g, a, linkable[j], best_neighbor)

        # Ground-only Dijkstra decides which nodes the relay fleet must bridge.
        _, ground_paths = nx.single_source_dijkstra(g, GATEWAY_KEY, weight="weight")
        self.ground_paths = ground_paths
        self.ground_link_rssi = {nid: g.edges[p[-2], nid]["rssi"] for nid, p in ground_paths.items() if len(p) >= 2}
        self.isolated_ground = {
            n.id for n in nodes
            if n.id not in ground_paths and self.node_can_link(n) and not n.is_gateway
        }

        paths = ground_paths
        if uav_nodes:
            for un in uav_nodes:
                g.add_node(un.id, type="uav")
            linkable_uavs = [un for un in uav_nodes if self.node_can_link(un)]
            for i, a in enumerate(linkable_uavs):
                for n in linkable:
                    self._try_link(g, a, n, best_neighbor)
                for other in linkable_uavs[i + 1:]:
                    self._try_link(g, a, other, best_neighbor)
            _, paths = nx.single_source_dijkstra(g, GATEWAY_KEY, weight="weight")
        for u in self.fleet.uavs.values():
            if not u.in_mesh:
                n = u.node
                n.status, n.rssi, n.snr, n.reachable = STATUS_ONLINE, None, None, False
                n.parent_id, n.hop_path = None, []
                n.rf_state, n.rf_penalty_db = "CLEAR", 0.0

        self.mesh_graph = g
        self.paths = paths
        self.update_node_states(graph_nodes, best_neighbor)
        # Stranded: stuck on an old channel, or silent (jammed / no route). Both need a UAV
        # or the node's own watchdog to recover; the gateway cannot command them.
        self.stranded_nodes = {
            n.id for n in nodes
            if not n.is_gateway
            and not (n.type == "hardware" and n.reported_status == STATUS_OFFLINE)
            and (n.channel != self.active_channel or n.status == STATUS_JAMMED or not n.reachable)
        }
        self.compute_metrics(nodes)

    def _try_link(self, g: nx.DiGraph, a: MeshNode, b: MeshNode, best_neighbor: dict[str, float]) -> None:
        if a.channel != b.channel:
            return
        d = haversine_m(a.lat, a.lng, b.lat, b.lng)
        a_air, b_air = a.type == "uav", b.type == "uav"
        if a_air or b_air:
            d = math.hypot(d, a.alt_m - b.alt_m)
            a2a = a_air and b_air
            if d > (UAV_A2A_MAX_M if a2a else RELAY_LINK_MAX_M):
                return
            nominal = air_rssi_nominal(d, abs(a.alt_m - b.alt_m), a2a) + self.rng.gauss(0, RSSI_SIGMA)
        else:
            nominal = self.link_rssi(a, b, d)
            if nominal is None:
                return
        rssi = nominal - max(a.rf_penalty_db, b.rf_penalty_db)
        if rssi <= LINK_RSSI_MIN:
            return
        attrs = {"weight": link_cost(rssi), "rssi": rssi, "nominal_rssi": nominal, "distance_m": d}
        g.add_edge(a.id, b.id, **attrs)
        g.add_edge(b.id, a.id, **attrs)
        best_neighbor[a.id] = max(best_neighbor.get(a.id, -999.0), rssi)
        best_neighbor[b.id] = max(best_neighbor.get(b.id, -999.0), rssi)

    def update_node_states(self, nodes: list[MeshNode], best_neighbor: dict[str, float]) -> None:
        t = self.swarm.t
        for n in nodes:
            path = self.paths.get(n.id)
            n.reachable = path is not None
            n.hop_path = list(path) if path else []
            n.parent_id = path[-2] if path and len(path) >= 2 else None
            # LKGP datum: the gateway only learns a node's position while it has a route to it.
            if n.reachable:
                n.lkgp_lat, n.lkgp_lng, n.lkgp_ts = n.lat, n.lng, t
                n.last_reachable_parent_id = n.parent_id
                n.orphan_since = None
            else:
                n.orphan_since = n.orphan_since if n.orphan_since is not None else t
            if n.is_gateway:
                if n.rf_state == STATUS_JAMMED:
                    n.status = STATUS_JAMMED
                elif n.rf_state == STATUS_DEGRADED:
                    n.status = STATUS_DEGRADED
                else:
                    n.status = STATUS_ONLINE
                n.rssi = None
                n.snr = None
                continue
            if n.parent_id is not None:
                edge = self.mesh_graph.edges[n.parent_id, n.id]
                nominal = edge["nominal_rssi"]
                n.rssi = edge["rssi"]
            else:
                nominal = best_neighbor.get(n.id)
                n.rssi = nominal
            if n.type == "hardware" and n.reported_status == STATUS_OFFLINE:
                n.status = STATUS_OFFLINE
                n.rssi = None
            elif n.rf_state == STATUS_JAMMED or (n.type == "hardware" and n.reported_status == STATUS_JAMMED):
                n.status = STATUS_JAMMED
                n.rssi = JAMMED_RSSI_DBM
            elif n.rf_state == STATUS_DEGRADED:
                n.status = STATUS_DEGRADED
                if nominal is not None:
                    n.rssi = max(FRINGE_RSSI_FLOOR_DBM, nominal - n.rf_penalty_db)
            elif not n.reachable:
                n.status = STATUS_OFFLINE
            else:
                n.status = STATUS_ONLINE
            noise = self.spectrum_noise.get(str(n.channel), BASE_NOISE_FLOOR_DBM)
            if n.status == STATUS_JAMMED:
                n.snr = JAMMED_SNR_DB
            elif n.rssi is not None:
                n.snr = n.rssi - noise
            else:
                n.snr = None

    def compute_metrics(self, nodes: list[MeshNode]) -> None:
        members = [n for n in nodes if not n.is_gateway]
        if not members:
            self.metrics.update(health_score=100.0, pdr_percent=100.0, avg_latency_ms=0.0, graph_diameter=0, reachable_count=0)
            return
        pdr_sum = 0.0
        latencies = []
        diameter = 0
        reachable = 0
        for n in members:
            path = self.paths.get(n.id)
            if not path or n.status in (STATUS_JAMMED, STATUS_OFFLINE):
                continue
            reachable += 1
            diameter = max(diameter, len(path) - 1)
            p_total = 1.0
            one_way = 0.0
            for u, v in zip(path, path[1:]):
                p = hop_success(self.mesh_graph.edges[u, v]["rssi"])
                p_total *= p
                one_way += HOP_BASE_LATENCY_MS + (1.0 - p) * HOP_RETRY_LATENCY_MS + abs(self.rng.gauss(0, 0.4))
            pdr_sum += p_total
            latencies.append(2.0 * one_way)
        pdr = 100.0 * pdr_sum / len(members)
        reach_pct = 100.0 * reachable / len(members)
        self.metrics.update(
            pdr_percent=pdr,
            avg_latency_ms=(sum(latencies) / len(latencies)) if latencies else 0.0,
            graph_diameter=diameter,
            reachable_count=reachable,
            health_score=0.6 * pdr + 0.4 * reach_pct,
        )

    def tick(self) -> None:
        self.tick_count += 1
        self.swarm.step(SIM_TICK_S, self.gateway_pos)
        self.fleet.update_homes()
        self.fleet.step(SIM_TICK_S)
        self.recompute_topology()
        self.evaluate_shepherd_and_hunt()
        self.evaluate_rf_alerts()
        self.evaluate_acs()
        self.fleet.evaluate()
        self.stream_mavlink()

    # ------------------------------------------------------------------ #
    # Relay UAV fleet + MAVLink
    # ------------------------------------------------------------------ #
    def uav_event(self, u: UavAgent, severity: str, text: str) -> None:
        """Fleet event: dashboard log line plus a MAVLink STATUSTEXT for that vehicle."""
        self.log_event("FLEET", severity, text)
        self.mavlink.send_statustext(u, severity, text.removeprefix(f"{u.id} ").strip())

    def stream_mavlink(self) -> None:
        for u in self.fleet.uavs.values():
            if u.sysid is None:
                continue
            self.mavlink.send_heartbeat(u)
            self.mavlink.send_global_position(u)
            self.mavlink.send_sys_status(u)
            self.mavlink.send_vfr_hud(u)

    # ------------------------------------------------------------------ #
    # RF attenuation zone & Adaptive Channel Switching
    # ------------------------------------------------------------------ #
    def rf_counts(self) -> tuple[int, int]:
        nodes = self.all_nodes()
        jammed = sum(1 for n in nodes if n.status == STATUS_JAMMED)
        degraded = sum(1 for n in nodes if n.status == STATUS_DEGRADED)
        return jammed, degraded

    def evaluate_rf_alerts(self) -> None:
        counts = self.rf_counts()
        now = time.monotonic()
        first_impact = self._last_rf_counts == (0, 0)
        due = first_impact or now - self._last_rf_alert_at >= RF_ALERT_MIN_INTERVAL_S
        if counts != self._last_rf_counts and self.zone is not None and due:
            jammed, degraded = counts
            if jammed or degraded:
                self._last_rf_alert_at = now
                est = self.estimated_epicenter
                est_txt = f"; est. center {est[0]:.5f}, {est[1]:.5f}" if est else ""
                where = "BARRAGE CH 1-11" if self.zone.barrage else f"{self.zone.mode} CH {self.zone.channel}"
                stranded = len(self.stranded_nodes)
                self.log_event(
                    "RF-ALERT", "critical" if jammed else "warn",
                    f"{where}: {jammed} node(s) in primary drop zone, {degraded} degraded, {stranded} stranded/silent "
                    f"({len(self.interference_reports)} interference reports{est_txt})",
                )
            self._last_rf_counts = counts
        elif self.zone is None or counts == (0, 0):
            self._last_rf_counts = counts

    def deploy_zone(self, msg: dict) -> None:
        lat, lng = float(msg["lat"]), float(msg["lng"])
        kill = float(msg.get("kill_radius_m", ZONE_DEFAULT_KILL_M))
        kill = max(10.0, min(ZONE_MAX_RADIUS_M, kill))
        bleed = float(msg.get("bleed_radius_m", ZONE_DEFAULT_BLEED_M))
        if bleed <= kill:
            bleed = kill * (ZONE_DEFAULT_BLEED_M / ZONE_DEFAULT_KILL_M)
        bleed = min(ZONE_MAX_RADIUS_M * 2.5, bleed)
        if "channel" in msg and msg["channel"] is not None:
            channel = int(msg["channel"])
        elif "freq_mhz" in msg and msg["freq_mhz"] is not None:
            channel = int(round((float(msg["freq_mhz"]) - 2407) / 5))
        else:
            channel = self.active_channel
        if channel not in WIFI_CHANNELS:
            raise ValueError(f"channel {channel} outside 1-11")
        mode = str(msg.get("mode", ZONE_MODE_SPOT)).upper()
        if mode not in ZONE_MODES:
            raise ValueError(f"interference mode {mode!r} not in {ZONE_MODES}")
        if mode == ZONE_MODE_SWEEPER and channel not in SWEEPER_CYCLE:
            channel = min(SWEEPER_CYCLE, key=lambda c: abs(c - channel))
        now = time.monotonic()
        self.zone = InterferenceZone(
            lat=lat, lng=lng, kill_radius_m=kill, bleed_radius_m=bleed,
            channel=channel, freq_mhz=channel_freq_mhz(channel), deployed_at=now, mode=mode, last_sweep_at=now,
        )
        self._last_rf_counts = (0, 0)
        self._barrage_acs_notice = False
        self.log_event(
            "RF-ALERT", "critical",
            f"{self._zone_mode_text()} interference zone placed at {lat:.5f}, {lng:.5f}: "
            f"primary drop {kill:.0f} m / fringe {bleed:.0f} m",
        )
        self.notify_phones(f"[RF-ALERT] {mode} interference detected" + ("" if mode == ZONE_MODE_BARRAGE else f" on CH {channel}"))

    def _zone_mode_text(self) -> str:
        z = self.zone
        if z is None:
            return "no"
        if z.barrage:
            return "BARRAGE (wideband CH 1-11)"
        if z.mode == ZONE_MODE_SWEEPER:
            return f"SWEEPER (hopping 1/6/11 every {SWEEPER_INTERVAL_S:.0f} s, now CH {z.channel})"
        return f"SPOT CH {z.channel} ({z.freq_mhz} MHz)"

    def set_zone_mode(self, mode: str) -> None:
        mode = str(mode).upper()
        if mode not in ZONE_MODES:
            raise ValueError(f"interference mode {mode!r} not in {ZONE_MODES}")
        z = self.zone
        if z is None:
            raise ValueError("no interference zone active")
        if z.mode == mode:
            return
        prev = z.mode
        z.mode = mode
        z.last_sweep_at = time.monotonic()
        if mode == ZONE_MODE_SWEEPER and z.channel not in SWEEPER_CYCLE:
            z.channel = min(SWEEPER_CYCLE, key=lambda c: abs(c - z.channel))
            z.freq_mhz = channel_freq_mhz(z.channel)
        self._last_rf_counts = (0, 0)
        self._barrage_acs_notice = False
        if self.acs_state == ACS_CONFIRMING:
            self._cancel_confirmation(f"interference mode changed {prev} -> {mode}")
        self.log_event("RF-ALERT", "critical", f"Interference mode {prev} -> {mode}: now {self._zone_mode_text()}")
        self.notify_phones(f"[RF-ALERT] Interference mode now {mode}")

    def clear_zone(self) -> None:
        if self.zone is None:
            return
        self.zone = None
        self.estimated_epicenter = None
        self._last_rf_counts = (0, 0)
        self._barrage_acs_notice = False
        if self.acs_state == ACS_CONFIRMING:
            self._cancel_confirmation("interference zone cleared")
        self.log_event("RF-ALERT", "ok", "Interference zone cleared; spectrum nominal")
        self.notify_phones("[RF-CLEAR] Interference cleared")

    def choose_clean_channel(self) -> int:
        """Cleanest non-overlapping channel away from the interference."""
        avoid = self.zone.channel if self.zone is not None else self.active_channel
        candidates = [
            ch for ch in ACS_NON_OVERLAPPING
            if ch != self.active_channel and abs(ch - avoid) >= ACS_MIN_SEPARATION
        ] or [ch for ch in ACS_NON_OVERLAPPING if ch != self.active_channel]

        def interference_rank(ch: int) -> float:
            noise = self.spectrum_noise.get(str(ch), BASE_NOISE_FLOOR_DBM)
            return 0.0 if noise <= BASE_NOISE_FLOOR_DBM + ACS_CLEAN_MARGIN_DB else noise

        # Clean channels tie, and ties prefer CH 11 (2462 MHz) over CH 1 (2412 MHz).
        return min(candidates, key=lambda ch: (interference_rank(ch), -ch))

    def fhss_countdown_s(self) -> float | None:
        if self.acs_state != ACS_CONFIRMING or self.acs_confirm_started is None:
            return None
        return max(0.0, self.acs_confirm_duration_s - (time.monotonic() - self.acs_confirm_started))

    def gateway_in_zone(self) -> bool:
        return self.zone is not None and self.gateway.rf_state in (STATUS_JAMMED, STATUS_DEGRADED)

    def _cancel_confirmation(self, reason: str) -> None:
        self.acs_state = ACS_IDLE
        self.acs_confirm_started = None
        self.acs_target_channel = None
        self.acs_confirm_token += 1
        self.log_event("CH-SWITCH", "info", f"ACS confirmation cancelled: {reason}")

    def evaluate_acs(self) -> None:
        jammed, degraded = self.rf_counts()
        affected = jammed + degraded
        zone = self.zone
        if zone is not None and zone.barrage:
            # Wideband barrage: every channel is noisy at ground level, so hopping is pointless.
            # Recovery falls to Protocol HIGH-MAST and perimeter flanking instead.
            if self.acs_state == ACS_CONFIRMING:
                self._cancel_confirmation("BARRAGE covers every channel; ACS cannot evade it")
            if self.acs_auto and affected and not self._barrage_acs_notice:
                self._barrage_acs_notice = True
                self.log_event(
                    "CH-SWITCH", "warn",
                    "ACS: wideband BARRAGE detected on CH 1-11; no clean channel exists. "
                    "Holding channel; HIGH-MAST gateway proxy and relay flanking arcs take over",
                )
            return
        self._barrage_acs_notice = False
        zone_on_channel = zone is not None and zone.channel == self.active_channel
        gateway_hit = zone_on_channel and self.gateway.rf_state in (STATUS_JAMMED, STATUS_DEGRADED)
        if self.acs_state == ACS_IDLE:
            if self.acs_auto and zone_on_channel and (affected >= ACS_TRIGGER_COUNT or gateway_hit):
                self.acs_state = ACS_CONFIRMING
                self.acs_confirm_started = time.monotonic()
                self.acs_confirm_duration_s = ACS_GATEWAY_EMERGENCY_CONFIRM_S if gateway_hit else ACS_CONFIRM_S
                self.acs_target_channel = self.choose_clean_channel()
                self.acs_confirm_token += 1
                if gateway_hit:
                    self.log_event(
                        "CH-SWITCH", "critical",
                        f"ACS: gateway radio reports CCA noise saturation on CH {self.active_channel} "
                        f"({self.gateway.rf_state}); EMERGENCY channel evacuation to CH {self.acs_target_channel} "
                        f"in {self.acs_confirm_duration_s:.1f}s",
                    )
                else:
                    self.log_event(
                        "CH-SWITCH", "warn",
                        f"ACS: {affected} node(s) disconnected/degraded on CH {self.active_channel}; "
                        f"confirming for {self.acs_confirm_duration_s:.1f}s before switching to CH {self.acs_target_channel}",
                    )
                self._spawn(self._confirm_then_switch(self.acs_confirm_token, self.acs_confirm_duration_s))
        elif self.acs_state == ACS_CONFIRMING:
            if not self.acs_auto:
                self._cancel_confirmation("switched to MANUAL mode")
            elif not zone_on_channel or (affected < ACS_TRIGGER_COUNT and not gateway_hit):
                self._cancel_confirmation("signal degradation subsided")

    async def _confirm_then_switch(self, token: int, delay_s: float = ACS_CONFIRM_S) -> None:
        await asyncio.sleep(delay_s)
        if self.acs_state != ACS_CONFIRMING or token != self.acs_confirm_token:
            return
        target = self.choose_clean_channel()
        await self.execute_channel_switch(target, "GATEWAY_EVACUATION" if self.gateway_in_zone() else "AUTO_CLEAN")

    async def execute_channel_switch(self, target: int, reason: str) -> None:
        if target not in WIFI_CHANNELS:
            self.log_event("CH-SWITCH", "warn", f"Rejected channel switch to invalid CH {target}")
            return
        if self.acs_state in (ACS_SWITCHING, ACS_SCANNING):
            self.log_event("CH-SWITCH", "warn", f"Channel switch to CH {target} ignored: switch already in progress")
            return
        if target == self.active_channel:
            if self.acs_state == ACS_CONFIRMING:
                self._cancel_confirmation(f"already on CH {target}")
            self.log_event("CH-SWITCH", "info", f"Mesh already operating on CH {target}")
            return

        old = self.active_channel
        self.acs_state = ACS_SWITCHING
        self.acs_confirm_token += 1
        self.acs_confirm_started = None
        self.acs_target_channel = target
        t0 = time.perf_counter()
        self.log_event(
            "CH-SWITCH", "warn",
            f"Executing channel switch CH {old} ({channel_freq_mhz(old)} MHz) -> CH {target} "
            f"({channel_freq_mhz(target)} MHz) [{reason}]",
        )

        # 1. Tell the physical ESP32 mesh (and their phones) about the new channel.
        self.serial_send(f"CH_SWITCH:{target}|{reason}")

        # 2. The CH_SWITCH broadcast is an over-the-air frame: only nodes that currently have a
        #    route and are not inside the drop zone can hear it. Everyone else stays on the old
        #    channel at its LKGP and must be recovered causally (Shepherd UAV or its own watchdog).
        self.active_channel = target
        self.gateway.channel = target
        for u in self.fleet.uavs.values():
            if u.airborne:
                u.node.channel = target
        moved, stranded = 0, 0
        t_sim = self.swarm.t
        for n in self.all_nodes():
            if n.type == "hardware" and n.reported_status == STATUS_OFFLINE:
                continue  # not on the air at all
            if n.is_gateway or (n.reachable and n.status != STATUS_JAMMED):
                if n.channel != target:
                    n.channel = target
                    n.stranded_since = None
                    moved += 1
            elif n.channel != target:
                n.stranded_since = n.stranded_since if n.stranded_since is not None else t_sim
                stranded += 1
        self._switch_t0 = t0
        self._switch_sim_t = t_sim
        self._switch_old_channel = old
        self.self_heal_convergence_ms = None
        self.recompute_topology()
        self.acs_state = ACS_IDLE
        self.acs_target_channel = None
        if stranded:
            self.log_event(
                "CH-SWITCH", "warn",
                f"{moved} node(s) heard the CH_SWITCH broadcast and retuned to CH {target}; "
                f"{stranded} silent node(s) stranded on CH {old} at their LKGP -- awaiting Shepherd UAV "
                f"burn-through (<= {SHEPHERD_BURNTHROUGH_SLANT_M:.0f} m slant) or autonomous beacon hunt "
                f"(>= {ORPHAN_BEACON_HUNT_S:.0f} s silence)",
            )
        else:
            self.log_event("CH-SWITCH", "info", f"{moved} node(s) retuned to CH {target} over the air; no stragglers")
        self.evaluate_shepherd_and_hunt()
        await self.broadcast_snapshot()

    # ------------------------------------------------------------------ #
    # Causal recovery of nodes stranded on an old channel
    # ------------------------------------------------------------------ #
    def _hunt_beacon_in_range(self, n: MeshNode, beacons: list[MeshNode], uavs: list["UavAgent"]) -> MeshNode | None:
        """A reachable radio on the active channel that this node's sweep would hear."""
        zone = self.zone
        if zone is not None and zone.affects_channel(self.active_channel):
            if haversine_m(n.lat, n.lng, zone.lat, zone.lng) <= zone.kill_radius_m:
                return None  # the sweep hears only noise on every candidate channel
        for b in beacons:
            if b.id != n.id and haversine_m(n.lat, n.lng, b.lat, b.lng) <= LINK_MAX_M:
                return b
        for u in uavs:
            if math.hypot(haversine_m(n.lat, n.lng, u.node.lat, u.node.lng), u.node.alt_m) <= RELAY_LINK_MAX_M:
                return u.node
        return None

    def evaluate_shepherd_and_hunt(self) -> None:
        """Every tick: Shepherd burn-through and the orphan beacon-hunt watchdog for stranded nodes."""
        target = self.active_channel
        stranded = [
            n for n in self.all_nodes()
            if not n.is_gateway and n.channel != target
            and not (n.type == "hardware" and n.reported_status == STATUS_OFFLINE)
        ]
        zone = self.zone
        if not stranded:
            self._finish_convergence()
            return
        airborne = [u for u in self.fleet.uavs.values() if u.in_mesh and u.node.channel == target]
        beacons = [
            n for n in self.all_nodes()
            if n.channel == target and (n.is_gateway or n.reachable) and n.status not in (STATUS_JAMMED, STATUS_OFFLINE)
        ]
        # A Shepherd already tasked (or still inbound this tick, before allocate() refreshes
        # shepherd_assigned) must be allowed to finish the flyover. Hunt is the fallback only
        # when no UAV is on that duty -- i.e. the fleet is empty, landed, or otherwise unavailable.
        shepherd_inbound = self.fleet.shepherd_assigned > 0 or any(
            u.task == TASK_SHEPHERD for u in self.fleet.uavs.values()
        )
        t = self.swarm.t
        retuned = 0
        for n in stranded:
            # (a) Shepherd UAV proximity burn-through: steep slant path beats the ground noise.
            hit = None
            for u in airborne:
                slant = math.hypot(haversine_m(n.lkgp_lat, n.lkgp_lng, u.node.lat, u.node.lng), u.node.alt_m)
                if slant <= SHEPHERD_BURNTHROUGH_SLANT_M and (hit is None or slant < hit[1]):
                    hit = (u, slant)
            if hit is not None:
                u, slant = hit
                n.channel = target
                n.stranded_since = None
                retuned += 1
                self.log_event(
                    "SHEPHERD", "ok",
                    f"[SHEPHERD] {u.id} burned through RF noise over {n.id} (slant {slant:.0f} m) -> retuned {n.id} to CH {target}",
                )
                continue
            # (b) Autonomous firmware beacon hunt, counted from the channel switch (stranded_since),
            #     never from orphan_since -- a node jammed for minutes would otherwise hunt at 0 s
            #     and self-heal via ground neighbours before any UAV leaves the pad.
            if n.stranded_since is None:
                continue
            elapsed = t - n.stranded_since
            timeout = ORPHAN_BEACON_HUNT_S + n.hunt_jitter_s
            if elapsed < timeout:
                continue
            if shepherd_inbound:
                continue  # UAV is already flying to the LKGP; do not steal the recovery
            beacon = self._hunt_beacon_in_range(n, beacons, airborne)
            if beacon is None:
                continue
            n.channel = target
            n.stranded_since = None
            retuned += 1
            self.log_event(
                "CH-SWITCH", "ok",
                f"[CH-SWITCH] {n.id} autonomous beacon-hunt locked onto CH {target} after {elapsed:.0f} s "
                f"of silence (beacon {beacon.id})",
            )
        if retuned:
            self.recompute_topology()
        remaining = [
            n for n in stranded if n.channel != target
            and not (zone is not None and zone.barrage and haversine_m(n.lat, n.lng, zone.lat, zone.lng) <= zone.kill_radius_m)
        ]
        if not remaining:
            self._finish_convergence()

    def _finish_convergence(self) -> None:
        if self._switch_t0 is None:
            return
        # Wall clock for a sub-second over-the-air switch; sim ticks once Shepherd / hunt recovery took part.
        self.self_heal_convergence_ms = max(
            (time.perf_counter() - self._switch_t0) * 1000.0,
            (self.swarm.t - self._switch_sim_t) * 1000.0,
        )
        members = [n for n in self.all_nodes() if not n.is_gateway]
        reachable = sum(1 for n in members if n.reachable)
        elapsed_s = self.self_heal_convergence_ms / 1000.0
        old = self._switch_old_channel
        self._switch_t0 = None
        self._switch_old_channel = None
        if reachable == len(members):
            self.log_event(
                "CH-SWITCH", "ok",
                f"Self-healing converged in {elapsed_s:.2f} s: every node left CH {old} for CH {self.active_channel}; "
                f"{reachable}/{len(members)} reachable via Dijkstra",
            )
        else:
            self.log_event(
                "CH-SWITCH", "warn",
                f"Channel recovery settled in {elapsed_s:.2f} s on CH {self.active_channel}; "
                f"{reachable}/{len(members)} node(s) reachable, the rest sit inside the drop zone",
            )

    def threat_dict(self) -> dict:
        jammed, degraded = self.rf_counts()
        z = self.zone
        est = self.estimated_epicenter
        countdown = self.fhss_countdown_s()
        mast = self.mast_overhead_uav()
        sweep = self.sweeper_next_hop_s()
        return {
            "active": z is not None,
            "mode": z.mode if z else ZONE_MODE_SPOT,
            "barrage": bool(z and z.barrage),
            "sweeper_next_hop_s": round(sweep, 1) if sweep is not None else None,
            "stranded_count": sum(1 for n in self.all_nodes() if not n.is_gateway and n.channel != self.active_channel),
            "shepherd_assigned": self.fleet.shepherd_assigned,
            "mast_active": mast is not None,
            "mast_uav_id": mast.id if mast else None,
            "gateway_protected": mast is not None,
            "gateway_in_zone": self.gateway_in_zone(),
            "gateway_rf_state": self.gateway.rf_state,
            "epicenter": [z.lat, z.lng] if z else None,
            "estimated_epicenter": [round(est[0], 7), round(est[1], 7)] if est else None,
            "estimation_error_m": round(haversine_m(z.lat, z.lng, est[0], est[1]), 1) if (z and est) else None,
            "kill_radius_m": z.kill_radius_m if z else ZONE_DEFAULT_KILL_M,
            "bleed_radius_m": z.bleed_radius_m if z else ZONE_DEFAULT_BLEED_M,
            "channel": z.channel if z else None,
            "freq_mhz": z.freq_mhz if z else None,
            "jammed_count": jammed,
            "degraded_count": degraded,
            "report_count": len(self.interference_reports),
            "eccm_auto": self.acs_auto,
            "acs_state": self.acs_state,
            "acs_target_channel": self.acs_target_channel,
            "fhss_countdown_s": round(countdown, 2) if countdown is not None else None,
            "fhss_confirm_s": self.acs_confirm_duration_s if countdown is not None else ACS_CONFIRM_S,
        }

    async def sim_loop(self):
        loop = asyncio.get_running_loop()
        next_t = loop.time()
        while True:
            try:
                self.tick()
                await self.broadcast_snapshot()
            except Exception:
                log.exception("Simulation tick failed")
            next_t += SIM_TICK_S
            await asyncio.sleep(max(0.0, next_t - loop.time()))

    # ------------------------------------------------------------------ #
    # Snapshot
    # ------------------------------------------------------------------ #
    def signal_bands(self) -> dict[str, int]:
        """Ground nodes bucketed by their next-hop RSSI (dashboard histogram bands)."""
        bands = {"excellent": 0, "good": 0, "degraded": 0, "disconnected": 0}
        for n in self.all_nodes():
            if n.is_gateway:
                continue
            r = n.rssi if n.reachable else None
            if r is None or r <= LINK_RSSI_MIN:
                bands["disconnected"] += 1
            elif r > RSSI_EXCELLENT_DBM:
                bands["excellent"] += 1
            elif r > RSSI_GOOD_DBM:
                bands["good"] += 1
            else:
                bands["degraded"] += 1
        return bands

    def node_to_dict(self, n: MeshNode) -> dict:
        return {
            "id": n.id,
            "type": n.type,
            "role": n.role,
            "status": n.status,
            "channel": n.channel,
            "coords": [round(n.lat, 7), round(n.lng, 7)],
            "alt_m": round(n.alt_m, 1),
            "rssi": _round(n.rssi, 1),
            "ground_rssi": _round(self.ground_link_rssi.get(n.id), 1),
            "snr": _round(n.snr, 1),
            "battery_mv": None if n.battery_mv is None else int(n.battery_mv),
            "parent_id": n.parent_id,
            "hop_path": n.hop_path,
            "hop_count": max(0, len(n.hop_path) - 1),
            "reachable": n.reachable,
            "holding": n.hold_pos is not None,
            "hold_reason": n.hold_reason,
            "lost_link_s": round(self.swarm.t - n.lost_link_since, 1) if n.lost_link_since is not None else None,
            "lkgp": [round(n.lkgp_lat, 7), round(n.lkgp_lng, 7)],
            "lkgp_age_s": round(self.swarm.t - n.lkgp_ts, 1) if not n.reachable else 0.0,
            "stranded": (not n.is_gateway) and n.channel != self.active_channel,
            "stranded_s": round(self.swarm.t - n.stranded_since, 1) if n.stranded_since is not None else None,
            "orphan_s": round(self.swarm.t - n.orphan_since, 1) if n.orphan_since is not None else None,
        }

    def network_dict(self) -> dict:
        nodes = self.all_nodes()
        return {
            "channel": self.active_channel,
            "carrier_freq_mhz": channel_freq_mhz(self.active_channel),
            "health_score": round(self.metrics["health_score"], 1),
            "pdr_percent": round(self.metrics["pdr_percent"], 2),
            "avg_latency_ms": round(self.metrics["avg_latency_ms"], 2),
            "self_heal_convergence_ms": _round(self.self_heal_convergence_ms, 1),
            "graph_diameter": self.metrics["graph_diameter"],
            "spectrum_noise": {k: round(v, 1) for k, v in self.spectrum_noise.items()},
            "node_count": len(nodes),
            "hardware_count": 1 + len(self.hw_nodes),
            "virtual_count": self.swarm.count,
            "uav_count": len(self.fleet.uavs),
            "airborne_uav_count": sum(1 for u in self.fleet.uavs.values() if u.airborne),
            "signal_bands": self.signal_bands(),
            "uav_served_count": sum(1 for n in nodes if n.parent_id and n.parent_id.startswith(UAV_ID_PREFIX)),
            "reachable_count": self.metrics["reachable_count"],
            "link_count": self.mesh_graph.number_of_edges() // 2,
        }

    def links_list(self) -> list[dict]:
        links = []
        for nid, path in self.paths.items():
            if len(path) < 2:
                continue
            parent = path[-2]
            edge = self.mesh_graph.edges[parent, nid]
            links.append({
                "source": parent,
                "target": nid,
                "rssi": round(edge["rssi"], 1),
                "quality": round(link_quality(edge["rssi"]), 2),
                "distance_m": round(edge["distance_m"], 1),
            })
        return links

    def build_snapshot(self) -> dict:
        return {
            "type": "SNAPSHOT",
            "ts": utc_iso(),
            "tick": self.tick_count,
            "bridge": {
                "serial_connected": self.serial_connected(),
                "serial_port": self.serial_port,
                "baud": self.baud,
                "uptime_s": round(time.monotonic() - self.started_at, 1),
            },
            "gateway": {"id": GATEWAY_KEY, "coords": [self.gateway_pos[0], self.gateway_pos[1]]},
            "swarm": {
                "mode": self.swarm.mode,
                "polygon": [list(v) for v in self.swarm.polygon] if self.swarm.polygon else None,
                "count": self.swarm.count,
                "min": SWARM_MIN,
                "max": SWARM_MAX,
                "radii": [round(r, 1) for r in self.swarm.radii],
                "speed_mps": round(self.swarm.speed_mps, 2),
                "max_ring_gap_m": round(self.swarm.max_ring_gap_m(), 1),
                "link_range_m": LINK_MAX_M,
                "holding_count": self.swarm.holding_count(),
                "limits": {
                    "max_rings": ORBIT_MAX_RINGS,
                    "radius_min_m": ORBIT_RADIUS_MIN_M,
                    "radius_max_m": ORBIT_RADIUS_MAX_M,
                    "min_gap_m": ORBIT_MIN_RING_GAP_M,
                    "speed_min_mps": ORBIT_SPEED_MIN_MPS,
                    "speed_max_mps": ORBIT_SPEED_MAX_MPS,
                },
            },
            "network": self.network_dict(),
            "nodes": [self.node_to_dict(n) for n in [*self.all_nodes(), *self.fleet.mesh_nodes()]],
            "links": self.links_list(),
            "threat": self.threat_dict(),
            "fleet": self.fleet.to_dict(),
        }

    async def broadcast_snapshot(self):
        if not self.clients:
            return
        await self.broadcast(json.dumps(self.build_snapshot()))

    # ------------------------------------------------------------------ #
    # WebSocket side
    # ------------------------------------------------------------------ #
    async def handle_client(self, websocket):
        self.clients.add(websocket)
        peer = websocket.remote_address
        log.info("Dashboard connected: %s (total clients: %d)", peer, len(self.clients))
        try:
            await websocket.send(json.dumps({"type": "LOG_HISTORY", "events": self.log_history}))
            await websocket.send(json.dumps(self.build_snapshot()))
            async for raw_msg in websocket:
                try:
                    msg = json.loads(raw_msg)
                except json.JSONDecodeError:
                    continue
                if not isinstance(msg, dict):
                    continue
                try:
                    await self.handle_command(msg)
                except (KeyError, TypeError, ValueError) as exc:
                    log.warning("Rejected malformed command %s: %s", msg.get("type"), exc)
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            self.clients.discard(websocket)
            log.info("Dashboard disconnected: %s (total clients: %d)", peer, len(self.clients))

    async def handle_command(self, msg: dict) -> None:
        mtype = msg.get("type")
        refresh = True

        if mtype == "OPERATOR_GPS":
            lat, lon = float(msg["lat"]), float(msg["lon"])
            first = self.operator_gps is None
            self.operator_gps = (lat, lon)
            self.gateway_pos = destination(lat, lon, GATEWAY_OFFSET_M, GATEWAY_OFFSET_BEARING)
            if first:
                self.log_event("SERIAL", "ok", f"Operator GPS lock {lat:.5f}, {lon:.5f}; gateway re-anchored")

        elif mtype == "SET_SWARM_SIZE":
            count = self.swarm.set_size(int(msg["count"]), self.gateway_pos, self.active_channel)
            self.log_event("SERIAL", "info", f"Virtual swarm resized to {count} nodes")

        elif mtype == "SET_PLACEMENT_MODE":
            mode = msg.get("mode")
            if mode == PLACEMENT_ORBIT:
                self.swarm.set_orbit()
                rings = "/".join(f"{r:.0f}" for r in self.swarm.radii)
                self.log_event("SERIAL", "info", f"Placement mode: ORBIT_PATROL (concentric rings {rings} m)")
            elif mode == PLACEMENT_POLYGON and self.swarm.polygon:
                points = self.swarm.set_polygon(self.swarm.polygon)
                self.log_event("SERIAL", "info", f"Placement mode: POLYGON_INFILL ({points} target points)")
            else:
                refresh = False

        elif mtype == "SET_DEPLOYMENT_POLYGON":
            vertices = [(float(v[0]), float(v[1])) for v in msg["vertices"]]
            points = self.swarm.set_polygon(vertices)
            if points:
                self.log_event("SERIAL", "ok", f"Coverage polygon set ({len(vertices)} vertices); {points} nodes transiting at {NODE_TRANSIT_SPEED_MPS:.0f} m/s")
            else:
                self.log_event("SERIAL", "warn", "Coverage polygon rejected (needs >= 3 vertices and non-zero area)")
                refresh = False

        elif mtype == "SET_ORBIT_CONFIG":
            try:
                self.swarm.set_orbit_config(msg["radii"], msg.get("speed_mps", self.swarm.speed_mps))
            except ValueError as exc:
                self.log_event("SERIAL", "warn", f"Orbit configuration rejected: {exc}")
                refresh = False
            else:
                rings = "/".join(f"{r:.0f}" for r in self.swarm.radii)
                gap = self.swarm.max_ring_gap_m()
                applies = "nodes transiting to new slots" if self.swarm.mode == PLACEMENT_ORBIT else "applies when Orbit Patrol is selected"
                self.log_event(
                    "SERIAL", "ok",
                    f"Orbit configuration: rings {rings} m at {self.swarm.speed_mps:.1f} m/s; {applies}",
                )
                if gap > LINK_MAX_M:
                    self.log_event(
                        "SERIAL", "warn",
                        f"Ring gap {gap:.0f} m exceeds the {LINK_MAX_M:.0f} m link range; outer rings need relay UAVs",
                    )

        elif mtype in ("UPLINK_TX", "MANUAL_PACKET"):
            target = str(msg.get("target_id", msg.get("target_node", "")))
            text = self._sanitize_serial_text(msg.get("message", msg.get("payload", "")))
            if text:
                self.uplink(target, text)
            refresh = False

        elif mtype == "REQUEST_SNAPSHOT":
            pass

        else:
            refresh = self.handle_extended_command(mtype, msg)

        if refresh:
            self.recompute_topology()
            await self.broadcast_snapshot()

    def handle_extended_command(self, mtype, msg: dict) -> bool:
        """RF / ACS / fleet commands. Returns True if a topology refresh is needed."""
        if mtype == "FLEET_ADD":
            try:
                self.fleet.add(int(msg.get("count", 1)), msg.get("uav_type"))
            except ValueError as exc:
                self.log_event("FLEET", "warn", str(exc))
                return False
            return True
        if mtype == "SET_COVERAGE":
            self.fleet.coverage_enabled = bool(msg.get("enabled", True))
            self.log_event(
                "FLEET", "info",
                "Coverage duty enabled: UAVs fly over weak next-hop areas" if self.fleet.coverage_enabled
                else "Coverage duty disabled: UAVs only bridge isolated clusters",
            )
            return True
        if mtype == "FLEET_REMOVE":
            ok, text = self.fleet.remove(str(msg["uav_id"]))
            self.log_event("FLEET", "info" if ok else "warn", text)
            return True
        if mtype == "UAV_COMMAND":
            args = {k: msg[k] for k in ("lat", "lng", "alt_m", "speed_mps") if msg.get(k) is not None}
            self.fleet.command(str(msg["uav_id"]), str(msg["action"]), "DASHBOARD", **args)
            return True
        if mtype == "FLEET_COMMAND":
            try:
                self.fleet.fleet_command(str(msg["action"]))
            except ValueError as exc:
                self.log_event("FLEET", "warn", str(exc))
                return False
            return True
        if mtype == "DEPLOY_JAMMER":
            self.deploy_zone(msg)
            return True
        if mtype == "CLEAR_JAMMER":
            self.clear_zone()
            return True
        if mtype == "SET_JAMMER_MODE":
            try:
                self.set_zone_mode(str(msg["mode"]))
            except ValueError as exc:
                self.log_event("RF-ALERT", "warn", f"Interference mode rejected: {exc}")
                return False
            return True
        if mtype == "SET_ECCM_MODE":
            self.acs_auto = bool(msg["auto"])
            self.log_event("CH-SWITCH", "info", f"Adaptive Channel Switching: {'AUTO' if self.acs_auto else 'MANUAL'}")
            if not self.acs_auto and self.acs_state == ACS_CONFIRMING:
                self._cancel_confirmation("switched to MANUAL mode")
            return True
        if mtype == "EXECUTE_FHSS":
            target = int(msg["target_channel"])
            if self.acs_state == ACS_CONFIRMING:
                self.acs_state = ACS_IDLE
                self.acs_confirm_token += 1
            self._spawn(self.execute_channel_switch(target, "AUTO_CLEAN" if msg.get("auto") else "MANUAL"))
            return False
        return False

    def uplink(self, target: str, text: str) -> None:
        path = self.paths.get(target)
        if target and path and len(path) >= 2:
            self.emit_packet_animation("outbound", path, text)
            self.log_event("MSG", "info", f"UPLINK -> {target} via {len(path) - 1} hop(s): {text}")
        elif target and target != GATEWAY_KEY:
            self.log_event("MSG", "warn", f"UPLINK -> {target}: no route in mesh graph; flooding to hardware only: {text}")
        else:
            self.log_event("MSG", "info", f"UPLINK broadcast: {text}")
        if self.serial_connected():
            self.notify_phones(text)
        else:
            self.log_event("SERIAL", "warn", "Gateway serial offline; message not delivered to physical nodes")

    async def broadcast(self, message: str):
        if not self.clients:
            return
        clients = list(self.clients)
        results = await asyncio.gather(
            *(client.send(message) for client in clients),
            return_exceptions=True,
        )
        for client, result in zip(clients, results):
            if isinstance(result, Exception):
                self.clients.discard(client)

    # ------------------------------------------------------------------ #
    # Serial side
    # ------------------------------------------------------------------ #
    def _open_serial(self) -> serial.Serial:
        ser = serial.Serial(self.serial_port, self.baud, timeout=1, write_timeout=1)
        ser.dtr = False
        ser.rts = True
        time.sleep(0.2)
        ser.rts = False
        ser.reset_input_buffer()
        return ser

    @staticmethod
    def _extract_json(line: str) -> str | None:
        try:
            json.loads(line)
            return line
        except json.JSONDecodeError:
            pass
        start = line.find("{")
        end = line.rfind("}")
        if start != -1 and end > start:
            candidate = line[start : end + 1]
            try:
                json.loads(candidate)
                return candidate
            except json.JSONDecodeError:
                pass
        return None

    async def serial_loop(self):
        loop = asyncio.get_running_loop()
        while True:
            ser = None
            try:
                log.info("Opening serial port %s @ %d baud...", self.serial_port, self.baud)
                ser = await loop.run_in_executor(None, self._open_serial)
                self.active_ser = ser
                self.log_event("SERIAL", "ok", f"Gateway serial {self.serial_port} open @ {self.baud} baud")

                last_data = loop.time()
                warned_silent = False

                while True:
                    raw = await loop.run_in_executor(None, ser.readline)
                    if not raw:
                        silent_for = loop.time() - last_data
                        if silent_for > SILENCE_RESET_S:
                            log.warning(
                                "No serial data for %.0fs -- cycling %s to auto-reset the gateway ESP32.",
                                silent_for, self.serial_port,
                            )
                            break
                        if not warned_silent and silent_for > SILENCE_WARN_S:
                            log.warning(
                                "No serial data for %.0fs. Is the gateway sketch printing? (auto-reset at %.0fs)",
                                silent_for, SILENCE_RESET_S,
                            )
                            warned_silent = True
                        continue

                    last_data = loop.time()
                    warned_silent = False

                    line = raw.decode("utf-8", errors="replace").strip()
                    if line:
                        self.process_serial_line(line)

            except serial.SerialException as exc:
                log.error("Serial error (%s). Retrying in %.0fs...", exc, SERIAL_RETRY_DELAY)
            except OSError as exc:
                log.error("OS error on serial port (%s). Retrying in %.0fs...", exc, SERIAL_RETRY_DELAY)
            finally:
                if self.active_ser is not None:
                    self.log_event("SERIAL", "warn", f"Gateway serial {self.serial_port} disconnected")
                self.active_ser = None
                if ser is not None:
                    try:
                        ser.close()
                    except Exception:
                        pass

            await asyncio.sleep(SERIAL_RETRY_DELAY)

    def process_serial_line(self, line: str) -> None:
        payload = self._extract_json(line)
        if payload is None:
            self.process_serial_text(line)
            return
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return
        if not isinstance(data, dict):
            return
        try:
            if "reply" in data:
                self.process_reply(data)
            elif "node_id" in data and "status" in data:
                self.process_telemetry(data)
        except Exception as exc:
            log.error("Error mapping payload %s: %s", payload, exc)

    def process_serial_text(self, line: str) -> None:
        drop = DROP_LINE_RE.search(line)
        if drop:
            lost_id = int(drop.group(1))
            if lost_id != GATEWAY_ID:
                self.dropped_ids.add(lost_id)
                if self._mark_offline(lost_id):
                    self.log_event("SERIAL", "warn", f"Gateway reports lost connection to HW node {lost_id}; marked OFFLINE")
        newc = NEW_CONN_RE.search(line)
        if newc:
            joined_id = int(newc.group(1))
            if joined_id in self.dropped_ids:
                self.dropped_ids.discard(joined_id)
                hw = self.hw_nodes.get(joined_id)
                if hw is not None:
                    hw.null_streak = 0
            self.log_event("SERIAL", "ok", f"Gateway reports new mesh connection: {joined_id}")
        if line.startswith(FIRMWARE_ACK_PREFIXES):
            self.log_event("SERIAL", "info", line[:200])
        elif not (drop or newc):
            log.info("Serial (non-JSON): %s", line[:200])

    def process_reply(self, data: dict) -> None:
        node_key = str(data.get("node_id"))
        text = str(data.get("reply", ""))
        self.log_event("MSG", "ok", f"REPLY from HW {node_key}: {text}")
        frame = json.dumps({"type": "NODE_REPLY", "node_id": node_key, "reply": text, "ts": utc_iso()})
        self._spawn(self.broadcast(frame))
        path = self.paths.get(node_key)
        if path and len(path) >= 2:
            self.emit_packet_animation("inbound", list(reversed(path)), text)

    def process_telemetry(self, data: dict) -> None:
        node_id = int(data["node_id"])
        status = str(data["status"])
        rssi = data.get("parent_rssi", "disconnected")
        channel = data.get("channel")

        if node_id == GATEWAY_ID:
            return

        try:
            rssi_numeric = float(rssi)
            if rssi_numeric >= 0:
                rssi_numeric = None
        except (ValueError, TypeError):
            rssi_numeric = None

        hw = self.hw_nodes.get(node_id)
        now = time.monotonic()

        # Case 1: no valid parent link
        if rssi_numeric is None and status != STATUS_JAMMED:
            streak = (hw.null_streak if hw else 0) + 1
            if hw is not None:
                hw.null_streak = streak
            if streak < NULL_RSSI_STREAK_LIMIT:
                return  # tolerate temporary glitches while painlessMesh rescans
            if hw is None:
                hw = self._create_hw_node(node_id)
                hw.null_streak = streak
            prev = hw.reported_status
            self._mark_offline(node_id)
            hw.reported_rssi = None
            hw.distance_m = None
            self.dropped_ids.add(node_id)
            if prev != STATUS_OFFLINE:
                self.log_event("SERIAL", "warn", f"HW node {node_id} has no parent link (parent_rssi={rssi}); marked OFFLINE")
            return

        # Case 2: validly connected or reporting signal loss
        if hw is None:
            hw = self._create_hw_node(node_id)
            self.log_event("SERIAL", "ok", f"HW edge node {node_id} joined the digital twin")
        prev = hw.reported_status
        hw.reported_status = STATUS_JAMMED if status == STATUS_JAMMED else STATUS_ONLINE
        hw.last_seen = now
        hw.offline_since = None
        hw.null_streak = 0
        self.dropped_ids.discard(node_id)
        if isinstance(channel, (int, float)) and 1 <= int(channel) <= 13:
            hw.channel = int(channel)

        if hw.reported_status == STATUS_JAMMED:
            hw.reported_rssi = None
            if prev != STATUS_JAMMED:
                self.log_event("RF-ALERT", "critical", f"HW node {node_id} reports critical signal loss (BOOT button simulation)")
        else:
            hw.reported_rssi = rssi_numeric
            dist = rssi_to_meters(rssi_numeric)
            hw.distance_m = round(dist, 2) if dist is not None else None
            target = display_distance(hw.distance_m, node_id)
            if hw.proj_distance_m is None:
                hw.proj_distance_m = target
            else:
                hw.proj_distance_m += HW_DISTANCE_SMOOTHING * (target - hw.proj_distance_m)
            if prev == STATUS_OFFLINE:
                self.log_event("SERIAL", "ok", f"HW node {node_id} telemetry resumed (RSSI {rssi_numeric:.0f} dBm); back ONLINE")
        log.info("MESH TLM %s status=%s rssi=%s dist=%s ch=%s", node_id, status, rssi_numeric, hw.distance_m, hw.channel)

    def _create_hw_node(self, node_id: int) -> MeshNode:
        lat, lng = self.gateway_pos
        hw = MeshNode(
            id=str(node_id),
            type="hardware",
            lat=lat,
            lng=lng,
            hw_id=node_id,
            channel=self.active_channel,
            last_seen=time.monotonic(),
            proj_distance_m=display_distance(None, node_id),
            hunt_jitter_s=self.rng.uniform(0.0, ORPHAN_HUNT_JITTER_MAX_S),
        )
        self.hw_nodes[node_id] = hw
        return hw

    def _mark_offline(self, node_id: int) -> bool:
        hw = self.hw_nodes.get(node_id)
        if hw is None or hw.reported_status == STATUS_OFFLINE:
            return False
        hw.reported_status = STATUS_OFFLINE
        hw.offline_since = hw.offline_since or time.monotonic()
        return True

    def _purge_node(self, node_id: int) -> None:
        if self.hw_nodes.pop(node_id, None) is not None:
            self.dropped_ids.discard(node_id)
            self.log_event("SERIAL", "warn", f"HW node {node_id} purged from topology")

    async def watchdog(self):
        while True:
            await asyncio.sleep(WATCHDOG_INTERVAL_S)
            now = time.monotonic()
            to_purge = []
            for node_id, hw in list(self.hw_nodes.items()):
                silent_for = now - hw.last_seen
                if hw.reported_status != STATUS_OFFLINE and silent_for > STALE_OFFLINE_S:
                    if self._mark_offline(node_id):
                        self.log_event("SERIAL", "warn", f"HW node {node_id} silent for {silent_for:.0f}s; marked OFFLINE")
                if hw.reported_status == STATUS_OFFLINE and hw.offline_since is not None:
                    if now - hw.offline_since > OFFLINE_REMOVE_S:
                        to_purge.append(node_id)
            for node_id in to_purge:
                self._purge_node(node_id)

    # ------------------------------------------------------------------ #
    # Entrypoint
    # ------------------------------------------------------------------ #
    async def gcs_loop(self):
        while True:
            await asyncio.sleep(GCS_POLL_INTERVAL_S)
            if self.mavlink.poll(self.fleet):
                await self.broadcast_snapshot()

    def background_tasks(self) -> list:
        return [self.serial_loop(), self.serial_writer(), self.watchdog(), self.sim_loop(), self.gcs_loop()]

    async def run(self):
        self.serial_queue = asyncio.Queue()
        async with websockets.serve(self.handle_client, self.ws_host, self.ws_port):
            log.info("WebSocket server listening on ws://%s:%d", self.ws_host, self.ws_port)
            self.log_event("SERIAL", "info", f"Digital twin online: {self.swarm.count} virtual nodes on channel {self.active_channel}")
            await asyncio.gather(*self.background_tasks())


def main():
    parser = argparse.ArgumentParser(description="AeroMesh HIL digital twin bridge")
    parser.add_argument("--port", default="COM9", help="Serial port (default: COM9)")
    parser.add_argument("--baud", type=int, default=115200, help="Baud rate (default: 115200)")
    parser.add_argument("--ws-host", default="localhost", help="WebSocket bind host (default: localhost)")
    parser.add_argument("--ws-port", type=int, default=8765, help="WebSocket port (default: 8765)")
    parser.add_argument("--swarm", type=int, default=SWARM_DEFAULT, help=f"Virtual node count {SWARM_MIN}-{SWARM_MAX} (default: {SWARM_DEFAULT})")
    default_mix = ",".join(f"{k}:{n}" for k, n in UAV_DEFAULT_MIX)
    parser.add_argument("--uav-mix", help=f"Initial relay fleet as TYPE:COUNT list (default: {default_mix}; types: {', '.join(UAV_TYPES)})")
    parser.add_argument("--uavs", type=int, help="Initial fleet size cycling through every type (ignored with --uav-mix)")
    parser.add_argument("--mavlink-port", type=int, default=MAVLINK_PORT, help=f"Ground station UDP port on {MAVLINK_HOST} (default: {MAVLINK_PORT})")
    args = parser.parse_args()
    try:
        uav_types = parse_uav_mix(args.uav_mix) if args.uav_mix else None
    except ValueError as exc:
        parser.error(str(exc))

    bridge = Bridge(
        args.port, args.baud, args.ws_host, args.ws_port, args.swarm,
        uav_count=args.uavs, uav_types=uav_types, mavlink_port=args.mavlink_port,
    )
    try:
        asyncio.run(bridge.run())
    except KeyboardInterrupt:
        log.info("Bridge stopped by user.")
        sys.exit(0)


if __name__ == "__main__":
    main()
