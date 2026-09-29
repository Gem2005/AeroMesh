/**
 * AeroMesh Tactical Types — Shared types and constants for the
 * GPS-mapped tactical dashboard. All topology and FSPL math lives
 * in the Python bridge (NetworkX); this frontend is a pure renderer.
 */

/* ------------------------------------------------------------------ */
/* Incoming WebSocket payload types (from bridge.py)                   */
/* ------------------------------------------------------------------ */

/** A single mesh node with projected GPS coordinates. */
export interface TopoNode {
  id: number;
  lat: number;
  lon: number;
  /** OFFLINE = telemetry stale/lost (set by the bridge watchdog). */
  status: "online" | "JAMMED" | "OFFLINE";
  /** RSSI in dBm from parent node (null for gateway) */
  rssi: number | null;
  /** FSPL-calculated distance from parent in meters (null for gateway) */
  distance_m: number | null;
  /** True if this node acts as a mesh relay for child nodes */
  is_relay?: boolean;
}

/** A link between two mesh nodes. */
export interface TopoLink {
  source: number;
  target: number;
  /** RSSI in dBm for this link (null if unavailable) */
  rssi: number | null;
  /** FSPL-calculated distance for this link in meters (null if unavailable) */
  distance_m: number | null;
  /** True if this link is a mesh relay link (child-to-child relay) */
  is_relay?: boolean;
}

/** Full topology snapshot from the bridge. */
export interface TopologyPayload {
  type: "TOPOLOGY";
  network_strength?: number;
  nodes: TopoNode[];
  links: TopoLink[];
}

/** UAV dispatch command when a node is jammed. */
export interface DispatchPayload {
  type: "UAV_DISPATCH";
  target_node: number;
  midpoint: [number, number]; // [lat, lon]
}

/** Reply from a field operator relayed through the mesh. */
export interface ManualReplyPayload {
  type?: undefined; // replies have no type discriminator — identified by "reply" key
  node_id: number;
  reply: string;
}

export type BridgeMessage = TopologyPayload | DispatchPayload | ManualReplyPayload;

/* ------------------------------------------------------------------ */
/* Aerial relay (injected UAV marker)                                  */
/* ------------------------------------------------------------------ */

export interface AerialRelay {
  lat: number;
  lon: number;
  targetNodeId: number;
  injectedAt: number;
}

/* ------------------------------------------------------------------ */
/* Packet flow animation (triggered by bridge on C2 uplink/downlink)   */
/* ------------------------------------------------------------------ */

/** Transient animation overlay showing packet routing hops on the map. */
export interface PacketAnimation {
  /** Unique ID for React key + cleanup tracking. */
  id: number;
  /** "outbound" = C2 command (GW → Node), "inbound" = field reply (Node → GW). */
  direction: "outbound" | "inbound";
  /** Ordered array of node IDs representing exact relay hops. */
  path: number[];
}

/* ------------------------------------------------------------------ */
/* Constants                                                           */
/* ------------------------------------------------------------------ */

/** Default map center (SRM campus). Used as fallback if geolocation denied. */
export const DEFAULT_CENTER: [number, number] = [12.8406, 80.1534];
export const DEFAULT_ZOOM = 16;

/** The first TOPOLOGY node is always the gateway (GATEWAY_ID in bridge.py). */
export const GATEWAY_ID = 1693866525;
