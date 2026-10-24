from __future__ import annotations

import math
import re

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

UAV_DEFAULT_TYPE = "QUAD-S"
UAV_DEFAULT_MIX = (("QUAD-S", 2), ("HEXA-H", 1), ("VTOL-FW", 1))

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
