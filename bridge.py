"""
AeroMesh Bridge -- Serial-to-WebSocket relay for the Self-Healing Ad-Hoc
Wireless Network with UAV Bridge project.

Reads JSON lines from the painlessMesh Gateway ESP32 (USB serial) and
broadcasts every valid JSON line to all connected WebSocket clients
(the Next.js dashboard).

Setup:
    pip install pyserial websockets

Run:
    python bridge.py                       # defaults: COM9 @ 115200, ws://localhost:8765
    python bridge.py --port COM5           # custom serial port
    python bridge.py --port COM9 --baud 115200 --ws-port 8765

Note: default WS port is 8765 (not 8080) because Apache/XAMPP commonly
occupies 8080. Keep it in sync with WS_URL in dashboard/app/page.tsx.
"""

import argparse
import asyncio
import json
import logging
import sys
import time

import serial  # pyserial
import websockets

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


class Bridge:
    def __init__(self, serial_port: str, baud: int, ws_host: str, ws_port: int):
        self.serial_port = serial_port
        self.baud = baud
        self.ws_host = ws_host
        self.ws_port = ws_port
        # Set of connected WebSocket client connections.
        self.clients = set()

    # ------------------------------------------------------------------ #
    # WebSocket side
    # ------------------------------------------------------------------ #
    async def handle_client(self, websocket):
        """Register a dashboard client and keep the connection open."""
        self.clients.add(websocket)
        peer = websocket.remote_address
        log.info("Dashboard connected: %s (total clients: %d)", peer, len(self.clients))
        try:
            # We never expect inbound messages; just wait until the client leaves.
            async for _ in websocket:
                pass
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            self.clients.discard(websocket)
            log.info("Dashboard disconnected: %s (total clients: %d)", peer, len(self.clients))

    async def broadcast(self, message: str):
        """Send a message to every connected client, dropping dead sockets."""
        if not self.clients:
            return
        clients = list(self.clients)  # snapshot: the set may change while we await
        results = await asyncio.gather(
            *(client.send(message) for client in clients),
            return_exceptions=True,
        )
        for client, result in zip(clients, results):
            if isinstance(result, Exception):
                self.clients.discard(client)

    # ------------------------------------------------------------------ #
    # Serial side
    # ------------------------------------------------------------------ #
    def _open_serial(self) -> serial.Serial:
        """
        Open the serial port and hard-reset the ESP32 into run mode
        (same DTR/RTS pulse that esptool performs). Blocking; run in executor.
        """
        ser = serial.Serial(self.serial_port, self.baud, timeout=1)
        ser.dtr = False   # GPIO0 high -> normal boot (not download mode)
        ser.rts = True    # EN low -> hold chip in reset
        time.sleep(0.2)
        ser.rts = False   # release reset -> chip boots
        ser.reset_input_buffer()
        return ser

    def _read_line(self, ser: serial.Serial) -> bytes:
        """Blocking line read (run in executor). Returns b'' on timeout."""
        return ser.readline()

    @staticmethod
    def _extract_json(line: str) -> str | None:
        """
        Return the JSON payload contained in a serial line, or None.

        Accepts both bare JSON lines and lines with a prefix/suffix around the
        JSON, e.g. 'TOPOLOGY: {"nodeId":123,"subs":[]}'.
        """
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
        """
        Continuously read lines from the serial port and broadcast valid JSON.
        Survives unplugged cables / port errors by retrying forever.
        """
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
                        # Read timeout -- watch for a hung/crashed gateway.
                        now = asyncio.get_event_loop().time()
                        silent_for = now - last_data
                        if silent_for > SILENCE_RESET_S:
                            # Cycling the port pulses DTR, hard-resetting the
                            # ESP32 -- recovers a hung/crashed gateway sketch.
                            log.warning(
                                "No serial data for %.0fs -- cycling %s to "
                                "auto-reset the gateway ESP32.",
                                silent_for, self.serial_port,
                            )
                            break  # close + reopen via the outer loop
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
                        # Show raw output so firmware issues are visible.
                        log.info("Serial (non-JSON, ignored): %s", line[:200])
                        continue

                    log.info("MESH -> WS: %s", payload)
                    await self.broadcast(payload)

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

    # ------------------------------------------------------------------ #
    # Entry point
    # ------------------------------------------------------------------ #
    async def run(self):
        async with websockets.serve(self.handle_client, self.ws_host, self.ws_port):
            log.info("WebSocket server listening on ws://%s:%d", self.ws_host, self.ws_port)
            await self.serial_loop()  # runs forever


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
