"""
AeroMesh Bridge -- Hardware-in-the-Loop (HIL) Wireless Mesh Digital Twin.

Combines the physical painlessMesh ESP32 nodes (read from the Gateway over USB
serial) with a configurable virtual mesh swarm inside a single NetworkX
directed graph, and streams a unified 1 Hz telemetry snapshot to every
connected dashboard over WebSocket.

All node movement and signal behaviour of the virtual nodes is a purely
mathematical simulation for educational wireless sensor network research.

Setup:
    pip install pyserial websockets networkx

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
ORBIT_RADII_M = (120.0, 260.0, 420.0)
ORBIT_TANGENTIAL_SPEED_MPS = 3.0
ORBIT_RADIAL_AMPLITUDE_M = 15.0
ORBIT_RADIAL_FREQ = 0.05
NODE_TRANSIT_SPEED_MPS = 12.0
POLYGON_GRID_SHRINK = 0.9
POLYGON_GRID_MAX_ITER = 60
VIRTUAL_BATTERY_DRAIN_MV_PER_S = 0.08
VIRTUAL_BATTERY_FLOOR_MV = 3300.0

PLACEMENT_ORBIT = "ORBIT_PATROL"
PLACEMENT_POLYGON = "POLYGON_INFILL"

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


def link_quality(rssi: float) -> float:
    return max(0.0, min(1.0, (rssi - LINK_RSSI_MIN) / (-50.0 - LINK_RSSI_MIN)))


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
    omega: float = 0.0
    target: tuple[float, float] | None = None
    # RF attenuation state (set by the interference engine)
    rf_state: str = "CLEAR"  # CLEAR | DEGRADED | JAMMED
    rf_penalty_db: float = 0.0

    @property
    def role(self) -> str:
        if self.is_gateway:
            return "gateway"
        return "edge" if self.type == "hardware" else "virtual"


def virtual_id(index: int) -> str:
    return f"0xV_{index:03d}"


# ------------------------------------------------------------------ #
# Virtual swarm simulator
# ------------------------------------------------------------------ #
class SwarmSimulator:
    """Kinematics for N synthetic mesh nodes anchored on the gateway."""

    def __init__(self, count: int, rng: random.Random):
        self.rng = rng
        self.mode = PLACEMENT_ORBIT
        self.polygon: list[tuple[float, float]] | None = None
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
        for n in self.nodes.values():
            n.target = None

    def set_polygon(self, vertices: list[tuple[float, float]]) -> int:
        """Returns the number of target points generated (0 on invalid polygon)."""
        if len(vertices) < 3:
            return 0
        self.polygon = vertices
        self.mode = PLACEMENT_POLYGON
        return self._assign_polygon_targets()

    def _layout_orbit(self) -> None:
        n = len(self.nodes)
        total_r = sum(ORBIT_RADII_M)
        raw = [n * r / total_r for r in ORBIT_RADII_M]
        counts = [int(math.floor(x)) for x in raw]
        remainders = sorted(range(len(raw)), key=lambda k: raw[k] - counts[k], reverse=True)
        for k in remainders[: n - sum(counts)]:
            counts[k] += 1
        ordered = list(self.nodes.values())
        pos = 0
        for ring, ring_count in enumerate(counts):
            direction = 1.0 if ring % 2 == 0 else -1.0
            omega = direction * ORBIT_TANGENTIAL_SPEED_MPS / ORBIT_RADII_M[ring]
            for j in range(ring_count):
                node = ordered[pos]
                node.ring = ring
                node.theta0 = 2 * math.pi * j / ring_count + ring * 0.35
                node.omega = omega
                pos += 1

    def _orbit_position(self, node: MeshNode, anchor: tuple[float, float]) -> tuple[float, float]:
        r = ORBIT_RADII_M[node.ring] + ORBIT_RADIAL_AMPLITUDE_M * math.sin(ORBIT_RADIAL_FREQ * self.t + node.psi)
        theta = node.theta0 + node.omega * self.t
        return offset_m(anchor[0], anchor[1], r * math.sin(theta), r * math.cos(theta))

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
        self.t += dt
        step_m = NODE_TRANSIT_SPEED_MPS * dt
        for node in self.nodes.values():
            if self.mode == PLACEMENT_POLYGON and node.target is not None:
                tlat, tlng = node.target
            else:
                tlat, tlng = self._orbit_position(node, anchor)
            node.lat, node.lng, _ = move_toward(node.lat, node.lng, tlat, tlng, step_m)
            if node.battery_mv is not None:
                node.battery_mv = max(VIRTUAL_BATTERY_FLOOR_MV, node.battery_mv - VIRTUAL_BATTERY_DRAIN_MV_PER_S * dt)


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
        return {str(ch): BASE_NOISE_FLOOR_DBM + self.rng.uniform(-1.0, 1.0) for ch in WIFI_CHANNELS}

    def node_can_link(self, n: MeshNode) -> bool:
        if n.is_gateway:
            return True
        if n.type == "hardware" and n.reported_status in (STATUS_OFFLINE, STATUS_JAMMED):
            return False
        return n.rf_state != STATUS_JAMMED

    def apply_rf_environment(self, nodes: list[MeshNode]) -> None:
        """Hook for the interference engine; clears attenuation when no zone is active."""
        for n in nodes:
            n.rf_state = "CLEAR"
            n.rf_penalty_db = 0.0

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
        self.spectrum_noise = self.compute_spectrum()
        self.apply_rf_environment(nodes)

        g = nx.DiGraph()
        for n in nodes:
            g.add_node(n.id, type=n.type)
        linkable = [n for n in nodes if self.node_can_link(n)]
        best_neighbor: dict[str, float] = {}
        for i in range(len(linkable)):
            a = linkable[i]
            for j in range(i + 1, len(linkable)):
                b = linkable[j]
                if a.channel != b.channel:
                    continue
                d = haversine_m(a.lat, a.lng, b.lat, b.lng)
                nominal = self.link_rssi(a, b, d)
                if nominal is None:
                    continue
                rssi = nominal - max(a.rf_penalty_db, b.rf_penalty_db)
                if rssi <= LINK_RSSI_MIN:
                    continue
                w = (100.0 / abs(rssi)) + 2.0
                attrs = {"weight": w, "rssi": rssi, "nominal_rssi": nominal, "distance_m": d}
                g.add_edge(a.id, b.id, **attrs)
                g.add_edge(b.id, a.id, **attrs)
                best_neighbor[a.id] = max(best_neighbor.get(a.id, -999.0), rssi)
                best_neighbor[b.id] = max(best_neighbor.get(b.id, -999.0), rssi)

        _, paths = nx.single_source_dijkstra(g, GATEWAY_KEY, weight="weight")
        self.mesh_graph = g
        self.paths = paths
        self.update_node_states(nodes, best_neighbor)
        self.compute_metrics(nodes)

    def update_node_states(self, nodes: list[MeshNode], best_neighbor: dict[str, float]) -> None:
        for n in nodes:
            path = self.paths.get(n.id)
            n.reachable = path is not None
            n.hop_path = list(path) if path else []
            n.parent_id = path[-2] if path and len(path) >= 2 else None
            if n.is_gateway:
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
                n.rssi = -95.0
            elif n.rf_state == STATUS_DEGRADED:
                n.status = STATUS_DEGRADED
                if nominal is not None:
                    n.rssi = max(-92.0, nominal - n.rf_penalty_db)
            elif not n.reachable:
                n.status = STATUS_OFFLINE
            else:
                n.status = STATUS_ONLINE
            noise = self.spectrum_noise.get(str(n.channel), BASE_NOISE_FLOOR_DBM)
            if n.status == STATUS_JAMMED:
                n.snr = -12.0
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
        self.recompute_topology()

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
        }

    def network_dict(self) -> dict:
        nodes = self.all_nodes()
        return {
            "channel": self.active_channel,
            "carrier_freq_mhz": channel_freq_mhz(self.active_channel),
            "health_score": round(self.metrics["health_score"], 1),
            "pdr_percent": round(self.metrics["pdr_percent"], 2),
            "avg_latency_ms": round(self.metrics["avg_latency_ms"], 2),
            "self_heal_convergence_ms": None,
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
                "mode": self.swarm.mode,
                "count": self.swarm.count,
                "min": SWARM_MIN,
                "max": SWARM_MAX,
                "polygon": [list(v) for v in self.swarm.polygon] if self.swarm.polygon else None,
            },
            "network": self.network_dict(),
            "nodes": [self.node_to_dict(n) for n in self.all_nodes()],
            "links": self.links_list(),
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
                self.log_event("SERIAL", "info", "Placement mode: ORBIT_PATROL (concentric rings 120/260/420 m)")
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
        """Hook for commands added by later subsystems. Returns True if a refresh is needed."""
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
