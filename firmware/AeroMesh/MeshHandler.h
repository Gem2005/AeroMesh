#ifndef MESH_HANDLER_H
#define MESH_HANDLER_H

#include <painlessMesh.h>

// Expose state variables for the simulation and messaging
extern bool isJammed;
extern unsigned long jammedStartTime;
extern String latestPacket;
extern int activeChannel;
#if !IS_GATEWAY
extern unsigned long lastGatewayBeaconMs;
#endif

void initMesh();
void updateMesh();
void broadcastTelemetry();
void orphanHuntWatchdog();
void noteGatewayBeacon();
void receivedCallback(uint32_t from, String &msg);
void newConnectionCallback(uint32_t nodeId);
void changedConnectionCallback();
int parseChannelSwitch(const String &msg);

#endif
