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
import json
import logging
import random
import sys
import time

import networkx as nx
import websockets

from sim.constants import (
    GATEWAY_LAT,
    GATEWAY_LON,
    GATEWAY_ID,
    GATEWAY_KEY,
    DEFAULT_CHANNEL,
    SIM_TICK_S,
    SWARM_MIN,
    SWARM_MAX,
    SWARM_DEFAULT,
    LOG_HISTORY_MAX,
    ACS_CONFIRM_S,
    UAV_DEFAULT_MIX,
    MAVLINK_HOST,
    MAVLINK_PORT,
    GCS_POLL_INTERVAL_S,
    ACS_IDLE,
)
from sim.geo import utc_iso
from sim.nodes import MeshNode, InterferenceZone
from sim.swarm import SwarmSimulator
from sim.mavlink import MavlinkGcsLink
from sim.fleet import UAV_TYPES, parse_uav_mix, UavAgent, FleetManager
from sim.p2p import P2pMixin
from sim.topology import TopologyMixin
from sim.rf import RfMixin
from sim.serial_io import SerialMixin
from sim.snapshot import SnapshotMixin

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)

log = logging.getLogger("bridge")

class Bridge(SerialMixin, TopologyMixin, RfMixin, P2pMixin, SnapshotMixin):
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
        self.p2p_history: list[dict] = []

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
