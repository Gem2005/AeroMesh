"""
AeroMesh Bridge -- Hardware-in-the-Loop (HIL) Wireless Mesh Digital Twin.

Combines the physical painlessMesh ESP32 nodes (read from the Gateway over USB
serial) with a configurable virtual mesh swarm inside a single NetworkX
directed graph, and streams a unified 1 Hz telemetry snapshot to every
connected dashboard over WebSocket.

Subsystems:
    * SwarmSimulator     -- configurable concentric-orbit kinematics with lost-link position hold
    * RF attenuation     -- primary drop zone + fringe penalty, weighted centroid estimate
    * Adaptive Channel Switching (ACS) -- confirmation timer, CH_SWITCH over serial
    * RelayAutopilot     -- midpoint positioning of UAV-RELAY-01 over partitions
    * MavlinkUdpSender   -- MAVLink v2 HEARTBEAT / position / waypoints to udp:127.0.0.1:14550

All node movement and signal behaviour of the virtual nodes is a purely
mathematical simulation for educational wireless sensor network research.

Setup:
    pip install pyserial websockets networkx
    pip install pymavlink   # optional; a standard-library MAVLink framer is used otherwise

Run:
    python bridge.py                       # defaults: COM9 @ 115200, ws://localhost:8765
    python bridge.py --port COM5           # custom serial port
    python bridge.py --port COM9 --baud 115200 --ws-port 8765 --swarm 40
"""

from __future__ import annotations

import argparse
import asyncio
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
SIM_TX_POWER = -40
SIM_PATH_LOSS_EXPONENT = 2.0   # puts the -88 dBm threshold at ~220 m
RSSI_SIGMA = 1.5
LINK_RSSI_MIN = -88.0
LINK_MAX_M = 220.0
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
VIRTUAL_BATTERY_DRAIN_MV_PER_S = 0.08
VIRTUAL_BATTERY_FLOOR_MV = 3300.0

# Lost-link position hold: a disconnected node freezes where it lost the link
# and only resumes its orbit after this many consecutive reachable ticks.
HOLD_RELEASE_TICKS = 3
HOLD_LOST_LINK = "LOST_LINK"
HOLD_UAV_BRIDGED = "UAV_BRIDGED"
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

RF_ALERT_MIN_INTERVAL_S = 5.0

# ------------------------------------------------------------------ #
# Mobile relay (UAV-RELAY-01) & MAVLink telemetry
# ------------------------------------------------------------------ #
UAV_ID_PREFIX = "UAV-"
RELAY_ID = "UAV-RELAY-01"
RELAY_ALT_M = 60.0
RELAY_SPEED_MPS = 18.0
RELAY_ARRIVAL_RADIUS_M = 10.0
RELAY_HOME_OFFSET_M = 45.0
RELAY_HOME_BEARING = 225.0          # loiter south-west of the gateway marker
RELAY_RTB_HOLD_S = 10.0             # partition must stay healed this long before returning
RELAY_WAYPOINT_DEADBAND_M = 40.0    # ignore centroid drift smaller than this
RELAY_DISPATCH_CONFIRM_TICKS = 2    # isolation must persist this many ticks before dispatch
RELAY_ANTENNA_GAIN_DB = 10.0        # elevated high-gain relay antenna (air-to-ground LOS)
RELAY_LINK_MAX_M = 450.0

RELAY_LOITERING = "LOITERING"
RELAY_DISPATCHED = "DISPATCHED"
RELAY_BRIDGING = "BRIDGING_ACTIVE"

MAVLINK_HOST = "127.0.0.1"
MAVLINK_PORT = 14550
MAVLINK_SYSID = 1
MAVLINK_COMPID = 1
MAV_TYPE_QUADROTOR = 2
MAV_AUTOPILOT_GENERIC = 0
MAV_MODE_FLAG_CUSTOM_MODE_ENABLED = 1
MAV_MODE_FLAG_GUIDED_ENABLED = 8
MAV_MODE_FLAG_SAFETY_ARMED = 128
MAV_STATE_STANDBY = 3
MAV_STATE_ACTIVE = 4
MAV_FRAME_GLOBAL_RELATIVE_ALT_INT = 6
MAV_CMD_NAV_WAYPOINT = 16
MAVLINK_MSG_HEARTBEAT = (0, 50)            # (msg id, CRC_EXTRA)
MAVLINK_MSG_GLOBAL_POSITION_INT = (33, 104)
MAVLINK_MSG_MISSION_ITEM_INT = (73, 38)

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
    # Lost-link position hold
    hold_pos: tuple[float, float] | None = None
    hold_reason: str | None = None
    lost_link_since: float | None = None
    reconnect_streak: int = 0
    # RF attenuation state (set by the interference engine)
    rf_state: str = "CLEAR"  # CLEAR | DEGRADED | JAMMED
    rf_penalty_db: float = 0.0

    @property
    def role(self) -> str:
        if self.is_gateway:
            return "gateway"
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


def virtual_id(index: int) -> str:
    return f"0xV_{index:03d}"


# ------------------------------------------------------------------ #
# Virtual swarm simulator
# ------------------------------------------------------------------ #
class SwarmSimulator:
    """Concentric-orbit kinematics for N synthetic mesh nodes anchored on the gateway."""

    def __init__(self, count: int, rng: random.Random):
        self.rng = rng
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
                )
        for nid in [k for k, n in self.nodes.items() if n.index > count]:
            del self.nodes[nid]
        self.nodes = dict(sorted(self.nodes.items(), key=lambda kv: kv[1].index))
        self._layout_orbit()
        return count

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
        # A node served only by an airborne relay stays where the relay is covering it.
        if any(hop.startswith(UAV_ID_PREFIX) for hop in node.hop_path):
            node.reconnect_streak = 0
            node.hold_reason = HOLD_UAV_BRIDGED
            return True
        node.reconnect_streak += 1
        if node.reconnect_streak < HOLD_RELEASE_TICKS:
            node.hold_reason = HOLD_RECONNECTING
            return True
        node.hold_pos = None
        node.hold_reason = None
        node.lost_link_since = None
        node.reconnect_streak = 0
        return False

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
                tlat, tlng = self._orbit_position(node, anchor)
                node.lat, node.lng, _ = move_toward(node.lat, node.lng, tlat, tlng, step_m)
            if node.battery_mv is not None:
                node.battery_mv = max(VIRTUAL_BATTERY_FLOOR_MV, node.battery_mv - VIRTUAL_BATTERY_DRAIN_MV_PER_S * dt)


# ------------------------------------------------------------------ #
# MAVLink v2 UDP telemetry sender
# ------------------------------------------------------------------ #
def x25_crc(data: bytes, crc: int = 0xFFFF) -> int:
    """CRC-16/MCRF4XX (X.25) as used by MAVLink."""
    for byte in data:
        tmp = byte ^ (crc & 0xFF)
        tmp = (tmp ^ (tmp << 4)) & 0xFF
        crc = ((crc >> 8) ^ (tmp << 8) ^ (tmp << 3) ^ (tmp >> 4)) & 0xFFFF
    return crc


class MavlinkUdpSender:
    """
    Streams MAVLink v2 to a ground station on UDP (QGroundControl / Mission
    Planner listen on 127.0.0.1:14550). Uses pymavlink when installed and a
    hand-framed standard-library socket otherwise, so it never hard-fails.
    """

    def __init__(self, host: str = MAVLINK_HOST, port: int = MAVLINK_PORT):
        self.addr = (host, port)
        self.seq = 0
        self.packet_count = 0
        self.boot = time.monotonic()
        self.conn = None
        self.sock = None
        try:
            from pymavlink import mavutil  # type: ignore

            self.conn = mavutil.mavlink_connection(
                f"udpout:{host}:{port}", source_system=MAVLINK_SYSID, source_component=MAVLINK_COMPID
            )
            self.backend = "pymavlink"
        except Exception:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.setblocking(False)
            self.backend = "stdlib"
        log.info("MAVLink v2 telemetry -> udp:%s:%d (%s backend)", host, port, self.backend)

    def frame(self, msg: tuple[int, int], payload: bytes) -> bytes:
        msgid, crc_extra = msg
        body = payload.rstrip(b"\x00") or b"\x00"  # MAVLink v2 trailing-zero truncation
        header = struct.pack(
            "<BBBBBBB", 0xFD, len(body), 0, 0, self.seq, MAVLINK_SYSID, MAVLINK_COMPID
        ) + msgid.to_bytes(3, "little")
        crc = x25_crc(header[1:] + body)
        crc = x25_crc(bytes([crc_extra]), crc)
        self.seq = (self.seq + 1) & 0xFF
        return header + body + struct.pack("<H", crc)

    def _send_raw(self, msg: tuple[int, int], payload: bytes) -> None:
        try:
            self.sock.sendto(self.frame(msg, payload), self.addr)
            self.packet_count += 1
        except OSError as exc:
            log.debug("MAVLink UDP send failed: %s", exc)

    def _send_pymavlink(self, fn, *args) -> None:
        try:
            fn(*args)
            self.packet_count += 1
        except Exception as exc:
            log.debug("pymavlink send failed: %s", exc)

    def send_heartbeat(self, active: bool) -> None:
        base_mode = MAV_MODE_FLAG_CUSTOM_MODE_ENABLED | MAV_MODE_FLAG_GUIDED_ENABLED
        if active:
            base_mode |= MAV_MODE_FLAG_SAFETY_ARMED
        state = MAV_STATE_ACTIVE if active else MAV_STATE_STANDBY
        if self.conn is not None:
            self._send_pymavlink(self.conn.mav.heartbeat_send, MAV_TYPE_QUADROTOR, MAV_AUTOPILOT_GENERIC, base_mode, 0, state)
            return
        payload = struct.pack("<IBBBBB", 0, MAV_TYPE_QUADROTOR, MAV_AUTOPILOT_GENERIC, base_mode, state, 3)
        self._send_raw(MAVLINK_MSG_HEARTBEAT, payload)

    def send_global_position(self, lat: float, lng: float, alt_m: float, speed_mps: float, bearing_deg: float) -> None:
        time_boot_ms = int((time.monotonic() - self.boot) * 1000) & 0xFFFFFFFF
        b = math.radians(bearing_deg)
        vx = int(max(-32768, min(32767, round(speed_mps * math.cos(b) * 100))))
        vy = int(max(-32768, min(32767, round(speed_mps * math.sin(b) * 100))))
        hdg = int(round(bearing_deg * 100)) % 36000
        lat_e7, lng_e7 = int(round(lat * 1e7)), int(round(lng * 1e7))
        alt_mm = int(round(alt_m * 1000))
        if self.conn is not None:
            self._send_pymavlink(self.conn.mav.global_position_int_send, time_boot_ms, lat_e7, lng_e7, alt_mm, alt_mm, vx, vy, 0, hdg)
            return
        payload = struct.pack("<IiiiihhhH", time_boot_ms, lat_e7, lng_e7, alt_mm, alt_mm, vx, vy, 0, hdg)
        self._send_raw(MAVLINK_MSG_GLOBAL_POSITION_INT, payload)

    def send_waypoint(self, lat: float, lng: float, alt_m: float, seq: int = 0) -> None:
        """MISSION_ITEM_INT carrying MAV_CMD_NAV_WAYPOINT (current=2: guided 'fly-to')."""
        lat_e7, lng_e7 = int(round(lat * 1e7)), int(round(lng * 1e7))
        hold_s, accept_m, pass_m, yaw = 0.0, RELAY_ARRIVAL_RADIUS_M, 0.0, float("nan")
        seq &= 0xFFFF
        if self.conn is not None:
            self._send_pymavlink(
                self.conn.mav.mission_item_int_send,
                1, 1, seq, MAV_FRAME_GLOBAL_RELATIVE_ALT_INT, MAV_CMD_NAV_WAYPOINT, 2, 1,
                hold_s, accept_m, pass_m, yaw, lat_e7, lng_e7, alt_m,
            )
            return
        payload = struct.pack(
            "<ffffiifHHBBBBBB",
            hold_s, accept_m, pass_m, yaw, lat_e7, lng_e7, alt_m,
            seq, MAV_CMD_NAV_WAYPOINT, 1, 1, MAV_FRAME_GLOBAL_RELATIVE_ALT_INT, 2, 1, 0,
        )
        self._send_raw(MAVLINK_MSG_MISSION_ITEM_INT, payload)


# ------------------------------------------------------------------ #
# Mobile relay autopilot
# ------------------------------------------------------------------ #
class RelayAutopilot:
    """Midpoint-positioning mobile relay that bridges partitioned mesh islands."""

    def __init__(self, home: tuple[float, float]):
        self.home = home
        self.node = MeshNode(id=RELAY_ID, type="virtual", lat=home[0], lng=home[1])
        self.mission_state = RELAY_LOITERING
        self.target_waypoint: tuple[float, float] | None = None
        self.isolated_centroid: tuple[float, float] | None = None
        self.ground_speed_mps = 0.0
        self.bearing_deg = 0.0
        self.eta_s: float | None = None
        self.isolated_count = 0
        self.bridged_count = 0
        self.isolation_streak = 0
        self.last_isolated_at = 0.0
        self.waypoint_count = 0

    def airborne_bridge(self) -> bool:
        return self.mission_state in (RELAY_DISPATCHED, RELAY_BRIDGING)

    def dispatch(self, waypoint: tuple[float, float]) -> None:
        self.target_waypoint = waypoint
        self.waypoint_count += 1
        on_station = haversine_m(self.node.lat, self.node.lng, *waypoint) <= RELAY_ARRIVAL_RADIUS_M
        if not (self.mission_state == RELAY_BRIDGING and on_station):
            self.mission_state = RELAY_DISPATCHED

    def return_home(self) -> None:
        self.mission_state = RELAY_LOITERING
        self.target_waypoint = None
        self.isolated_centroid = None

    def step(self, dt: float) -> bool:
        """Advances the relay. Returns True on the DISPATCHED -> BRIDGING_ACTIVE transition."""
        goal = self.target_waypoint if (self.airborne_bridge() and self.target_waypoint) else self.home
        d = haversine_m(self.node.lat, self.node.lng, *goal)
        if d > 0.5:
            self.bearing_deg = calculate_bearing(self.node.lat, self.node.lng, *goal)
        lat, lng, remaining = move_toward(self.node.lat, self.node.lng, goal[0], goal[1], RELAY_SPEED_MPS * dt)
        self.node.lat, self.node.lng = lat, lng
        self.ground_speed_mps = (d - remaining) / dt
        self.eta_s = remaining / RELAY_SPEED_MPS
        if self.mission_state == RELAY_DISPATCHED and remaining <= RELAY_ARRIVAL_RADIUS_M:
            self.mission_state = RELAY_BRIDGING
            return True
        return False


# ------------------------------------------------------------------ #
# Bridge / Digital Twin engine
# ------------------------------------------------------------------ #
class Bridge:
    def __init__(self, serial_port: str, baud: int, ws_host: str, ws_port: int, swarm_size: int = SWARM_DEFAULT):
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
        self._last_rf_counts = (0, 0)
        self._last_rf_alert_at = 0.0

        # Mobile relay + MAVLink
        self.isolated_ground: set[str] = set()
        self.mavlink = MavlinkUdpSender(MAVLINK_HOST, MAVLINK_PORT)
        self.relay = RelayAutopilot(self.relay_home())

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
        return SIM_TX_POWER - 10 * SIM_PATH_LOSS_EXPONENT * math.log10(max(d, 1.0)) + self.rng.gauss(0, RSSI_SIGMA)

    def compute_spectrum(self) -> dict[str, float]:
        spectrum = {}
        for ch in WIFI_CHANNELS:
            if self.zone is not None and abs(ch - self.zone.channel) <= 1:
                spectrum[str(ch)] = INTERFERED_NOISE_DBM + self.rng.uniform(-1.5, 1.5)
            else:
                spectrum[str(ch)] = BASE_NOISE_FLOOR_DBM + self.rng.uniform(-1.0, 1.0)
        return spectrum

    def node_can_link(self, n: MeshNode) -> bool:
        if n.type == "hardware" and not n.is_gateway and n.reported_status in (STATUS_OFFLINE, STATUS_JAMMED):
            return False
        return n.rf_state != STATUS_JAMMED

    def apply_rf_environment(self, nodes: list[MeshNode]) -> None:
        """Dual-zone attenuation physics: primary drop zone + proportional fringe penalty."""
        self.interference_reports = []
        zone = self.zone
        for n in nodes:
            n.rf_state = "CLEAR"
            n.rf_penalty_db = 0.0
            if zone is None or n.channel != zone.channel:
                continue
            d = haversine_m(n.lat, n.lng, zone.lat, zone.lng)
            if d <= zone.kill_radius_m:
                n.rf_state = STATUS_JAMMED
            elif d <= zone.bleed_radius_m:
                penalty = ((zone.bleed_radius_m - d) / (zone.bleed_radius_m - zone.kill_radius_m)) * FRINGE_MAX_PENALTY_DB
                n.rf_state = STATUS_DEGRADED
                n.rf_penalty_db = penalty
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
        """Weighted centroid of degraded perimeter reports (weight = observed attenuation)."""
        if self.zone is None:
            return None
        total_w = sum(r["penalty_db"] for r in self.interference_reports)
        if total_w > 0:
            lat = sum(r["coords"][0] * r["penalty_db"] for r in self.interference_reports) / total_w
            lng = sum(r["coords"][1] * r["penalty_db"] for r in self.interference_reports) / total_w
            return lat, lng
        jammed = [n for n in nodes if n.rf_state == STATUS_JAMMED]
        if jammed:
            return sum(n.lat for n in jammed) / len(jammed), sum(n.lng for n in jammed) / len(jammed)
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
        relay = self.relay.node
        relay_in_graph = self.relay.airborne_bridge()
        if relay_in_graph:
            relay.channel = self.active_channel
        graph_nodes = [*nodes, relay] if relay_in_graph else nodes
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

        # Ground-only Dijkstra decides which nodes the mobile relay must bridge.
        _, ground_paths = nx.single_source_dijkstra(g, GATEWAY_KEY, weight="weight")
        self.isolated_ground = {
            n.id for n in nodes
            if n.id not in ground_paths and self.node_can_link(n) and not n.is_gateway
        }

        paths = ground_paths
        if not relay_in_graph:
            relay.status, relay.rssi, relay.snr, relay.reachable = STATUS_ONLINE, None, None, False
            relay.parent_id, relay.hop_path = None, []
        else:
            g.add_node(relay.id, type="relay")
            if self.node_can_link(relay):
                for n in linkable:
                    self._try_link(g, relay, n, best_neighbor)
            _, paths = nx.single_source_dijkstra(g, GATEWAY_KEY, weight="weight")

        self.mesh_graph = g
        self.paths = paths
        self.update_node_states(graph_nodes, best_neighbor)
        self.compute_metrics(nodes)

    def _try_link(self, g: nx.DiGraph, a: MeshNode, b: MeshNode, best_neighbor: dict[str, float]) -> None:
        if a.channel != b.channel:
            return
        d = haversine_m(a.lat, a.lng, b.lat, b.lng)
        if a.id == RELAY_ID or b.id == RELAY_ID:
            d = math.hypot(d, RELAY_ALT_M)  # slant range to the airborne relay
            if d > RELAY_LINK_MAX_M:
                return
            nominal = SIM_TX_POWER + RELAY_ANTENNA_GAIN_DB - 10 * SIM_PATH_LOSS_EXPONENT * math.log10(max(d, 1.0)) + self.rng.gauss(0, RSSI_SIGMA)
        else:
            nominal = self.link_rssi(a, b, d)
            if nominal is None:
                return
        rssi = nominal - max(a.rf_penalty_db, b.rf_penalty_db)
        if rssi <= LINK_RSSI_MIN:
            return
        w = (100.0 / abs(rssi)) + 2.0
        attrs = {"weight": w, "rssi": rssi, "nominal_rssi": nominal, "distance_m": d}
        g.add_edge(a.id, b.id, **attrs)
        g.add_edge(b.id, a.id, **attrs)
        best_neighbor[a.id] = max(best_neighbor.get(a.id, -999.0), rssi)
        best_neighbor[b.id] = max(best_neighbor.get(b.id, -999.0), rssi)

    def update_node_states(self, nodes: list[MeshNode], best_neighbor: dict[str, float]) -> None:
        for n in nodes:
            path = self.paths.get(n.id)
            n.reachable = path is not None
            n.hop_path = list(path) if path else []
            n.parent_id = path[-2] if path and len(path) >= 2 else None
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
        self.update_relay_kinematics()
        self.recompute_topology()
        self.evaluate_rf_alerts()
        self.evaluate_acs()
        self.evaluate_relay()
        self.stream_mavlink()

    # ------------------------------------------------------------------ #
    # Mobile relay (UAV-RELAY-01) + MAVLink
    # ------------------------------------------------------------------ #
    def relay_home(self) -> tuple[float, float]:
        return destination(self.gateway_pos[0], self.gateway_pos[1], RELAY_HOME_OFFSET_M, RELAY_HOME_BEARING)

    def update_relay_kinematics(self) -> None:
        self.relay.home = self.relay_home()
        arrived = self.relay.step(SIM_TICK_S)
        if arrived:
            wp = self.relay.target_waypoint
            self.log_event(
                "MAVLINK", "ok",
                f"Relay Node on station at WP-ALPHA ({wp[0]:.5f}, {wp[1]:.5f}, {RELAY_ALT_M:.0f}m); BRIDGING_ACTIVE",
            )

    def evaluate_relay(self) -> None:
        r = self.relay
        isolated = [n for n in self.all_nodes() if n.id in self.isolated_ground]
        r.isolated_count = len(isolated)
        r.bridged_count = sum(1 for n in self.all_nodes() if RELAY_ID in n.hop_path)
        now = self.tick_count * SIM_TICK_S
        r.isolation_streak = r.isolation_streak + 1 if isolated else 0

        if isolated and r.isolation_streak >= RELAY_DISPATCH_CONFIRM_TICKS:
            r.last_isolated_at = now
            iso_lat = sum(n.lat for n in isolated) / len(isolated)
            iso_lng = sum(n.lng for n in isolated) / len(isolated)
            gw_lat, gw_lng = self.gateway_pos
            relay_lat = (gw_lat + iso_lat) / 2.0
            relay_lng = (gw_lng + iso_lng) / 2.0
            r.isolated_centroid = (iso_lat, iso_lng)
            needs_new_wp = (
                r.mission_state == RELAY_LOITERING
                or r.target_waypoint is None
                or haversine_m(relay_lat, relay_lng, *r.target_waypoint) > RELAY_WAYPOINT_DEADBAND_M
            )
            if needs_new_wp:
                first_dispatch = r.mission_state == RELAY_LOITERING
                r.dispatch((relay_lat, relay_lng))
                self.mavlink.send_waypoint(relay_lat, relay_lng, RELAY_ALT_M, r.waypoint_count)
                self.log_event(
                    "MAVLINK", "warn",
                    f"Relay Node -> WP-ALPHA ({relay_lat:.5f}, {relay_lng:.5f}, {RELAY_ALT_M:.0f}m) "
                    f"bridging {len(isolated)} isolated node(s)",
                )
                if first_dispatch:
                    self.notify_phones(f"[RELAY] {RELAY_ID} dispatched to bridge {len(isolated)} node(s)")
        elif r.mission_state != RELAY_LOITERING and not isolated and now - r.last_isolated_at > RELAY_RTB_HOLD_S:
            r.return_home()
            self.log_event("MAVLINK", "info", f"Partition healed; {RELAY_ID} returning to LOITER over gateway")

    def stream_mavlink(self) -> None:
        r = self.relay
        self.mavlink.send_heartbeat(active=r.mission_state != RELAY_LOITERING)
        self.mavlink.send_global_position(r.node.lat, r.node.lng, RELAY_ALT_M, r.ground_speed_mps, r.bearing_deg)

    def autopilot_dict(self) -> dict:
        r = self.relay
        return {
            "id": RELAY_ID,
            "coords": [round(r.node.lat, 7), round(r.node.lng, 7)],
            "target_waypoint": [round(r.target_waypoint[0], 7), round(r.target_waypoint[1], 7)] if r.target_waypoint else None,
            "home": [round(r.home[0], 7), round(r.home[1], 7)],
            "isolated_centroid": [round(r.isolated_centroid[0], 7), round(r.isolated_centroid[1], 7)] if r.isolated_centroid else None,
            "alt_m": RELAY_ALT_M,
            "speed_mps": round(r.ground_speed_mps, 1),
            "cruise_speed_mps": RELAY_SPEED_MPS,
            "bearing_deg": round(r.bearing_deg, 1),
            "eta_s": round(r.eta_s, 1) if r.eta_s is not None else None,
            "mission_state": r.mission_state,
            "status": r.node.status,
            "isolated_count": r.isolated_count,
            "bridged_count": r.bridged_count,
            "waypoint_count": r.waypoint_count,
            "mavlink_host": MAVLINK_HOST,
            "mavlink_port": MAVLINK_PORT,
            "mavlink_backend": self.mavlink.backend,
            "last_command": "MAV_CMD_NAV_WAYPOINT" if r.waypoint_count else None,
            "packet_count": self.mavlink.packet_count,
        }

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
                self.log_event(
                    "RF-ALERT", "critical" if jammed else "warn",
                    f"CH {self.zone.channel}: {jammed} node(s) in primary drop zone, {degraded} degraded "
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
        self.zone = InterferenceZone(
            lat=lat, lng=lng, kill_radius_m=kill, bleed_radius_m=bleed,
            channel=channel, freq_mhz=channel_freq_mhz(channel), deployed_at=time.monotonic(),
        )
        self._last_rf_counts = (0, 0)
        self.log_event(
            "RF-ALERT", "critical",
            f"Interference zone placed at {lat:.5f}, {lng:.5f} on CH {channel} ({channel_freq_mhz(channel)} MHz): "
            f"primary drop {kill:.0f} m / fringe {bleed:.0f} m",
        )
        self.notify_phones(f"[RF-ALERT] Interference detected on CH {channel}")

    def clear_zone(self) -> None:
        if self.zone is None:
            return
        self.zone = None
        self.estimated_epicenter = None
        self._last_rf_counts = (0, 0)
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
        return max(0.0, ACS_CONFIRM_S - (time.monotonic() - self.acs_confirm_started))

    def _cancel_confirmation(self, reason: str) -> None:
        self.acs_state = ACS_IDLE
        self.acs_confirm_started = None
        self.acs_target_channel = None
        self.acs_confirm_token += 1
        self.log_event("CH-SWITCH", "info", f"ACS confirmation cancelled: {reason}")

    def evaluate_acs(self) -> None:
        jammed, degraded = self.rf_counts()
        affected = jammed + degraded
        zone_on_channel = self.zone is not None and self.zone.channel == self.active_channel
        if self.acs_state == ACS_IDLE:
            if self.acs_auto and zone_on_channel and affected >= ACS_TRIGGER_COUNT:
                self.acs_state = ACS_CONFIRMING
                self.acs_confirm_started = time.monotonic()
                self.acs_target_channel = self.choose_clean_channel()
                self.acs_confirm_token += 1
                self.log_event(
                    "CH-SWITCH", "warn",
                    f"ACS: {affected} node(s) disconnected/degraded on CH {self.active_channel}; "
                    f"confirming for {ACS_CONFIRM_S:.1f}s before switching to CH {self.acs_target_channel}",
                )
                self._spawn(self._confirm_then_switch(self.acs_confirm_token))
        elif self.acs_state == ACS_CONFIRMING:
            if not self.acs_auto:
                self._cancel_confirmation("switched to MANUAL mode")
            elif not zone_on_channel or affected < ACS_TRIGGER_COUNT:
                self._cancel_confirmation("signal degradation subsided")

    async def _confirm_then_switch(self, token: int) -> None:
        await asyncio.sleep(ACS_CONFIRM_S)
        if self.acs_state != ACS_CONFIRMING or token != self.acs_confirm_token:
            return
        target = self.choose_clean_channel()
        await self.execute_channel_switch(target, "AUTO_CLEAN")

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

        # 2. Move every active node to the new channel.
        self.active_channel = target
        moved = 0
        for n in self.all_nodes():
            if n.channel == old and n.status != STATUS_JAMMED and not (n.type == "hardware" and n.reported_status == STATUS_OFFLINE):
                n.channel = target
                moved += 1
        self.recompute_topology()
        await self.broadcast_snapshot()
        self.log_event("CH-SWITCH", "info", f"{moved} active node(s) retuned to CH {target}; scanning for stragglers")

        # 3. After the scan delay, pull the disconnected nodes onto the clean channel.
        self.acs_state = ACS_SCANNING
        await asyncio.sleep(ACS_SCAN_DELAY_S)
        recovered = 0
        for n in self.all_nodes():
            if n.channel == old and not (n.type == "hardware" and n.reported_status == STATUS_OFFLINE):
                n.channel = target
                recovered += 1
        self.recompute_topology()

        # 4. Record self-healing convergence.
        self.self_heal_convergence_ms = (time.perf_counter() - t0) * 1000.0
        members = [n for n in self.all_nodes() if not n.is_gateway]
        reachable = sum(1 for n in members if n.reachable)
        self.acs_state = ACS_IDLE
        self.acs_target_channel = None
        elapsed_s = self.self_heal_convergence_ms / 1000.0
        if reachable == len(members):
            self.log_event(
                "CH-SWITCH", "ok",
                f"Self-healing converged in {elapsed_s:.2f} s on CH {target}: "
                f"{recovered} node(s) recovered, {reachable}/{len(members)} reachable via Dijkstra",
            )
        else:
            self.log_event(
                "CH-SWITCH", "warn",
                f"Channel switch settled in {elapsed_s:.2f} s on CH {target}, but only "
                f"{reachable}/{len(members)} node(s) reachable; CH {target} is still impaired",
            )
        await self.broadcast_snapshot()

    def threat_dict(self) -> dict:
        jammed, degraded = self.rf_counts()
        z = self.zone
        est = self.estimated_epicenter
        countdown = self.fhss_countdown_s()
        return {
            "active": z is not None,
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
            "fhss_confirm_s": ACS_CONFIRM_S,
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
    def node_to_dict(self, n: MeshNode) -> dict:
        return {
            "id": n.id,
            "type": n.type,
            "role": n.role,
            "status": n.status,
            "channel": n.channel,
            "coords": [round(n.lat, 7), round(n.lng, 7)],
            "rssi": _round(n.rssi, 1),
            "snr": _round(n.snr, 1),
            "battery_mv": None if n.battery_mv is None else int(n.battery_mv),
            "parent_id": n.parent_id,
            "hop_path": n.hop_path,
            "hop_count": max(0, len(n.hop_path) - 1),
            "reachable": n.reachable,
            "holding": n.hold_pos is not None,
            "hold_reason": n.hold_reason,
            "lost_link_s": round(self.swarm.t - n.lost_link_since, 1) if n.lost_link_since is not None else None,
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
            "nodes": [self.node_to_dict(n) for n in self.all_nodes()],
            "links": self.links_list(),
            "threat": self.threat_dict(),
            "autopilot": self.autopilot_dict(),
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

        elif mtype == "SET_ORBIT_CONFIG":
            try:
                self.swarm.set_orbit_config(msg["radii"], msg.get("speed_mps", self.swarm.speed_mps))
            except ValueError as exc:
                self.log_event("SERIAL", "warn", f"Orbit configuration rejected: {exc}")
                refresh = False
            else:
                rings = "/".join(f"{r:.0f}" for r in self.swarm.radii)
                gap = self.swarm.max_ring_gap_m()
                self.log_event(
                    "SERIAL", "ok",
                    f"Orbit configuration: rings {rings} m at {self.swarm.speed_mps:.1f} m/s; nodes transiting to new slots",
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
        """RF / ACS commands. Returns True if a topology refresh is needed."""
        if mtype == "DEPLOY_JAMMER":
            self.deploy_zone(msg)
            return True
        if mtype == "CLEAR_JAMMER":
            self.clear_zone()
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
    def background_tasks(self) -> list:
        return [self.serial_loop(), self.serial_writer(), self.watchdog(), self.sim_loop()]

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
    args = parser.parse_args()

    bridge = Bridge(args.port, args.baud, args.ws_host, args.ws_port, args.swarm)
    try:
        asyncio.run(bridge.run())
    except KeyboardInterrupt:
        log.info("Bridge stopped by user.")
        sys.exit(0)


if __name__ == "__main__":
    main()
