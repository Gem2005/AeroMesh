#ifndef CONFIG_H
#define CONFIG_H

// --- CRITICAL COMPILER FLAG ---
// true = Flash this to the Gateway node plugged into your laptop via USB
// false = Flash this to the Edge nodes running on portable power banks
#define IS_GATEWAY false 
// ------------------------------

#define MESH_PREFIX "AeroMesh_Network"
#define MESH_PASSWORD "secure_password"
#define MESH_PORT 5555

// All nodes are locked to Wi-Fi Channel 6 (2437 MHz) so background scans
// never desynchronize the single 2.4 GHz radio between mesh peers.
#define MESH_CHANNEL 6

// 4000 ms leaves enough CPU headroom to serve the local web page without
// dropping mesh heartbeats.
#define BROADCAST_INTERVAL_MS 4000

// The physical BOOT button on standard generic ESP32 boards
#define BOOT_BUTTON 0 

#endif
