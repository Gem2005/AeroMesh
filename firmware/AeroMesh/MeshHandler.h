#ifndef MESH_HANDLER_H
#define MESH_HANDLER_H

#include <painlessMesh.h>

// ------------------------------------------------------------------ //
// P2P Ring Buffer: 12-message circular log for peer-to-peer chat
// ------------------------------------------------------------------ //
struct PeerMessage {
    uint32_t seq;
    String fromId;
    String sender;
    String target;
    String text;
    int channel;
    unsigned long rxMillis;
};

#define MSG_LOG_SIZE 12

extern PeerMessage msgLog[MSG_LOG_SIZE];
extern int msgCount;
extern uint32_t nextMsgSeq;

void appendMessage(const String& fromId, const String& sender, const String& target, const String& text, int ch);

// ------------------------------------------------------------------ //
// Existing state variables
// ------------------------------------------------------------------ //
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
