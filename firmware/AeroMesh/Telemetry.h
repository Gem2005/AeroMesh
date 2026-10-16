#ifndef TELEMETRY_H
#define TELEMETRY_H

#include <ArduinoJson.h>
#include <painlessMesh.h>
#include <WiFi.h>

extern painlessMesh mesh;
extern int activeChannel;

// Builds the JSON telemetry frame consumed by bridge.py
String buildTelemetryPayload(bool isAttenuated) {
  JsonDocument doc; 

  doc["node_id"] = mesh.getNodeId();
  doc["channel"] = activeChannel;
  
  if (isAttenuated) {
    doc["status"] = "JAMMED";
    doc["parent_rssi"] = -95; // Simulated critical signal drop
  } else {
    doc["status"] = "online";
    int currentRSSI = WiFi.RSSI();
    
    if (currentRSSI < 0) {
      doc["parent_rssi"] = currentRSSI;
    } else {
      doc["parent_rssi"] = "disconnected";
    }
  }

  String payload;
  serializeJson(doc, payload);

  return payload;
}

#endif
