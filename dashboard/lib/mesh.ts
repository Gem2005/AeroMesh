/**
 * Shared types, constants, and pure helpers for the AeroMesh dashboard.
 * Used by app/page.tsx (state management) and components/MeshGraph.tsx (render).
 */

/* ------------------------------------------------------------------ */
/* Incoming packet types (from bridge.py)                              */
/* ------------------------------------------------------------------ */

/** {"node_id":1693866525,"status":"online","parent_rssi":-43} */
export interface TelemetryPacket {
  node_id: number;
  status: string;
  /** The mesh root reports "disconnected" (it has no parent link). */
  parent_rssi: number | "disconnected";
}

/** painlessMesh subConnectionJson(): {"nodeId":123,"subs":[{...}]} */
export interface TopologyPacket {
  nodeId: number;
  subs?: TopologyPacket[];
}

export type TelemetryEntry = TelemetryPacket & { lastSeen: number };
export type TelemetryMap = Record<number, TelemetryEntry>;

/* ------------------------------------------------------------------ */
/* Graph structures                                                    */
/* ------------------------------------------------------------------ */

export interface GraphNode {
  id: number;
  isRoot: boolean;
  status: "online" | "dropped";
  droppedAt?: number;
  /** Set once the failure drift velocity kick has been applied. */
  kicked?: boolean;
  // Injected/consumed by the d3 force engine at runtime:
  x?: number;
  y?: number;
  vx?: number;
  vy?: number;
}

export interface GraphLink {
  source: number | GraphNode;
  target: number | GraphNode;
}

export interface GraphData {
  nodes: GraphNode[];
  links: GraphLink[];
}

/** Resolve a link endpoint to its node id (d3 mutates ids into objects). */
export function idOf(endpoint: number | GraphNode | string | undefined): number {
  if (endpoint === undefined) return NaN;
  if (typeof endpoint === "object") return endpoint.id;
  return Number(endpoint);
}

/* ------------------------------------------------------------------ */
/* Tuning constants                                                    */
/* ------------------------------------------------------------------ */

/** Links weaker than this are considered degraded (amber). */
export const DEGRADED_RSSI = -85;
/** How long a failed node lingers (crimson, drifting) before purge. */
export const DROP_LINGER_MS = 2500;
/** Telemetry older than this marks a node stale/lost (fallback topology). */
export const STALE_MS = 8000;

/* ------------------------------------------------------------------ */
/* RSSI math                                                           */
/* ------------------------------------------------------------------ */

/**
 * Map RSSI to a physical link distance for the physics engine.
 * Strong signal pulls nodes together; weak signal strains them apart.
 *   -30 dBm ->  40 px    -100 dBm -> 280 px
 */
export function rssiToDistance(rssi: number): number {
  const clamped = Math.max(-100, Math.min(-30, rssi));
  return 40 + ((-clamped - 30) / 70) * 240;
}

/** Map -100..-40 dBm to a 0..100% quality score. */
export function rssiPercent(rssi: number): number {
  return Math.max(0, Math.min(100, Math.round(((rssi + 100) / 60) * 100)));
}

/* ------------------------------------------------------------------ */
/* Topology parsing                                                    */
/* ------------------------------------------------------------------ */

/** Recursively flatten the nested painlessMesh topology into nodes + links. */
export function flattenTopology(root: TopologyPacket): {
  nodes: { id: number; isRoot: boolean }[];
  links: { source: number; target: number }[];
} {
  const nodes: { id: number; isRoot: boolean }[] = [];
  const links: { source: number; target: number }[] = [];

  const walk = (node: TopologyPacket, parent: number | null) => {
    nodes.push({ id: node.nodeId, isRoot: parent === null });
    if (parent !== null) links.push({ source: parent, target: node.nodeId });
    node.subs?.forEach((sub) => walk(sub, node.nodeId));
  };

  walk(root, null);
  return { nodes, links };
}
