from __future__ import annotations

import asyncio
import json
import logging
import time

import serial  # pyserial

from sim.constants import (
    SERIAL_RETRY_DELAY,
    SILENCE_WARN_S,
    SILENCE_RESET_S,
    SERIAL_WRITE_SPACING_S,
    SERIAL_WRITE_QUEUE_MAX,
    SERIAL_MESSAGE_MAX_LEN,
    GATEWAY_ID,
    HW_DISTANCE_SMOOTHING,
    STALE_OFFLINE_S,
    WATCHDOG_INTERVAL_S,
    OFFLINE_REMOVE_S,
    NULL_RSSI_STREAK_LIMIT,
    ORPHAN_HUNT_JITTER_MAX_S,
    STATUS_ONLINE,
    STATUS_JAMMED,
    STATUS_OFFLINE,
    DROP_LINE_RE,
    NEW_CONN_RE,
    FIRMWARE_ACK_PREFIXES,
)
from sim.geo import utc_iso
from sim.radio import rssi_to_meters, display_distance
from sim.nodes import MeshNode

log = logging.getLogger("bridge")


class SerialMixin:
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
        # ── P2P packet detection ──────────────────────────────────────────
        is_p2p = bool(data.get("p2p")) or ("p2p_msg" in data)
        if is_p2p:
            sender = str(data.get("sender", f"HW-{node_key}"))
            target_id = str(data.get("target", "ALL"))
            p2p_text = str(data.get("p2p_msg") or data.get("reply", ""))
            self.log_event("P2P", "ok", f"P2P relay from HW {node_key} ({sender}) -> {target_id}: {p2p_text}")
            self.route_p2p_message(node_key, sender, target_id, p2p_text, origin="hardware")
            return
        # ── Standard hub reply ───────────────────────────────────────────
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
            hunt_jitter_s=self.rng.uniform(0.0, ORPHAN_HUNT_JITTER_MAX_S),
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
