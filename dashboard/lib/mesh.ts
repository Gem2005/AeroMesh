/**
 * AeroMesh HIL Digital Twin -- shared wire types and helpers.
 *
 * All topology, RF and kinematics math lives in bridge.py (NetworkX);
 * this frontend is a pure renderer of the unified 1 Hz SNAPSHOT frame.
 */

/* ------------------------------------------------------------------ */
/* Snapshot schema (bridge.py -> dashboard)                            */
/* ------------------------------------------------------------------ */

export type LatLng = [number, number];
export type NodeStatus = "ONLINE" | "DEGRADED" | "JAMMED" | "OFFLINE";
export type NodeType = "hardware" | "virtual";
export type NodeRole = "gateway" | "edge" | "virtual";
export type PlacementMode = "ORBIT_PATROL" | "POLYGON_INFILL";
export type AcsState = "IDLE" | "CONFIRMING" | "SWITCHING" | "SCANNING";
export type MissionState = "LOITERING" | "DISPATCHED" | "BRIDGING_ACTIVE";

export interface MeshNodeState {
  id: string;
  type: NodeType;
  role: NodeRole;
  status: NodeStatus;
  channel: number;
  coords: LatLng;
  rssi: number | null;
  snr: number | null;
  battery_mv: number | null;
  parent_id: string | null;
  hop_path: string[];
  hop_count: number;
  reachable: boolean;
}

export interface MeshLink {
  source: string;
  target: string;
  rssi: number;
  /** 0..1 link quality derived from RSSI */
  quality: number;
  distance_m: number;
}

export interface NetworkState {
  channel: number;
  carrier_freq_mhz: number;
  health_score: number;
  pdr_percent: number;
  avg_latency_ms: number;
  self_heal_convergence_ms: number | null;
  graph_diameter: number;
  spectrum_noise: Record<string, number>;
  node_count: number;
  hardware_count: number;
  virtual_count: number;
  reachable_count: number;
  link_count: number;
}

export interface ThreatState {
  active: boolean;
  epicenter: LatLng | null;
  estimated_epicenter: LatLng | null;
  estimation_error_m: number | null;
  kill_radius_m: number;
  bleed_radius_m: number;
  channel: number | null;
  freq_mhz: number | null;
  jammed_count: number;
  degraded_count: number;
  report_count: number;
  eccm_auto: boolean;
  acs_state: AcsState;
  acs_target_channel: number | null;
  fhss_countdown_s: number | null;
  fhss_confirm_s: number;
}

export interface AutopilotState {
  id: string;
  coords: LatLng;
  target_waypoint: LatLng | null;
  home: LatLng;
  isolated_centroid: LatLng | null;
  alt_m: number;
  speed_mps: number;
  cruise_speed_mps: number;
  bearing_deg: number;
  eta_s: number | null;
  mission_state: MissionState;
  status: NodeStatus;
  isolated_count: number;
  bridged_count: number;
  waypoint_count: number;
  mavlink_host: string;
  mavlink_port: number;
  mavlink_backend: "pymavlink" | "stdlib";
  last_command: string | null;
  packet_count: number;
}

export interface Snapshot {
  type: "SNAPSHOT";
  ts: string;
  tick: number;
  bridge: { serial_connected: boolean; serial_port: string; baud: number; uptime_s: number };
  gateway: { id: string; coords: LatLng };
  swarm: { mode: PlacementMode; count: number; min: number; max: number; polygon: LatLng[] | null };
  network: NetworkState;
  nodes: MeshNodeState[];
  links: MeshLink[];
  threat: ThreatState;
  autopilot: AutopilotState;
}

export type LogTag = "SERIAL" | "RF-ALERT" | "CH-SWITCH" | "MAVLINK" | "MSG" | "SYSTEM";
export type Severity = "info" | "ok" | "warn" | "critical";

export interface LogEventFrame {
  type: "LOG_EVENT";
  tag: LogTag;
  severity: Severity;
  text: string;
  ts: string;
}

export interface LogHistoryFrame {
  type: "LOG_HISTORY";
  events: LogEventFrame[];
}

export interface PacketAnimationFrame {
  type: "PACKET_ANIMATION";
  direction: "outbound" | "inbound";
  path: string[];
  message: string;
}

export interface NodeReplyFrame {
  type: "NODE_REPLY";
  node_id: string;
  reply: string;
  ts: string;
}

export type BridgeFrame = Snapshot | LogEventFrame | LogHistoryFrame | PacketAnimationFrame | NodeReplyFrame;

/* ------------------------------------------------------------------ */
/* Commands (dashboard -> bridge.py)                                   */
/* ------------------------------------------------------------------ */

export type BridgeCommand =
  | { type: "OPERATOR_GPS"; lat: number; lon: number }
  | { type: "SET_SWARM_SIZE"; count: number }
  | { type: "SET_PLACEMENT_MODE"; mode: PlacementMode }
  | { type: "SET_DEPLOYMENT_POLYGON"; vertices: LatLng[] }
  | { type: "UPLINK_TX"; target_id: string; message: string }
  | {
      type: "DEPLOY_JAMMER";
      lat: number;
      lng: number;
      kill_radius_m: number;
      bleed_radius_m: number;
      freq_mhz: number;
      channel: number;
    }
  | { type: "CLEAR_JAMMER" }
  | { type: "SET_ECCM_MODE"; auto: boolean }
  | { type: "EXECUTE_FHSS"; target_channel: number }
  | { type: "REQUEST_SNAPSHOT" };

/* ------------------------------------------------------------------ */
/* Constants & helpers                                                 */
/* ------------------------------------------------------------------ */

/** Must match bridge.py --ws-port. */
export const WS_URL = "ws://localhost:8765";
/** Gateway anchor (bridge.py GATEWAY_LAT / GATEWAY_LON). */
export const DEFAULT_CENTER: LatLng = [12.8354, 80.138];
export const DEFAULT_ZOOM = 16;
export const RELAY_ID = "UAV-RELAY-01";
export const WIFI_CHANNELS = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11];
export const DEFAULT_CHANNEL = 6;
/** Fringe radius is scaled from the primary drop radius (200 m -> 450 m). */
export const BLEED_RATIO = 2.25;

export const channelFreqMhz = (ch: number) => 2407 + 5 * ch;

export type RssiBucket = "excellent" | "good" | "degraded" | "disconnected";

export const RSSI_BUCKETS: { key: RssiBucket; label: string; range: string; color: string }[] = [
  { key: "excellent", label: "Excellent", range: "> -55 dBm", color: "#34d399" },
  { key: "good", label: "Good", range: "-55 to -75 dBm", color: "#22d3ee" },
  { key: "degraded", label: "Degraded", range: "-75 to -88 dBm", color: "#fbbf24" },
  { key: "disconnected", label: "Disconnected", range: "< -88 dBm", color: "#f43f5e" },
];

export function rssiBucket(rssi: number | null, status?: NodeStatus): RssiBucket {
  if (rssi === null || status === "JAMMED" || status === "OFFLINE") return "disconnected";
  if (rssi > -55) return "excellent";
  if (rssi > -75) return "good";
  if (rssi > -88) return "degraded";
  return "disconnected";
}

/** 0..5 signal bars for roster display. */
export function rssiBars(rssi: number | null): number {
  if (rssi === null) return 0;
  if (rssi > -55) return 5;
  if (rssi > -65) return 4;
  if (rssi > -75) return 3;
  if (rssi > -82) return 2;
  if (rssi > -88) return 1;
  return 0;
}

export const STATUS_COLORS: Record<NodeStatus, string> = {
  ONLINE: "#22d3ee",
  DEGRADED: "#fbbf24",
  JAMMED: "#f43f5e",
  OFFLINE: "#64748b",
};

export function roleLabel(n: Pick<MeshNodeState, "role">): string {
  if (n.role === "gateway") return "HW-GATEWAY";
  if (n.role === "edge") return "HW-EDGE";
  return "V-NODE";
}

export const fmtCoord = (c: LatLng | null | undefined, digits = 5) =>
  c ? `${c[0].toFixed(digits)}, ${c[1].toFixed(digits)}` : "--";
