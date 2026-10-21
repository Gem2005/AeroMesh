#include "Config.h"
#include "MeshHandler.h"
#include <painlessMesh.h>

#if !IS_GATEWAY
  #include <ESPAsyncWebServer.h>
  #include <ESPmDNS.h> // Built-in ESP32 library for .local URLs
  
  AsyncWebServer server(80);

  String htmlEscape(const String &in) {
    String out;
    out.reserve(in.length() + 8);
    for (size_t i = 0; i < in.length(); i++) {
      char c = in[i];
      switch (c) {
        case '&': out += "&amp;"; break;
        case '<': out += "&lt;"; break;
        case '>': out += "&gt;"; break;
        case '"': out += "&quot;"; break;
        case '\'': out += "&#39;"; break;
        default: out += c;
      }
    }
    return out;
  }

  String jsonEscape(const String &in) {
    String out;
    out.reserve(in.length() + 8);
    for (size_t i = 0; i < in.length(); i++) {
      char c = in[i];
      if (c == '"' || c == '\\') {
        out += '\\';
        out += c;
      } else if (c >= 0x20) {
        out += c;
      }
    }
    return out;
  }
#endif

// Declare the global mesh and scheduler objects
painlessMesh mesh;
Scheduler userScheduler;

void setup() {
  Serial.begin(115200);
  Serial.setTimeout(100);
  pinMode(BOOT_BUTTON, INPUT_PULLUP);
  
  Serial.println("Starting AeroMesh Node...");
  initMesh();

  #if !IS_GATEWAY
    // 1. Start the mDNS Responder so phones on AeroMesh_Network can open http://aeromesh.local
    if (!MDNS.begin("aeromesh")) {
      Serial.println("Error setting up MDNS responder!");
    } else {
      MDNS.addService("http", "tcp", 80);
      Serial.println("mDNS responder started. Access via http://aeromesh.local");
    }

    // 2. Edge Node Web Server with Bidirectional HTML Form
    server.on("/", HTTP_GET, [](AsyncWebServerRequest *request){
      String html = "<html><head><meta name='viewport' content='width=device-width, initial-scale=1'><title>AeroMesh Edge</title></head>";
      html += "<body style='background:#0f172a; color:#38bdf8; font-family:monospace; padding:40px; font-size:24px;'>";
      html += "<h1>AeroMesh Tactical Edge</h1>";
      html += "<p style='font-size:20px; color:#94a3b8;'>Node " + String(mesh.getNodeId()) + "</p>";
      html += "<p style='display:inline-block; font-size:22px; background:#064e3b; color:#6ee7b7; padding:10px 18px; border-radius:8px;'>Channel " + String(activeChannel) + "</p>";
      // Orphan channel-hunt watchdog status: how long since the hub was last heard
      unsigned long beaconAgeMs = millis() - lastGatewayBeaconMs;
      bool hunting = beaconAgeMs > ORPHAN_HUNT_MS;
      html += "<p style='display:inline-block; margin-left:12px; font-size:18px; padding:8px 14px; border-radius:8px; background:" + String(hunting ? "#7f1d1d" : "#1e3a8a") + "; color:" + String(hunting ? "#fecaca" : "#bfdbfe") + ";'>";
      html += hunting ? "HUNTING CARRIER (beacon lost " + String(beaconAgeMs / 1000) + " s)" : "Hub beacon " + String(beaconAgeMs / 1000) + " s ago";
      html += "</p>";
      html += "<h2 style='color:#f87171;'>Latest Hub Message:</h2>";
      html += "<p style='font-size:32px; background:#1e293b; padding:20px; border-radius:10px;'>" + htmlEscape(latestPacket) + "</p>";
      html += "<hr style='border-color:#334155; margin:30px 0;'>";
      html += "<h2>Send Report to Command:</h2>";
      html += "<form action='/send' method='GET'>";
      html += "<input type='text' name='msg' placeholder='Type message...' maxlength='32' style='padding:15px; font-size:20px; width:70%; border-radius:5px; border:none;'>";
      html += "<button type='submit' style='padding:15px; font-size:20px; background:#0284c7; color:white; border:none; border-radius:5px; margin-left:10px; cursor:pointer;'>SEND</button>";
      html += "</form>";
      // Smart refresh: Only reload if the user isn't currently typing
      html += "<script>setInterval(() => { if(document.activeElement.name !== 'msg') location.reload(); }, 3000);</script>"; 
      html += "</body></html>";
      request->send(200, "text/html", html);
    });

    // Endpoint to catch form submission and broadcast back to the Python Gateway
    server.on("/send", HTTP_GET, [](AsyncWebServerRequest *request){
      if (request->hasParam("msg")) {
        String msg = request->getParam("msg")->value();
        String replyJson = "{\"node_id\": " + String(mesh.getNodeId()) + ", \"reply\": \"" + jsonEscape(msg) + "\"}";
        
        // painlessMesh floods the broadcast so it reaches the Gateway over any route
        mesh.sendBroadcast(replyJson);
        Serial.printf("Sent Reply to C2: %s\n", msg.c_str());
      }
      request->redirect("/");
    });

    // Catch-all to ensure reliability
    server.onNotFound([](AsyncWebServerRequest *request){
      request->redirect("/");
    });

    server.begin();
    Serial.println("Edge Node Web Server Running.");
  #endif
}

void loop() {
  updateMesh();
  
  #if IS_GATEWAY
    // Only the Gateway listens for Python Serial commands
    if (Serial.available()) {
      String input = Serial.readStringUntil('\n');
      input.trim();
      
      if (input.startsWith("TARGET:")) {
        // Format: TARGET:<ALL|node_id>|<message>
        // Every hub message is broadcast globally so every phone stays in sync.
        int separator = input.indexOf('|');
        if (separator > 0) {
          String payload = input.substring(separator + 1);
          if (payload.length() > 0) {
            mesh.sendBroadcast(payload);
            Serial.printf("[HUB] Broadcast message to mesh: %s\n", payload.c_str());
          }
        }
      } else if (input.startsWith("CH_SWITCH:")) {
        // Format: CH_SWITCH:<channel>|<reason>
        int channel = parseChannelSwitch(input);
        if (channel > 0) {
          activeChannel = channel;
          mesh.sendBroadcast(input);
          Serial.printf("[ACS] Gateway announced channel switch -> Channel %d\n", activeChannel);
        } else {
          Serial.printf("[ACS] Ignored malformed channel switch: %s\n", input.c_str());
        }
      }
    }
  #endif
}
