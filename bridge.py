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

TX_POWER = -50  # dBm at 1 meter
PATH_LOSS_EXPONENT = 2.5 # Environmental factor

def rssi_to_meters(rssi):
    """Converts RSSI to distance in meters using Free Space Path Loss."""
    try:
        val = float(rssi)
        if val >= 0:
            return 0
        return 10 ** ((TX_POWER - val) / (10 * PATH_LOSS_EXPONENT))
    except (ValueError, TypeError):
        return 0

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

    # ------------------------------------------------------------------ #
    # WebSocket side
    # ------------------------------------------------------------------ #
    async def handle_client(self, websocket):
        self.clients.add(websocket)
        peer = websocket.remote_address
        log.info("Dashboard connected: %s (total clients: %d)", peer, len(self.clients))
        try:
            async for _ in websocket:
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
            nodes.append({
                "id": n_id,
                "status": node[1].get("status", "online"),
                "lat": lat,
                "lon": lon
            })
            
        for edge in self.mesh_graph.edges():
            links.append({"source": edge[0], "target": edge[1]})
            
        payload = json.dumps({"type": "TOPOLOGY", "nodes": nodes, "links": links})
        await self.broadcast(payload)

    # ------------------------------------------------------------------ #
    # Serial side
    # ------------------------------------------------------------------ #
    def _open_serial(self) -> serial.Serial:
        ser = serial.Serial(self.serial_port, self.baud, timeout=1)
        ser.dtr = False   # GPIO0 high -> normal boot
        ser.rts = True    # EN low -> hold chip in reset
        time.sleep(0.2)
        ser.rts = False   # release reset -> chip boots
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
                                "No serial data for %.0fs -- cycling %s to "
                                "auto-reset the gateway ESP32.",
                                silent_for, self.serial_port,
                            )
                            break 
                        if not warned_silent and silent_for > SILENCE_WARN_S:
                            log.warning(
                                "No serial data for %.0fs. Is the gateway sketch "
                                "printing? (auto-reset at %.0fs)",
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
                        log.info("Serial (non-JSON, ignored): %s", line[:200])
                        continue

                    # NetworkX Processing Logic
                    try:
                        data = json.loads(payload)
                        
                        if "node_id" in data and "status" in data:
                            node_id = data["node_id"]
                            status = data["status"]
                            rssi = data.get("parent_rssi", "disconnected")
                            
                            self.mesh_graph.add_node(node_id, status=status)
                            
                            parent_id = GATEWAY_ID 
                            if node_id != GATEWAY_ID:
                                self.mesh_graph.add_edge(parent_id, node_id)
                                
                                dist = rssi_to_meters(rssi)
                                p_lat, p_lon = self.node_locations.get(parent_id, (GATEWAY_LAT, GATEWAY_LON))
                                self.node_locations[node_id] = calculate_projected_gps(p_lat, p_lon, dist, node_id)
                            
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
                            
                            # Broadcast the calculated mapping to the UI
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

    async def run(self):
        async with websockets.serve(self.handle_client, self.ws_host, self.ws_port):
            log.info("WebSocket C2 server listening on ws://%s:%d", self.ws_host, self.ws_port)
            await self.serial_loop() 


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