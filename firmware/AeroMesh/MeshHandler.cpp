#include "MeshHandler.h"
#include "Config.h"
#include "Telemetry.h"

extern painlessMesh mesh;
extern Scheduler userScheduler; // The task scheduler from painlessMesh

bool isJammed = false;
unsigned long jammedStartTime = 0;
String latestPacket = "Awaiting Orders..."; // Latest hub broadcast shown on the phone UI
int activeChannel = MESH_CHANNEL;           // Logical channel announced by the hub (ACS)

// Define the broadcasting task
Task taskSendMessage(TASK_MILLISECOND * BROADCAST_INTERVAL_MS, TASK_FOREVER, &broadcastTelemetry);

void initMesh() {
  // Enforce AP+STA mode and the locked channel on every node
  mesh.init(MESH_PREFIX, MESH_PASSWORD, &userScheduler, MESH_PORT, WIFI_AP_STA, MESH_CHANNEL);

  // Assign callbacks for network events
  mesh.onReceive(&receivedCallback);
  mesh.onNewConnection(&newConnectionCallback);
  mesh.onChangedConnections(&changedConnectionCallback);

  userScheduler.addTask(taskSendMessage);
  taskSendMessage.enable();
}

void updateMesh() {
  mesh.update();
}

void broadcastTelemetry() {
  // 1. Check if the physical button is pressed (LOW) to trigger the simulation
  if (digitalRead(BOOT_BUTTON) == LOW && !isJammed) {
    isJammed = true;
    jammedStartTime = millis();
    Serial.println("CRITICAL: Signal attenuation simulated! Broadcasting distress...");
  }

  // 2. Build the payload using the current state and broadcast
  String msg = buildTelemetryPayload(isJammed);
  mesh.sendBroadcast(msg);

  // 3. Drop off the network 3 seconds after the simulated signal loss
  if (isJammed && (millis() - jammedStartTime > 3000)) {
    Serial.println("Signal lost. Node dropping from mesh...");
    ESP.restart(); // Hard reset to force the network to fracture and self-heal
  }
}

// Parses "CH_SWITCH:<n>" or "CH_SWITCH:<n>|<reason>".
// Returns the channel (1-13) or -1 if the frame is malformed.
int parseChannelSwitch(const String &msg) {
  const int prefixLen = 10; // strlen("CH_SWITCH:")
  int pipeIdx = msg.indexOf('|');
  String channelStr = (pipeIdx > prefixLen) ? msg.substring(prefixLen, pipeIdx) : msg.substring(prefixLen);
  channelStr.trim();
  int channel = channelStr.toInt();
  if (channel < 1 || channel > 13) {
    return -1;
  }
  return channel;
}

// ---------------------------------------------------------
// CALLBACK FUNCTIONS
// ---------------------------------------------------------

void receivedCallback(uint32_t from, String &msg) {
  if (msg.startsWith("{")) {
    // Standard JSON telemetry / replies: print for the Python bridge
    Serial.printf("Received from %u msg=%s\n", from, msg.c_str());
  } else if (msg.startsWith("CH_SWITCH:")) {
    // Adaptive Channel Switching notification from the hub
    int channel = parseChannelSwitch(msg);
    if (channel > 0) {
      activeChannel = channel;
      latestPacket = "[ACS] Switched to Channel " + String(activeChannel);
      Serial.printf("[ACS] Channel switch from %u -> Channel %d\n", from, activeChannel);
    } else {
      Serial.printf("[ACS] Ignored malformed channel switch from %u: %s\n", from, msg.c_str());
    }
  } else {
    // Plain text: a hub broadcast message for the phone UI
    latestPacket = msg;
    Serial.printf("DIRECT PACKET from %u: %s\n", from, msg.c_str());
  }
}

void newConnectionCallback(uint32_t nodeId) {
  Serial.printf("New Connection, nodeId = %u\n", nodeId);
}

void changedConnectionCallback() {
  Serial.printf("Changed connections in the mesh topology.\n");
}
