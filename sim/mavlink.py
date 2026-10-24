from __future__ import annotations

import logging
import math
import socket
import struct
import time
from typing import TYPE_CHECKING

from sim.constants import (
    UAV_ARRIVAL_RADIUS_M,
    MODE_AUTO,
    MODE_GUIDED,
    MODE_LOITER,
    MAVLINK_HOST,
    MAVLINK_PORT,
    MAVLINK_COMPID,
    MAV_AUTOPILOT_ARDUPILOTMEGA,
    MAV_MODE_FLAG_CUSTOM_MODE_ENABLED,
    MAV_MODE_FLAG_GUIDED_ENABLED,
    MAV_MODE_FLAG_SAFETY_ARMED,
    MAV_STATE_STANDBY,
    MAV_STATE_ACTIVE,
    MAV_TYPE_GCS,
    MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
    MAV_PARAM_TYPE_REAL32,
    MAV_SENSORS_SIMULATED,
    GCS_HEARTBEAT_TIMEOUT_S,
    MAV_CMD_NAV_WAYPOINT,
    MAV_CMD_NAV_RETURN_TO_LAUNCH,
    MAV_CMD_NAV_LAND,
    MAV_CMD_NAV_TAKEOFF,
    MAV_CMD_DO_SET_MODE,
    MAV_CMD_DO_CHANGE_SPEED,
    MAV_CMD_DO_REPOSITION,
    MAV_CMD_COMPONENT_ARM_DISARM,
    MAV_CMD_ACK_ONLY,
    MAV_RESULT_ACCEPTED,
    MAV_RESULT_DENIED,
    MAV_RESULT_UNSUPPORTED,
    MAV_RESULT_FAILED,
    MAV_MISSION_ACCEPTED,
    MAV_MISSION_ERROR,
    MAV_MISSION_DENIED,
    MAV_SEVERITY,
    MAVLINK_MSG_HEARTBEAT,
    MAVLINK_MSG_SYS_STATUS,
    MAVLINK_MSG_PARAM_VALUE,
    MAVLINK_MSG_GLOBAL_POSITION_INT,
    MAVLINK_MSG_MISSION_COUNT,
    MAVLINK_MSG_MISSION_ACK,
    MAVLINK_MSG_MISSION_ITEM_INT,
    MAVLINK_MSG_VFR_HUD,
    MAVLINK_MSG_COMMAND_ACK,
    MAVLINK_MSG_STATUSTEXT,
    MAVLINK_RX_SPECS,
)
from sim.radio import _round

if TYPE_CHECKING:
    from sim.fleet import FleetManager, UavAgent

log = logging.getLogger("bridge")

# ------------------------------------------------------------------ #
# MAVLink v2 ground-station link (UDP, bidirectional)
# ------------------------------------------------------------------ #
def x25_crc(data: bytes, crc: int = 0xFFFF) -> int:
    """CRC-16/MCRF4XX (X.25) as used by MAVLink."""
    for byte in data:
        tmp = byte ^ (crc & 0xFF)
        tmp = (tmp ^ (tmp << 4)) & 0xFF
        crc = ((crc >> 8) ^ (tmp << 8) ^ (tmp << 3) ^ (tmp >> 4)) & 0xFFFF
    return crc


def parse_mavlink_frames(data: bytes) -> tuple[list[tuple[int, int, int, bytes]], int, int]:
    """
    Splits a datagram into MAVLink v1/v2 frames. Returns
    ([(msgid, sysid, compid, payload zero-padded to full length)], crc_errors, ignored).
    Frames whose msg id is not in MAVLINK_RX_SPECS cannot be CRC-checked and are ignored.
    """
    frames: list[tuple[int, int, int, bytes]] = []
    crc_errors = ignored = 0
    i, n = 0, len(data)
    while i < n:
        stx = data[i]
        if stx == 0xFD:
            if i + 10 > n:
                break
            ln, incompat = data[i + 1], data[i + 2]
            end = i + 10 + ln + 2 + (13 if incompat & 0x01 else 0)
            if end > n:
                break
            sysid, compid = data[i + 5], data[i + 6]
            msgid = int.from_bytes(data[i + 7:i + 10], "little")
            payload = data[i + 10:i + 10 + ln]
            crc_input = data[i + 1:i + 10 + ln]
            crc = struct.unpack_from("<H", data, i + 10 + ln)[0]
        elif stx == 0xFE:
            if i + 6 > n:
                break
            ln = data[i + 1]
            end = i + 6 + ln + 2
            if end > n:
                break
            sysid, compid, msgid = data[i + 3], data[i + 4], data[i + 5]
            payload = data[i + 6:i + 6 + ln]
            crc_input = data[i + 1:i + 6 + ln]
            crc = struct.unpack_from("<H", data, i + 6 + ln)[0]
        else:
            i += 1
            continue
        spec = MAVLINK_RX_SPECS.get(msgid)
        if spec is None:
            ignored += 1
        elif x25_crc(bytes([spec[0]]), x25_crc(crc_input)) != crc:
            crc_errors += 1
        else:
            frames.append((msgid, sysid, compid, payload.ljust(spec[1], b"\x00")))
        i = end
    return frames, crc_errors, ignored


class MavlinkGcsLink:
    """
    Two-way MAVLink v2 link between the relay fleet and a ground station
    (QGroundControl / Mission Planner on udp:127.0.0.1:14550). Every UAV is a
    separate vehicle (own system id and sequence counter). Inbound
    COMMAND_LONG/INT, SET_MODE, guided MISSION_ITEM_INT and
    SET_POSITION_TARGET_GLOBAL_INT are routed through FleetManager.command,
    so GCS and dashboard commands share the same validation and logging.

    Uses one non-blocking UDP socket bound to an ephemeral port; the GCS
    replies to that port. poll() drains it without blocking the event loop.
    """

    def __init__(self, host: str = MAVLINK_HOST, port: int = MAVLINK_PORT):
        self.addr = (host, port)
        self.seq: dict[int, int] = {}
        self.packet_count = 0
        self.rx_count = 0
        self.cmd_count = 0
        self.crc_errors = 0
        self.ignored = 0
        self.gcs_addr: tuple[str, int] | None = None
        self.gcs_sysid: int | None = None
        self.gcs_heartbeat_at: float | None = None
        self.last_command: str | None = None
        self.last_command_at: float | None = None
        self.boot = time.monotonic()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setblocking(False)
        if hasattr(socket, "SIO_UDP_CONNRESET"):
            # Windows: an ICMP port-unreachable (no GCS listening) would otherwise
            # surface as ConnectionResetError on every recvfrom.
            self.sock.ioctl(socket.SIO_UDP_CONNRESET, False)
        self.sock.bind(("0.0.0.0", 0))
        self.local_port = self.sock.getsockname()[1]
        self.backend = "stdlib"
        log.info(
            "MAVLink v2 GCS link -> udp:%s:%d (replies on local port %d)", host, port, self.local_port
        )

    @property
    def gcs_connected(self) -> bool:
        return self.gcs_heartbeat_at is not None and time.monotonic() - self.gcs_heartbeat_at <= GCS_HEARTBEAT_TIMEOUT_S

    def status_dict(self) -> dict:
        now = time.monotonic()
        return {
            "host": self.addr[0],
            "port": self.addr[1],
            "local_port": self.local_port,
            "backend": self.backend,
            "packet_count": self.packet_count,
            "rx_count": self.rx_count,
            "cmd_count": self.cmd_count,
            "rx_errors": self.crc_errors,
            "connected": self.gcs_connected,
            "gcs_addr": f"{self.gcs_addr[0]}:{self.gcs_addr[1]}" if self.gcs_addr else None,
            "gcs_sysid": self.gcs_sysid,
            "heartbeat_age_s": _round(now - self.gcs_heartbeat_at, 1) if self.gcs_heartbeat_at else None,
            "last_command": self.last_command,
            "last_command_age_s": _round(now - self.last_command_at, 1) if self.last_command_at else None,
        }

    def frame(self, msg: tuple[int, int], payload: bytes, sysid: int, compid: int = MAVLINK_COMPID) -> bytes:
        msgid, crc_extra = msg
        body = payload.rstrip(b"\x00") or b"\x00"  # MAVLink v2 trailing-zero truncation
        seq = self.seq.get(sysid, 0)
        header = struct.pack("<BBBBBBB", 0xFD, len(body), 0, 0, seq, sysid, compid) + msgid.to_bytes(3, "little")
        crc = x25_crc(header[1:] + body)
        crc = x25_crc(bytes([crc_extra]), crc)
        self.seq[sysid] = (seq + 1) & 0xFF
        return header + body + struct.pack("<H", crc)

    def _send_raw(self, msg: tuple[int, int], payload: bytes, sysid: int) -> None:
        packet = self.frame(msg, payload, sysid)
        targets = {self.addr}
        if self.gcs_addr is not None and self.gcs_connected:
            targets.add(self.gcs_addr)  # a GCS on another host that sent us heartbeats
        for addr in targets:
            try:
                self.sock.sendto(packet, addr)
                self.packet_count += 1
            except OSError as exc:
                log.debug("MAVLink UDP send to %s failed: %s", addr, exc)

    def time_boot_ms(self) -> int:
        return int((time.monotonic() - self.boot) * 1000) & 0xFFFFFFFF

    def send_heartbeat(self, u: "UavAgent") -> None:
        base_mode = MAV_MODE_FLAG_CUSTOM_MODE_ENABLED
        if u.mode in (MODE_GUIDED, MODE_AUTO):
            base_mode |= MAV_MODE_FLAG_GUIDED_ENABLED
        if u.armed:
            base_mode |= MAV_MODE_FLAG_SAFETY_ARMED
        state = MAV_STATE_ACTIVE if u.airborne else MAV_STATE_STANDBY
        payload = struct.pack(
            "<IBBBBB", u.utype.custom_modes[u.mode], u.utype.mav_type, MAV_AUTOPILOT_ARDUPILOTMEGA, base_mode, state, 3
        )
        self._send_raw(MAVLINK_MSG_HEARTBEAT, payload, u.sysid)

    def send_global_position(self, u: "UavAgent") -> None:
        b = math.radians(u.bearing_deg)
        vx = int(max(-32768, min(32767, round(u.ground_speed_mps * math.cos(b) * 100))))
        vy = int(max(-32768, min(32767, round(u.ground_speed_mps * math.sin(b) * 100))))
        vz = int(max(-32768, min(32767, round(-u.climb_mps * 100))))
        hdg = int(round(u.bearing_deg * 100)) % 36000
        lat_e7, lng_e7 = int(round(u.node.lat * 1e7)), int(round(u.node.lng * 1e7))
        alt_mm = int(round(u.node.alt_m * 1000))
        payload = struct.pack("<IiiiihhhH", self.time_boot_ms(), lat_e7, lng_e7, alt_mm, alt_mm, vx, vy, vz, hdg)
        self._send_raw(MAVLINK_MSG_GLOBAL_POSITION_INT, payload, u.sysid)

    def send_waypoint(self, u: "UavAgent", lat: float, lng: float, alt_m: float) -> None:
        """MISSION_ITEM_INT carrying MAV_CMD_NAV_WAYPOINT (current=2: guided 'fly-to')."""
        lat_e7, lng_e7 = int(round(lat * 1e7)), int(round(lng * 1e7))
        hold_s, accept_m, pass_m, yaw = 0.0, UAV_ARRIVAL_RADIUS_M, 0.0, float("nan")
        payload = struct.pack(
            "<ffffiifHHBBBBBB",
            hold_s, accept_m, pass_m, yaw, lat_e7, lng_e7, alt_m,
            u.waypoint_seq & 0xFFFF, MAV_CMD_NAV_WAYPOINT, u.sysid, 1, MAV_FRAME_GLOBAL_RELATIVE_ALT_INT, 2, 1, 0,
        )
        self._send_raw(MAVLINK_MSG_MISSION_ITEM_INT, payload, u.sysid)

    def send_sys_status(self, u: "UavAgent") -> None:
        voltage_mv = int(round((14.0 + 2.8 * u.battery_pct / 100.0) * 1000))  # 4S LiPo
        current_ca = 1500 if u.airborne else 0
        payload = struct.pack(
            "<IIIHHhHHHHHHb",
            MAV_SENSORS_SIMULATED, MAV_SENSORS_SIMULATED, MAV_SENSORS_SIMULATED,
            300 if u.airborne else 100, voltage_mv, current_ca, 0, 0, 0, 0, 0, 0,
            int(round(max(0.0, min(100.0, u.battery_pct)))),
        )
        self._send_raw(MAVLINK_MSG_SYS_STATUS, payload, u.sysid)

    def send_vfr_hud(self, u: "UavAgent") -> None:
        throttle = 50 if u.airborne else 0
        payload = struct.pack(
            "<ffffhH", u.ground_speed_mps, u.ground_speed_mps, u.node.alt_m, u.climb_mps,
            int(round(u.bearing_deg)) % 360, throttle,
        )
        self._send_raw(MAVLINK_MSG_VFR_HUD, payload, u.sysid)

    def send_statustext(self, u: "UavAgent", severity: str, text: str) -> None:
        if u.sysid is None:
            return
        body = f"{u.id}: {text}".encode("ascii", "replace")[:50]
        payload = struct.pack("<B50s", MAV_SEVERITY.get(severity, 6), body)
        self._send_raw(MAVLINK_MSG_STATUSTEXT, payload, u.sysid)

    def send_command_ack(self, sysid: int, command: int, result: int, target_sys: int, target_comp: int) -> None:
        payload = struct.pack("<HBBiBB", command, result, 0, 0, target_sys, target_comp)
        self._send_raw(MAVLINK_MSG_COMMAND_ACK, payload, sysid)

    def send_mission_ack(self, sysid: int, result: int, target_sys: int, target_comp: int) -> None:
        self._send_raw(MAVLINK_MSG_MISSION_ACK, struct.pack("<BBBB", target_sys, target_comp, result, 0), sysid)

    def send_mission_count(self, sysid: int, target_sys: int, target_comp: int) -> None:
        """Relay UAVs fly allocator-generated guided targets, not uploaded missions."""
        self._send_raw(MAVLINK_MSG_MISSION_COUNT, struct.pack("<HBBB", 0, target_sys, target_comp, 0), sysid)

    def send_param_value(self, u: "UavAgent") -> None:
        payload = struct.pack("<fHH16sB", float(u.sysid), 1, 0, b"SYSID_THISMAV", MAV_PARAM_TYPE_REAL32)
        self._send_raw(MAVLINK_MSG_PARAM_VALUE, payload, u.sysid)

    # ---------------- inbound ---------------- #
    def poll(self, fleet: "FleetManager") -> int:
        """Drains the socket and executes GCS messages. Returns the number of state-changing commands."""
        executed = 0
        while True:
            try:
                data, addr = self.sock.recvfrom(4096)
            except (BlockingIOError, InterruptedError):
                break
            except ConnectionResetError:
                continue
            except OSError as exc:
                log.debug("MAVLink UDP receive failed: %s", exc)
                break
            frames, crc_errors, ignored = parse_mavlink_frames(data)
            self.crc_errors += crc_errors
            self.ignored += ignored
            for msgid, sysid, compid, payload in frames:
                if fleet.is_own_sysid(sysid) and compid == MAVLINK_COMPID:
                    continue  # our own telemetry looped back
                self.rx_count += 1
                try:
                    executed += self._handle(fleet, msgid, sysid, compid, payload, addr)
                except (struct.error, ValueError) as exc:
                    self.crc_errors += 1
                    log.debug("MAVLink message %d from %s rejected: %s", msgid, addr, exc)
        return executed

    def _targets(self, fleet: "FleetManager", target_sys: int) -> list["UavAgent"]:
        if target_sys == 0:
            return [u for u in fleet.uavs.values() if u.sysid is not None]
        u = fleet.by_sysid(target_sys)
        return [u] if u is not None else []

    def _record(self, text: str) -> None:
        self.cmd_count += 1
        self.last_command = text
        self.last_command_at = time.monotonic()

    def _handle(self, fleet: "FleetManager", msgid: int, src_sys: int, src_comp: int, p: bytes, addr) -> int:
        if msgid == 0:  # HEARTBEAT
            mav_type = p[4]
            if mav_type == MAV_TYPE_GCS:
                if not self.gcs_connected:
                    fleet.bridge.log_event(
                        "FLEET", "ok", f"Ground station connected over MAVLink (sysid {src_sys}, {addr[0]}:{addr[1]})"
                    )
                self.gcs_heartbeat_at = time.monotonic()
                self.gcs_sysid = src_sys
                self.gcs_addr = addr
            return 0

        if msgid == 76:  # COMMAND_LONG
            *params, command, target_sys, target_comp, _confirm = struct.unpack("<7fHBBB", p)
            return self._run_command(fleet, command, params, None, target_sys, src_sys, src_comp)

        if msgid == 75:  # COMMAND_INT
            p1, p2, p3, p4, x, y, z, command, target_sys, target_comp, _frame, _cur, _auto = struct.unpack(
                "<4fiifHBBBBB", p
            )
            return self._run_command(fleet, command, [p1, p2, p3, p4, 0.0, 0.0, z], (x / 1e7, y / 1e7), target_sys, src_sys, src_comp)

        if msgid == 11:  # SET_MODE
            custom_mode, target_sys, _base = struct.unpack("<IBB", p)
            done = 0
            for u in self._targets(fleet, target_sys):
                result = self._set_mode(fleet, u, custom_mode)
                self.send_command_ack(u.sysid, 11, result, src_sys, src_comp)
                done += result == MAV_RESULT_ACCEPTED
            return done

        if msgid == 73:  # MISSION_ITEM_INT (current=2 -> guided fly-to)
            (_p1, _p2, _p3, _p4, x, y, z, _seq, command, target_sys, target_comp, _frame, current, _auto, _mtype) = (
                struct.unpack("<ffffiifHHBBBBBB", p)
            )
            done = 0
            for u in self._targets(fleet, target_sys):
                if current == 2 and command == MAV_CMD_NAV_WAYPOINT:
                    ok = self._goto(fleet, u, x / 1e7, y / 1e7, z, "MISSION_ITEM_INT")
                    self.send_mission_ack(u.sysid, MAV_MISSION_ACCEPTED if ok else MAV_MISSION_ERROR, src_sys, src_comp)
                    done += ok
                else:
                    self.send_mission_ack(u.sysid, MAV_MISSION_DENIED, src_sys, src_comp)
            return done

        if msgid == 86:  # SET_POSITION_TARGET_GLOBAL_INT
            _tbm, lat, lon, alt, *_rest, type_mask, target_sys, target_comp, _frame = struct.unpack("<Iii9fHBBB", p)
            if type_mask & 0x0007:
                return 0  # position ignored by the sender (velocity/accel-only setpoint)
            done = 0
            for u in self._targets(fleet, target_sys):
                done += self._goto(fleet, u, lat / 1e7, lon / 1e7, alt, "SET_POSITION_TARGET")
            return done

        if msgid in (21, 20):  # PARAM_REQUEST_LIST / PARAM_REQUEST_READ
            target_sys = p[0] if msgid == 21 else p[2]
            for u in self._targets(fleet, target_sys):
                self.send_param_value(u)
            return 0

        if msgid == 43:  # MISSION_REQUEST_LIST
            for u in self._targets(fleet, p[0]):
                self.send_mission_count(u.sysid, src_sys, src_comp)
            return 0
        return 0  # MISSION_ACK and other handshake traffic

    def _goto(self, fleet: "FleetManager", u: "UavAgent", lat: float, lng: float, alt: float, via: str) -> bool:
        args = {"lat": lat, "lng": lng}
        if math.isfinite(alt) and alt > 0:
            args["alt_m"] = alt
        ok, _ = fleet.command(u.id, "GOTO", source="GCS", **args)
        self._record(f"{u.id} GOTO ({via}) {'accepted' if ok else 'rejected'}")
        return ok

    def _set_mode(self, fleet: "FleetManager", u: "UavAgent", custom_mode: int) -> int:
        mode = next((m for m, v in u.utype.custom_modes.items() if v == int(custom_mode)), None)
        if mode is None:
            self._record(f"{u.id} SET_MODE {custom_mode} unsupported")
            return MAV_RESULT_UNSUPPORTED
        action = {MODE_LOITER: "HOLD"}.get(mode, mode)
        ok, _ = fleet.command(u.id, action, source="GCS")
        self._record(f"{u.id} MODE {mode} {'accepted' if ok else 'rejected'}")
        return MAV_RESULT_ACCEPTED if ok else MAV_RESULT_DENIED

    def _run_command(
        self, fleet: "FleetManager", command: int, params: list[float], latlng: tuple[float, float] | None,
        target_sys: int, src_sys: int, src_comp: int,
    ) -> int:
        done = 0
        for u in self._targets(fleet, target_sys):
            result = self._execute(fleet, u, command, params, latlng)
            self.send_command_ack(u.sysid, command, result, src_sys, src_comp)
            done += result == MAV_RESULT_ACCEPTED and command not in MAV_CMD_ACK_ONLY
        return done

    def _execute(
        self, fleet: "FleetManager", u: "UavAgent", command: int, params: list[float], latlng: tuple[float, float] | None
    ) -> int:
        if command in MAV_CMD_ACK_ONLY:
            return MAV_RESULT_ACCEPTED  # streams are fixed-rate; capability probes need no payload

        def finite(v: float) -> bool:
            return math.isfinite(v) and v > 0

        if command == MAV_CMD_DO_SET_MODE:
            return self._set_mode(fleet, u, int(params[1]))
        if command == MAV_CMD_DO_REPOSITION:
            lat, lng = latlng if latlng is not None else (params[4], params[5])
            if not (math.isfinite(lat) and math.isfinite(lng)):
                return MAV_RESULT_FAILED
            args = {"lat": lat, "lng": lng}
            if finite(params[6]):
                args["alt_m"] = params[6]
            if finite(params[0]):
                args["speed_mps"] = params[0]
            action = "GOTO"
        elif command == MAV_CMD_COMPONENT_ARM_DISARM:
            action, args = ("ARM" if params[0] >= 0.5 else "DISARM"), {}
        elif command == MAV_CMD_NAV_TAKEOFF:
            action, args = "TAKEOFF", ({"alt_m": params[6]} if finite(params[6]) else {})
        elif command == MAV_CMD_NAV_LAND:
            action, args = "LAND", {}
        elif command == MAV_CMD_NAV_RETURN_TO_LAUNCH:
            action, args = "RTL", {}
        elif command == MAV_CMD_DO_CHANGE_SPEED:
            if not finite(params[1]):
                return MAV_RESULT_FAILED
            action, args = "SET_SPEED", {"speed_mps": params[1]}
        else:
            self._record(f"{u.id} MAV_CMD {command} unsupported")
            return MAV_RESULT_UNSUPPORTED
        ok, _ = fleet.command(u.id, action, source="GCS", **args)
        self._record(f"{u.id} {action} {'accepted' if ok else 'rejected'}")
        return MAV_RESULT_ACCEPTED if ok else MAV_RESULT_DENIED
