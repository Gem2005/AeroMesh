from __future__ import annotations

import math

import networkx as nx

from sim.constants import (
    GATEWAY_KEY,
    RSSI_SIGMA,
    LINK_RSSI_MIN,
    LINK_MAX_M,
    BASE_NOISE_FLOOR_DBM,
    HOP_BASE_LATENCY_MS,
    HOP_RETRY_LATENCY_MS,
    FRINGE_RSSI_FLOOR_DBM,
    JAMMED_RSSI_DBM,
    JAMMED_SNR_DB,
    UAV_A2A_MAX_M,
    RELAY_LINK_MAX_M,
    STATUS_ONLINE,
    STATUS_DEGRADED,
    STATUS_JAMMED,
    STATUS_OFFLINE,
)
from sim.geo import haversine_m, destination
from sim.radio import hop_success, link_cost, ground_rssi_nominal, air_rssi_nominal
from sim.nodes import MeshNode


class TopologyMixin:
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

    def node_can_link(self, n: MeshNode) -> bool:
        if n.type == "hardware" and not n.is_gateway and n.reported_status in (STATUS_OFFLINE, STATUS_JAMMED):
            return False
        return n.rf_state != STATUS_JAMMED

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
