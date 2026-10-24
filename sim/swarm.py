from __future__ import annotations

import math
import random

from sim.constants import (
    SWARM_MIN,
    SWARM_MAX,
    ORBIT_RADII_DEFAULT_M,
    ORBIT_MAX_RINGS,
    ORBIT_RADIUS_MIN_M,
    ORBIT_RADIUS_MAX_M,
    ORBIT_MIN_RING_GAP_M,
    ORBIT_TANGENTIAL_SPEED_MPS,
    ORBIT_SPEED_MIN_MPS,
    ORBIT_SPEED_MAX_MPS,
    ORBIT_RADIAL_AMPLITUDE_M,
    ORBIT_RADIAL_FREQ,
    NODE_TRANSIT_SPEED_MPS,
    POLYGON_GRID_SHRINK,
    POLYGON_GRID_MAX_ITER,
    VIRTUAL_BATTERY_DRAIN_MV_PER_S,
    VIRTUAL_BATTERY_FLOOR_MV,
    PLACEMENT_ORBIT,
    PLACEMENT_POLYGON,
    HOLD_RELEASE_TICKS,
    HOLD_LOST_LINK,
    HOLD_RECONNECTING,
    ORPHAN_HUNT_JITTER_MAX_S,
    STATUS_JAMMED,
    STATUS_OFFLINE,
)
from sim.geo import haversine_m, offset_m, to_local_m, move_toward, point_in_polygon, polygon_area
from sim.nodes import MeshNode, virtual_id

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
