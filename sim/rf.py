from __future__ import annotations

import asyncio
import math
import time

from sim.constants import (
    LINK_MAX_M,
    WIFI_CHANNELS,
    BASE_NOISE_FLOOR_DBM,
    ZONE_DEFAULT_KILL_M,
    ZONE_DEFAULT_BLEED_M,
    ZONE_MAX_RADIUS_M,
    FRINGE_MAX_PENALTY_DB,
    INTERFERED_NOISE_DBM,
    ACS_TRIGGER_COUNT,
    ACS_CONFIRM_S,
    ACS_NON_OVERLAPPING,
    ACS_MIN_SEPARATION,
    ACS_CLEAN_MARGIN_DB,
    ACS_GATEWAY_EMERGENCY_CONFIRM_S,
    RF_ALERT_MIN_INTERVAL_S,
    ZONE_MODE_SPOT,
    ZONE_MODE_BARRAGE,
    ZONE_MODE_SWEEPER,
    ZONE_MODES,
    SWEEPER_INTERVAL_S,
    SWEEPER_CYCLE,
    SHEPHERD_BURNTHROUGH_SLANT_M,
    ORPHAN_BEACON_HUNT_S,
    MISSING_EDGE_WEIGHT_DB,
    MAST_HORIZONTAL_RADIUS_M,
    MAST_MIN_ALT_M,
    MAST_BURNTHROUGH_PENALTY_DB,
    BARRAGE_VERTICAL_MIN_ALT_M,
    BARRAGE_VERTICAL_BONUS_DB,
    BARRAGE_VERTICAL_MIN_ELEV_DEG,
    RELAY_LINK_MAX_M,
    TASK_SHEPHERD,
    TASK_MAST,
    ACS_IDLE,
    ACS_CONFIRMING,
    ACS_SWITCHING,
    ACS_SCANNING,
    STATUS_DEGRADED,
    STATUS_JAMMED,
    STATUS_OFFLINE,
)
from sim.geo import haversine_m, channel_freq_mhz
from sim.nodes import MeshNode, InterferenceZone


class RfMixin:
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
