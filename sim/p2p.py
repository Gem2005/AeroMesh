from __future__ import annotations

import asyncio
import json
import time

import networkx as nx

from sim.constants import (
    GATEWAY_KEY,
    UAV_ID_PREFIX,
)
from sim.geo import utc_iso


class P2pMixin:
    # ------------------------------------------------------------------ #
    # P2P Graph Routing Engine
    # ------------------------------------------------------------------ #
    def route_p2p_message(
        self,
        source_id: str,
        sender: str,
        target_id: str,
        text: str,
        origin: str = "hardware",
    ) -> None:
        """Route a P2P message through the NetworkX digital-twin graph.

        Handles three cases:
          A) Unicast to a specific node (hardware or virtual)
          B) Mesh-wide broadcast (target_id == "ALL")
          C) Uplink to Command Hub (target_id in {"HUB", GATEWAY_KEY})
        """
        # Normalise target
        if not target_id:
            target_id = "ALL"
        sender = sender or source_id

        # Build the record shared across all paths below
        msg_id = f"p2p-{int(time.monotonic() * 1000)}-{source_id}"

        def _record(path: list[str], delivered: bool, uav_bridged: bool) -> dict:
            rec = {
                "id": msg_id,
                "source_id": source_id,
                "sender": sender,
                "target_id": target_id,
                "text": text,
                "path": path,
                "hops": max(0, len(path) - 1),
                "uav_bridged": uav_bridged,
                "delivered": delivered,
                "origin": origin,
                "ts": utc_iso(),
            }
            self.p2p_history.append(rec)
            if len(self.p2p_history) > 50:
                del self.p2p_history[: len(self.p2p_history) - 50]
            # Broadcast P2P_MESSAGE frame immediately
            self._spawn(self.broadcast(json.dumps({"type": "P2P_MESSAGE", **rec})))
            return rec

        # ── Case C: uplink to Command Hub ──
        if target_id in ("HUB", GATEWAY_KEY):
            path = list(reversed(self.paths.get(source_id, [source_id, GATEWAY_KEY])))
            uav_bridged = any(n.startswith(UAV_ID_PREFIX) for n in path)
            self.emit_packet_animation("inbound", path, f"[{sender} -> HUB] {text}")
            self.log_event("P2P", "ok", f"P2P {source_id} ({sender}) -> HUB via {len(path)-1} hop(s): {text}")
            _record(path, True, uav_bridged)
            return

        # ── Case B: mesh-wide broadcast ──
        if target_id == "ALL":
            self.serial_send(f"P2P:{source_id}|{sender}|ALL|{self._sanitize_serial_text(text)}")
            self.log_event("P2P", "ok", f"P2P BROADCAST from {source_id} ({sender}): {text}")
            # Animate up to 3 paths from source to reachable virtual neighbours
            animated = 0
            for node_id in list(self.mesh_graph.nodes):
                if node_id == source_id or animated >= 3:
                    break
                try:
                    path = nx.shortest_path(self.mesh_graph, source=source_id, target=node_id, weight="weight")
                    if len(path) >= 2:
                        uav_in = any(n.startswith(UAV_ID_PREFIX) for n in path)
                        self.emit_packet_animation("p2p", path, f"[{sender} -> ALL] {text}")
                        animated += 1
                except (nx.NetworkXNoPath, nx.NodeNotFound):
                    pass
            _record([source_id, "*"], True, False)
            return

        # ── Case A: unicast to a specific node ──
        try:
            path = nx.shortest_path(self.mesh_graph, source=source_id, target=target_id, weight="weight")
        except (nx.NetworkXNoPath, nx.NodeNotFound, nx.NetworkXError):
            path = []

        if path and len(path) >= 2:
            uav_bridged = any(n.startswith(UAV_ID_PREFIX) for n in path)
            hops = len(path) - 1
            uav_label = " [UAV BRIDGED]" if uav_bridged else ""
            self.emit_packet_animation("p2p", path, f"[{sender} -> {target_id}] {text}")
            self.log_event(
                "P2P", "ok",
                f"P2P {source_id} ({sender}) -> {target_id} via {hops} hop(s){uav_label}: {text}",
            )
            # Forward over serial so physical ESP32s receive it
            self.serial_send(f"P2P:{source_id}|{sender}|{target_id}|{self._sanitize_serial_text(text)}")
            _record(path, True, uav_bridged)

            # Virtual Peer Auto-Responder: if target is a virtual node, reply 1.2 s later
            target_node = self.find_node(target_id)
            if target_node is not None and target_node.type == "virtual" and target_node.reachable:
                rev_path = list(reversed(path))
                batt = int(target_node.battery_mv or 4000)
                rssi_val = target_node.rssi or -65
                ack_text = (
                    f"ACK from {target_id} (CH {target_node.channel}, "
                    f"RSSI {rssi_val:.0f}dBm, Batt {batt}mV): "
                    f"Copy '{text[:30]}'"
                )

                async def _delayed_ack(
                    _rev=rev_path, _tid=target_id, _sid=source_id, _ack=ack_text
                ):
                    await asyncio.sleep(1.2)
                    self.emit_packet_animation("p2p", _rev, f"[{_tid} -> {_sid}] {_ack}")
                    self.serial_send(
                        f"P2P:{_tid}|{_tid}|{_sid}|{self._sanitize_serial_text(_ack)}"
                    )
                    self.log_event(
                        "P2P", "ok",
                        f"Virtual auto-ACK: {_tid} -> {_sid}: {_ack}",
                    )
                    ack_rec = {
                        "id": f"p2p-ack-{int(time.monotonic()*1000)}-{_tid}",
                        "source_id": _tid,
                        "sender": _tid,
                        "target_id": _sid,
                        "text": _ack,
                        "path": _rev,
                        "hops": max(0, len(_rev) - 1),
                        "uav_bridged": any(n.startswith(UAV_ID_PREFIX) for n in _rev),
                        "delivered": True,
                        "origin": "virtual",
                        "ts": utc_iso(),
                    }
                    self.p2p_history.append(ack_rec)
                    if len(self.p2p_history) > 50:
                        del self.p2p_history[: len(self.p2p_history) - 50]
                    self._spawn(self.broadcast(json.dumps({"type": "P2P_MESSAGE", **ack_rec})))

                self._spawn(_delayed_ack())
        else:
            self.log_event(
                "P2P", "warn",
                f"P2P {source_id} -> {target_id} DROPPED: no active route in mesh topology "
                "(awaiting UAV bridge / channel heal)",
            )
            _record([source_id], False, False)
