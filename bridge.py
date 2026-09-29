"""
AeroMesh Bridge (C2 NetworkX Edition) -- Serial-to-WebSocket relay for the 
Self-Healing Ad-Hoc Wireless Network with UAV Bridge project.

Reads JSON lines from the painlessMesh Gateway ESP32 (USB serial), processes
the topology and Free Space Path Loss (FSPL) using NetworkX and Geopy, and
broadcasts tactical mapping data to all connected WebSocket clients.

Setup:
    pip install pyserial websockets networkx geopy

Run:
    python bridge.py                       # defaults: COM9 @ 115200, ws://localhost:8765
    python bridge.py --port COM5           # custom serial port
    python bridge.py --port COM9 --baud 115200 --ws-port 8765
"""

import argparse
import asyncio
import json
import logging
import re
import sys
import time
import math

import serial  # pyserial
import websockets
import networkx as nx
from geopy.distance import geodesic

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("bridge")

# Seconds to wait before retrying a failed/lost serial connection.
SERIAL_RETRY_DELAY = 3.0
# Warn after this many seconds without serial data.
SILENCE_WARN_S = 10.0
# After this many silent seconds, cycle the port (DTR pulse resets the ESP32).
SILENCE_RESET_S = 30.0

# ------------------------------------------------------------------ #
# NetworkX & Geospatial Configuration
# ------------------------------------------------------------------ #
GATEWAY_LAT = 12.8406
GATEWAY_LON = 80.1534
GATEWAY_ID = 1693866525 # IMPORTANT: Replace with your actual Gateway Node ID

# Log-distance path loss model: d = 10 ^ ((A - RSSI) / (10 * n))
TX_POWER = -40           # A: measured RSSI at 1 m for ESP32 (dBm)
PATH_LOSS_EXPONENT = 2.7 # n: indoor/obstructed environment

# Display-only constraints (do NOT affect the reported distance_m value):
MIN_DISPLAY_SPREAD_M = 18   # keep child markers visually clear of the gateway
MAX_DISPLAY_DISTANCE_M = 500  # garbage RSSI must not fling nodes off the map
UNKNOWN_SIGNAL_SPREAD_M = 25  # placement radius when a node has no valid RSSI

# Keep the gateway marker visually clear of the operator marker.
GATEWAY_OFFSET_M = 15
GATEWAY_OFFSET_BEARING = 90  # due east of the operator

# Topology watchdog:
# Increased limits to prevent nodes from flashing OFFLINE due to minor packet loss.
STALE_OFFLINE_S = 7.0
WATCHDOG_INTERVAL_S = 1.0   
OFFLINE_REMOVE_S = 10.0

# Consecutive telemetry packets with rssi=None before we treat a node that
# previously had a valid RSSI as "link lost" (force it offline immediately).
NULL_RSSI_STREAK_LIMIT = 3

# painlessMesh gateway debug line announcing a lost mesh connection
DROP_LINE_RE = re.compile(
    r"(?:dropped|lost)\s+connection[^0-9]*(\d{6,})", re.IGNORECASE
)
NEW_CONN_RE = re.compile(r"new\s+connection.*?(\d{6,})", re.IGNORECASE)

def rssi_to_meters(rssi):
    """
    Estimated true distance in meters from RSSI via log-distance path loss.
    Returns None when RSSI is unavailable/invalid. Sanity-capped at
    MAX_DISPLAY_DISTANCE_M so noise spikes don't produce absurd readings.
    """
    try:
        if rssi is None:
            return None
        val = float(rssi)
    except (ValueError, TypeError):
        return None
    if val >= 0:  # nonsensical reading
        return None
    dist = 10 ** ((TX_POWER - val) / (10 * PATH_LOSS_EXPONENT))
    return min(dist, MAX_DISPLAY_DISTANCE_M)

def display_distance(dist_m, node_id):
    """
    Distance used only for map projection: the true estimate with a small
    minimum spread so markers never stack; jittered fallback when unknown.
    """
    if dist_m is None:
        return UNKNOWN_SIGNAL_SPREAD_M + (node_id % 40)
    return max(dist_m, MIN_DISPLAY_SPREAD_M)

def offset_from(lat, lon, meters, bearing):
    """GPS coordinate at `meters` distance from (lat, lon) along `bearing`."""
    destination = geodesic(meters=meters).destination((lat, lon), bearing)
    return destination.latitude, destination.longitude

def calculate_projected_gps(parent_lat, parent_lon, distance_meters, node_id):
    """Calculates a GPS coordinate based on distance and a pseudo-random fixed bearing."""
    bearing = (node_id % 360) 
    origin = (parent_lat, parent_lon)
    destination = geodesic(meters=distance_meters).destination(origin, bearing)
    return destination.latitude, destination.longitude


class Bridge:
    def __init__(self, serial_port: str, baud: int, ws_host: str, ws_port: int):
        self.serial_port = serial_port
        self.baud = baud
        self.ws_host = ws_host
        self.ws_port = ws_port
        self.clients = set()
        
        # Initialize System State
        self.mesh_graph = nx.Graph()
        self.mesh_graph.add_node(GATEWAY_ID, status="online")
        self.node_locations = {GATEWAY_ID: (GATEWAY_LAT, GATEWAY_LON)}
        
        # Operator GPS
        self.operator_gps = None  # (lat, lon) from browser geolocation
        
        # Per-node signal data: node_id -> {rssi, distance_m}
        self.node_signal = {}
        
        # Watchdog bookkeeping
        self.node_last_seen = {}
        self.node_offline_since = {}
        self.dropped_ids = set()
        self.null_rssi_streak: dict[int, int] = {}

    def calculate_network_strength(self):
        """Calculates total network strength as a percentage (0-100%)."""
        valid_rssis = []
        for n_id, sig in self.node_signal.items():
            if n_id != GATEWAY_ID and self.mesh_graph.nodes.get(n_id, {}).get("status") == "online":
                r = sig.get("rssi")
                if r is not None:
                    valid_rssis.append(r)
        
        if not valid_rssis:
            return 100.0  # Default if no active edge nodes
            
        # Map -90 dBm (0%) to -40 dBm (100%)
        total_pct = 0
        for r in valid_rssis:
            pct = (r + 90) * 2.0
            pct = max(0.0, min(100.0, pct))
            total_pct += pct
            
        return total_pct / len(valid_rssis)

    # ------------------------------------------------------------------ #
    # WebSocket side
    # ------------------------------------------------------------------ #
    async def handle_client(self, websocket):
        self.clients.add(websocket)
        peer = websocket.remote_address
        log.info("Dashboard connected: %s (total clients: %d)", peer, len(self.clients))
        await self.broadcast_topology()
        try:
            async for raw_msg in websocket:
                try:
                    msg = json.loads(raw_msg)
                    if msg.get("type") == "OPERATOR_GPS":
                        lat = msg["lat"]
                        lon = msg["lon"]
                        old_gps = self.operator_gps
                        self.operator_gps = (lat, lon)
                        
                        gw_lat, gw_lon = offset_from(
                            lat, lon, GATEWAY_OFFSET_M, GATEWAY_OFFSET_BEARING
                        )
                        self.node_locations[GATEWAY_ID] = (gw_lat, gw_lon)
                        
                        for n_id in list(self.node_locations.keys()):
                            if n_id != GATEWAY_ID:
                                sig = self.node_signal.get(n_id, {})
                                proj = display_distance(sig.get("distance_m"), n_id)
                                self.node_locations[n_id] = calculate_projected_gps(gw_lat, gw_lon, proj, n_id)
                        
                        if old_gps is None:
                            log.info("[BRIDGE] Operator GPS received: %.4f, %.4f — gateway re-anchored", lat, lon)
                        
                        await self.broadcast_topology()
                except (json.JSONDecodeError, KeyError):
                    pass
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            self.clients.discard(websocket)
            log.info("Dashboard disconnected: %s (total clients: %d)", peer, len(self.clients))

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

    async def broadcast_topology(self):
        """Generates the mapped graph state and sends it to the frontend."""
        if not self.clients:
            return
            
        nodes = []
        links = []
        
        for node in self.mesh_graph.nodes(data=True):
            n_id = node[0]
            lat, lon = self.node_locations.get(n_id, (0,0))
            sig = self.node_signal.get(n_id, {})
            nodes.append({
                "id": n_id,
                "status": node[1].get("status", "online"),
                "lat": lat,
                "lon": lon,
                "rssi": sig.get("rssi", None),
                "distance_m": sig.get("distance_m", None),
            })
            
        for edge in self.mesh_graph.edges():
            child_id = edge[1] if edge[0] == GATEWAY_ID else edge[0]
            sig = self.node_signal.get(child_id, {})
            links.append({
                "source": edge[0],
                "target": edge[1],
                "rssi": sig.get("rssi", None),
                "distance_m": sig.get("distance_m", None),
            })
            
        # We append the backend network strength to the payload so the UI can sync if desired
        net_strength = self.calculate_network_strength()
        payload = json.dumps({
            "type": "TOPOLOGY", 
            "network_strength": round(net_strength, 1),
            "nodes": nodes, 
            "links": links
        })
        await self.broadcast(payload)

    # ------------------------------------------------------------------ #
    # Serial side
    # ------------------------------------------------------------------ #
    def _open_serial(self) -> serial.Serial:
        ser = serial.Serial(self.serial_port, self.baud, timeout=1)
        ser.dtr = False   
        ser.rts = True    
        time.sleep(0.2)
        ser.rts = False   
        ser.reset_input_buffer()
        return ser

    def _read_line(self, ser: serial.Serial) -> bytes:
        return ser.readline()

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
                log.info("Serial port %s open. Streaming mesh data.", self.serial_port)

                last_data = asyncio.get_event_loop().time()
                warned_silent = False

                while True:
                    raw = await loop.run_in_executor(None, self._read_line, ser)
                    if not raw:
                        now = asyncio.get_event_loop().time()
                        silent_for = now - last_data
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

                    last_data = asyncio.get_event_loop().time()
                    warned_silent = False

                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue

                    payload = self._extract_json(line)
                    if payload is None:
                        topology_changed = False

                        drop = DROP_LINE_RE.search(line)
                        if drop:
                            lost_id = int(drop.group(1))
                            if lost_id != GATEWAY_ID:
                                self.dropped_ids.add(lost_id)
                                topology_changed = self._mark_offline(lost_id) or topology_changed
                                log.warning("[WATCHDOG] Gateway reports lost connection to %s — marked OFFLINE.", lost_id)

                        newc = NEW_CONN_RE.search(line)
                        if newc:
                            joined_id = int(newc.group(1))
                            if joined_id in self.dropped_ids:
                                self.dropped_ids.discard(joined_id)
                                self.null_rssi_streak.pop(joined_id, None)
                                log.info("[WATCHDOG] Gateway reports new connection to %s — telemetry re-armed.", joined_id)

                        if topology_changed:
                            await self.broadcast_topology()
                        
                        log.info("Serial (non-JSON, ignored): %s", line[:200])
                        continue

                    # NetworkX Processing Logic
                    try:
                        data = json.loads(payload)
                        
                        if "node_id" in data and "status" in data:
                            node_id = data["node_id"]
                            status = data["status"]
                            rssi = data.get("parent_rssi", "disconnected")

                            try:
                                rssi_numeric = float(rssi)
                                if rssi_numeric >= 0:
                                    rssi_numeric = None
                            except (ValueError, TypeError):
                                rssi_numeric = None

                            if node_id != GATEWAY_ID:
                                # Case 1: Node has NO valid parent link
                                if rssi_numeric is None and status != "JAMMED":
                                    prev_status = (
                                        self.mesh_graph.nodes[node_id].get("status")
                                        if node_id in self.mesh_graph
                                        else None
                                    )
                                    
                                    topology_changed = False
                                    if self.mesh_graph.has_edge(GATEWAY_ID, node_id):
                                        self.mesh_graph.remove_edge(GATEWAY_ID, node_id)
                                        topology_changed = True
                                        
                                    if node_id not in self.mesh_graph:
                                        self.mesh_graph.add_node(node_id, status="OFFLINE")
                                        self.node_offline_since.setdefault(node_id, time.monotonic())
                                        topology_changed = True
                                    else:
                                        if self._mark_offline(node_id):
                                            topology_changed = True
                                            
                                    self.dropped_ids.add(node_id)
                                    self.node_signal[node_id] = {
                                        "rssi": None,
                                        "distance_m": None,
                                    }
                                    
                                    if prev_status != "OFFLINE":
                                        log.warning(
                                            "[WATCHDOG] Node %s has no parent link (parent_rssi=%s) "
                                            "— marked OFFLINE immediately (topology fractured).",
                                            node_id, rssi,
                                        )
                                        
                                    if topology_changed:
                                        await self.broadcast_topology()
                                    continue

                                # Case 2: Node is validly connected or JAMMED
                                prev_status = (
                                    self.mesh_graph.nodes[node_id].get("status")
                                    if node_id in self.mesh_graph
                                    else None
                                )
                                self.mesh_graph.add_node(node_id, status=status)
                                self.node_last_seen[node_id] = time.monotonic()
                                self.node_offline_since.pop(node_id, None)
                                self.dropped_ids.discard(node_id)
                                self.null_rssi_streak[node_id] = 0

                                if prev_status == "OFFLINE" and status == "online":
                                    log.info(
                                        "[WATCHDOG] Node %s telemetry resumed with RSSI %s — "
                                        "back ONLINE, topology restored.",
                                        node_id, rssi_numeric,
                                    )

                                # -------------------------------------------------------------
                                # --- 1. Update Current Signal State ---
                                dist = rssi_to_meters(rssi_numeric)
                                self.node_signal[node_id] = {
                                    "rssi": rssi_numeric,
                                    "distance_m": round(dist, 2) if dist is not None else None,
                                }

                                # --- 2. Calculate Total Network Strength ---
                                net_strength = self.calculate_network_strength()

                                # --- 3. DYNAMIC REROUTING LOGIC (Network Strength Based) ---
                                parent_id = GATEWAY_ID
                                
                                # If the TOTAL network strength is below 80%, try to find a better relay
                                if rssi_numeric is not None and net_strength < 80.0:
                                    best_relay = None
                                    best_rssi = -999
                                    
                                    for n, n_data in self.mesh_graph.nodes(data=True):
                                        if n != node_id and n != GATEWAY_ID and n_data.get("status") == "online":
                                            n_sig = self.node_signal.get(n, {})
                                            n_rssi = n_sig.get("rssi")
                                            
                                            # Relay must simply have a connection at least 5 dBm better than the struggling node
                                            if n_rssi is not None and n_rssi > (rssi_numeric + 5) and n_rssi > best_rssi:
                                                best_relay = n
                                                best_rssi = n_rssi
                                    
                                    if best_relay:
                                        parent_id = best_relay
                                        log.info("[NETWORKX] REROUTING: Network Strength %.1f%% < 80%%. Node %s (%sdBm) relaying via %s (%sdBm).", 
                                                 net_strength, node_id, rssi_numeric, best_relay, best_rssi)

                                # --- 4. Apply the calculated topology ---
                                # Clear existing links to maintain a clean tree topology without loops
                                edges_to_remove = list(self.mesh_graph.edges(node_id))
                                self.mesh_graph.remove_edges_from(edges_to_remove)
                                
                                # Connect to the calculated parent (Gateway or Relay)
                                self.mesh_graph.add_edge(parent_id, node_id)

                                log.info(
                                    "MESH TLM %s status=%s rssi=%s dist=%s (Parent: %s)",
                                    node_id, status, rssi_numeric,
                                    self.node_signal[node_id]["distance_m"], parent_id
                                )

                                # Project the GPS coordinate relative to the active parent, not the Gateway
                                p_lat, p_lon = self.node_locations.get(parent_id, (GATEWAY_LAT, GATEWAY_LON))
                                proj = display_distance(
                                    self.node_signal[node_id]["distance_m"], node_id
                                )
                                self.node_locations[node_id] = calculate_projected_gps(p_lat, p_lon, proj, node_id)
                                # -------------------------------------------------------------
                                
                            else:
                                # Gateway local node
                                self.mesh_graph.add_node(GATEWAY_ID, status="online")
                            
                            if status == "JAMMED":
                                log.warning(f"[NETWORKX] TACTICAL ALERT: Node {node_id} jammed.")
                                mid_lat = (self.node_locations[parent_id][0] + self.node_locations[node_id][0]) / 2.0
                                mid_lon = (self.node_locations[parent_id][1] + self.node_locations[node_id][1]) / 2.0
                                
                                dispatch_payload = json.dumps({
                                    "type": "UAV_DISPATCH",
                                    "target_node": node_id,
                                    "midpoint": [mid_lat, mid_lon]
                                })
                                await self.broadcast(dispatch_payload)
                                log.info(f"[NETWORKX] Dispatching UAV to {mid_lat}, {mid_lon}...")
                            
                            await self.broadcast_topology()
                            
                    except Exception as e:
                        log.error("Error mapping payload: %s", e)

            except serial.SerialException as exc:
                log.error("Serial error (%s). Retrying in %.0fs...", exc, SERIAL_RETRY_DELAY)
            except OSError as exc:
                log.error("OS error on serial port (%s). Retrying in %.0fs...", exc, SERIAL_RETRY_DELAY)
            finally:
                if ser is not None:
                    try:
                        ser.close()
                    except Exception:
                        pass

            await asyncio.sleep(SERIAL_RETRY_DELAY)

    def _mark_offline(self, n_id: int) -> bool:
        if n_id == GATEWAY_ID or n_id not in self.mesh_graph:
            return False
        attrs = self.mesh_graph.nodes[n_id]
        changed = False
        if attrs.get("status") != "OFFLINE":
            attrs["status"] = "OFFLINE"
            self.node_offline_since.setdefault(n_id, time.monotonic())
            changed = True
        edges = list(self.mesh_graph.edges(n_id))
        if edges:
            self.mesh_graph.remove_edges_from(edges)
            changed = True
        return changed

    def _purge_node(self, n_id: int) -> None:
        if n_id == GATEWAY_ID:
            return
        if n_id in self.mesh_graph:
            self.mesh_graph.remove_node(n_id)
        self.node_locations.pop(n_id, None)
        self.node_signal.pop(n_id, None)
        self.node_last_seen.pop(n_id, None)
        self.node_offline_since.pop(n_id, None)
        self.null_rssi_streak.pop(n_id, None)
        self.dropped_ids.discard(n_id)
        log.info("[WATCHDOG] Node %s purged from topology (marker removed).", n_id)

    async def watchdog(self):
        while True:
            await asyncio.sleep(WATCHDOG_INTERVAL_S)
            now = time.monotonic()
            changed = False
            to_purge = []
            for n_id, last in list(self.node_last_seen.items()):
                if n_id == GATEWAY_ID or n_id not in self.mesh_graph:
                    continue
                attrs = self.mesh_graph.nodes[n_id]
                silent_for = now - last
                if attrs.get("status") == "online" and silent_for > STALE_OFFLINE_S:
                    if self._mark_offline(n_id):
                        changed = True
                        log.warning(
                            "[WATCHDOG] Node %s silent for %.0fs — marked OFFLINE.",
                            n_id, silent_for,
                        )
                offline_since = self.node_offline_since.get(n_id)
                if attrs.get("status") == "OFFLINE" and offline_since is not None:
                    if now - offline_since > OFFLINE_REMOVE_S:
                        to_purge.append(n_id)
            for n_id in to_purge:
                self._purge_node(n_id)
                changed = True
            if changed:
                await self.broadcast_topology()

    async def run(self):
        async with websockets.serve(self.handle_client, self.ws_host, self.ws_port):
            log.info("WebSocket C2 server listening on ws://%s:%d", self.ws_host, self.ws_port)
            await asyncio.gather(self.serial_loop(), self.watchdog())


def main():
    parser = argparse.ArgumentParser(description="AeroMesh serial-to-WebSocket bridge")
    parser.add_argument("--port", default="COM9", help="Serial port (default: COM9)")
    parser.add_argument("--baud", type=int, default=115200, help="Baud rate (default: 115200)")
    parser.add_argument("--ws-host", default="localhost", help="WebSocket bind host (default: localhost)")
    parser.add_argument("--ws-port", type=int, default=8765, help="WebSocket port (default: 8765)")
    args = parser.parse_args()

    bridge = Bridge(args.port, args.baud, args.ws_host, args.ws_port)
    try:
        asyncio.run(bridge.run())
    except KeyboardInterrupt:
        log.info("Bridge stopped by user.")
        sys.exit(0)

if __name__ == "__main__":
    main()