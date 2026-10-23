#include "Config.h"
#include "MeshHandler.h"
#include <painlessMesh.h>
#include <ArduinoJson.h>

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

    // ----------------------------------------------------------------
    // GET /peers  — mesh peer list for dropdown target selection
    // ----------------------------------------------------------------
    server.on("/peers", HTTP_GET, [](AsyncWebServerRequest *request){
      String json = "{\"node_id\":" + String(mesh.getNodeId()) + ",\"peers\":[\"ALL\",\"HUB\"";
      auto nodeList = mesh.getNodeList();
      for (auto &nid : nodeList) {
        json += "," + String(nid);
      }
      json += "]}";
      request->send(200, "application/json", json);
    });

    // ----------------------------------------------------------------
    // GET /messages  — ring buffer for AJAX polling
    // ----------------------------------------------------------------
    server.on("/messages", HTTP_GET, [](AsyncWebServerRequest *request){
      unsigned long now = millis();
      String json = "{\"node_id\":" + String(mesh.getNodeId());
      json += ",\"channel\":" + String(activeChannel);
      json += ",\"latest\":\"" + jsonEscape(latestPacket) + "\"";
      json += ",\"peer_count\":" + String(mesh.getNodeList().size());
      json += ",\"messages\":[";

      int total = min(msgCount, MSG_LOG_SIZE);
      int start = (msgCount >= MSG_LOG_SIZE) ? msgCount % MSG_LOG_SIZE : 0;
      bool first = true;
      for (int i = 0; i < total; i++) {
        int idx = (start + i) % MSG_LOG_SIZE;
        if (!first) json += ",";
        first = false;
        json += "{\"seq\":" + String(msgLog[idx].seq);
        json += ",\"from\":\"" + jsonEscape(msgLog[idx].fromId) + "\"";
        json += ",\"sender\":\"" + jsonEscape(msgLog[idx].sender) + "\"";
        json += ",\"target\":\"" + jsonEscape(msgLog[idx].target) + "\"";
        json += ",\"text\":\"" + jsonEscape(msgLog[idx].text) + "\"";
        json += ",\"ch\":" + String(msgLog[idx].channel);
        unsigned long age = (now - msgLog[idx].rxMillis) / 1000;
        json += ",\"age_s\":" + String(age);
        json += "}";
      }
      json += "]}";
      request->send(200, "application/json", json);
    });

    // ----------------------------------------------------------------
    // GET /send  — send P2P message from phone
    // ----------------------------------------------------------------
    server.on("/send", HTTP_GET, [](AsyncWebServerRequest *request){
      if (request->hasParam("msg")) {
        String msg = request->getParam("msg")->value();
        String nodeIdStr = String(mesh.getNodeId());
        String last4 = nodeIdStr.substring(nodeIdStr.length() > 4 ? nodeIdStr.length() - 4 : 0);
        String sender = request->hasParam("sender") ? request->getParam("sender")->value() : ("Phone-" + last4);
        String target = request->hasParam("target") ? request->getParam("target")->value() : "ALL";

        // Immediately log to own ring buffer so sender sees their message
        appendMessage(nodeIdStr, sender, target, msg, activeChannel);

        // Build P2P JSON and broadcast via mesh
        String replyJson = "{\"node_id\": " + nodeIdStr
          + ", \"p2p\": true"
          + ", \"sender\": \"" + jsonEscape(sender) + "\""
          + ", \"target\": \"" + jsonEscape(target) + "\""
          + ", \"p2p_msg\": \"" + jsonEscape(msg) + "\""
          + ", \"reply\": \"" + jsonEscape(msg) + "\""
          + ", \"channel\": " + String(activeChannel)
          + "}";

        mesh.sendBroadcast(replyJson);
        Serial.printf("Sent P2P: %s -> %s: %s\n", sender.c_str(), target.c_str(), msg.c_str());
      }

      // AJAX mode: return JSON instead of redirect
      if (request->hasParam("ajax") && request->getParam("ajax")->value() == "1") {
        request->send(200, "application/json", "{\"ok\":true}");
      } else {
        request->redirect("/");
      }
    });

    // ----------------------------------------------------------------
    // GET /  — responsive dark-mode mobile P2P chat UI
    // ----------------------------------------------------------------
    server.on("/", HTTP_GET, [](AsyncWebServerRequest *request){
      // Serve a complete single-page chat application
      String html = R"rawliteral(<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>AeroMesh P2P</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0a0f1e;color:#e2e8f0;font-family:'Segoe UI',system-ui,sans-serif;height:100vh;display:flex;flex-direction:column;overflow:hidden}
.hdr{background:linear-gradient(135deg,#0f172a,#1e293b);padding:10px 14px;border-bottom:1px solid #1e293b;display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.hdr h1{font-size:16px;font-weight:700;color:#38bdf8;letter-spacing:1px}
.badge{display:inline-block;font-size:11px;font-weight:600;padding:3px 10px;border-radius:6px}
.b-ch{background:#064e3b;color:#6ee7b7}
.b-id{background:#1e3a5f;color:#7dd3fc;font-size:10px}
.b-peers{background:#312e81;color:#a5b4fc;font-size:10px}
.cfg{background:#111827;padding:8px 14px;border-bottom:1px solid #1e293b;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.cfg label{font-size:10px;color:#64748b;letter-spacing:0.5px;text-transform:uppercase}
.cfg input,.cfg select{background:#0f172a;color:#e2e8f0;border:1px solid #334155;border-radius:5px;padding:5px 8px;font-size:12px;outline:none}
.cfg input:focus,.cfg select:focus{border-color:#38bdf8}
.feed{flex:1;overflow-y:auto;padding:10px 14px;display:flex;flex-direction:column;gap:4px}
.feed::-webkit-scrollbar{width:4px}
.feed::-webkit-scrollbar-thumb{background:#334155;border-radius:2px}
.msg{padding:6px 10px;border-radius:8px;font-size:12px;line-height:1.4;max-width:88%;word-break:break-word}
.msg-in{background:#1e293b;align-self:flex-start;border-bottom-left-radius:2px}
.msg-out{background:#0c4a6e;align-self:flex-end;border-bottom-right-radius:2px}
.msg-sys{background:#1a1a2e;color:#94a3b8;align-self:center;font-size:10px;border-radius:12px;padding:3px 12px}
.msg .meta{font-size:9px;color:#64748b;margin-bottom:2px}
.msg .meta .name{color:#38bdf8;font-weight:600}
.msg .meta .tgt{color:#a78bfa}
.bar{background:#111827;padding:8px 14px;border-top:1px solid #1e293b;display:flex;gap:6px}
.bar input{flex:1;background:#0f172a;color:#e2e8f0;border:1px solid #334155;border-radius:8px;padding:10px 12px;font-size:14px;outline:none}
.bar input:focus{border-color:#38bdf8}
.bar button{background:linear-gradient(135deg,#0369a1,#0ea5e9);color:#fff;border:none;border-radius:8px;padding:10px 18px;font-size:13px;font-weight:700;cursor:pointer;letter-spacing:0.5px}
.bar button:active{transform:scale(0.97)}
.empty{text-align:center;color:#475569;margin-top:40px;font-size:13px}
</style>
</head><body>
<div class="hdr">
<h1>AeroMesh P2P</h1>
<span class="badge b-ch" id="chBadge">CH --</span>
<span class="badge b-id" id="idBadge">Node --</span>
<span class="badge b-peers" id="peerBadge">0 peers</span>
</div>
<div class="cfg">
<div><label>Callsign</label><br><input id="callsign" placeholder="Field-Alpha" maxlength="16" style="width:110px"></div>
<div><label>Send To</label><br><select id="target" style="width:130px"><option value="ALL">ALL PHONES (Broadcast)</option><option value="HUB">COMMAND HUB</option></select></div>
</div>
<div class="feed" id="feed"><div class="empty">Connecting to mesh&hellip;</div></div>
<div class="bar">
<input id="msgInput" placeholder="Type message..." maxlength="80" autocomplete="off">
<button id="sendBtn" onclick="sendMsg()">SEND</button>
</div>
<script>
var lastSeq=0,myId='',callEl,tgtEl,feedEl,msgEl;
function init(){
  callEl=document.getElementById('callsign');
  tgtEl=document.getElementById('target');
  feedEl=document.getElementById('feed');
  msgEl=document.getElementById('msgInput');
  var saved=localStorage.getItem('aero_callsign');
  if(saved)callEl.value=saved;
  callEl.addEventListener('change',function(){localStorage.setItem('aero_callsign',callEl.value)});
  msgEl.addEventListener('keydown',function(e){if(e.key==='Enter'){e.preventDefault();sendMsg()}});
  poll();setInterval(poll,1500);
  loadPeers();setInterval(loadPeers,5000);
}
function poll(){
  fetch('/messages').then(r=>r.json()).then(function(d){
    myId=String(d.node_id);
    document.getElementById('chBadge').textContent='CH '+d.channel;
    document.getElementById('idBadge').textContent='Node '+myId;
    document.getElementById('peerBadge').textContent=(d.peer_count||0)+' peers';
    if(!d.messages||d.messages.length===0){if(lastSeq===0)feedEl.innerHTML='<div class="empty">No messages yet. Send one!</div>';return}
    var newMsgs=d.messages.filter(function(m){return m.seq>lastSeq});
    if(newMsgs.length===0)return;
    if(lastSeq===0)feedEl.innerHTML='';
    newMsgs.forEach(function(m){
      lastSeq=Math.max(lastSeq,m.seq);
      var div=document.createElement('div');
      var isMe=m.from===myId;
      var isSys=m.sender==='ACS'||m.sender==='COMMAND'||m.from==='SYSTEM'||m.from==='HUB';
      if(isSys){div.className='msg msg-sys';div.textContent=m.text}
      else{
        div.className='msg '+(isMe?'msg-out':'msg-in');
        div.innerHTML='<div class="meta"><span class="name">'+esc(m.sender)+'</span> &rarr; <span class="tgt">'+esc(m.target)+'</span> &middot; CH'+m.ch+'</div>'+esc(m.text);
      }
      feedEl.appendChild(div);
    });
    feedEl.scrollTop=feedEl.scrollHeight;
  }).catch(function(){});
}
function loadPeers(){
  fetch('/peers').then(r=>r.json()).then(function(d){
    var sel=tgtEl.value;
    tgtEl.innerHTML='<option value="ALL">ALL PHONES (Broadcast)</option><option value="HUB">COMMAND HUB</option>';
    (d.peers||[]).forEach(function(p){
      if(p!=='ALL'&&p!=='HUB'&&String(p)!==String(d.node_id)){
        var o=document.createElement('option');o.value=p;o.textContent='Node '+p;tgtEl.appendChild(o);
      }
    });
    tgtEl.value=sel||'ALL';
  }).catch(function(){});
}
function sendMsg(){
  var txt=msgEl.value.trim();if(!txt)return;
  var cs=callEl.value.trim()||('Phone-'+myId.slice(-4));
  var tg=tgtEl.value||'ALL';
  fetch('/send?ajax=1&msg='+encodeURIComponent(txt)+'&sender='+encodeURIComponent(cs)+'&target='+encodeURIComponent(tg))
  .then(function(){msgEl.value='';}).catch(function(){});
}
function esc(s){var d=document.createElement('div');d.textContent=s;return d.innerHTML}
window.onload=init;
</script>
</body></html>)rawliteral";
      request->send(200, "text/html", html);
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
      } else if (input.startsWith("P2P:")) {
        // P2P bridged message from bridge.py: broadcast to all mesh nodes
        mesh.sendBroadcast(input);
        Serial.printf("[P2P] Gateway relayed P2P packet to mesh: %s\n", input.c_str());
      }
    }
  #endif
}
