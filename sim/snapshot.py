from __future__ import annotations

import asyncio
import json
import logging
import time

import websockets

from sim.constants import (
    GATEWAY_KEY,
    GATEWAY_OFFSET_M,
    GATEWAY_OFFSET_BEARING,
    LINK_RSSI_MIN,
    LINK_MAX_M,
    RSSI_EXCELLENT_DBM,
    RSSI_GOOD_DBM,
    SWARM_MIN,
    SWARM_MAX,
    ORBIT_MAX_RINGS,
    ORBIT_RADIUS_MIN_M,
    ORBIT_RADIUS_MAX_M,
    ORBIT_MIN_RING_GAP_M,
    ORBIT_SPEED_MIN_MPS,
    ORBIT_SPEED_MAX_MPS,
    NODE_TRANSIT_SPEED_MPS,
    PLACEMENT_ORBIT,
    PLACEMENT_POLYGON,
    UAV_ID_PREFIX,
    ACS_IDLE,
    ACS_CONFIRMING,
)
from sim.geo import destination, channel_freq_mhz, utc_iso
from sim.radio import link_quality, _round
from sim.nodes import MeshNode

log = logging.getLogger("bridge")


class SnapshotMixin:
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
            "p2p_messages": self.p2p_history[-20:],
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

        elif mtype == "SEND_P2P":
            source_id = str(msg.get("source_id", GATEWAY_KEY))
            sender = str(msg.get("sender", source_id))
            target_id = str(msg.get("target_id", "ALL"))
            text = self._sanitize_serial_text(msg.get("message", ""))
            if text:
                self.route_p2p_message(source_id, sender, target_id, text, origin="dashboard")
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
