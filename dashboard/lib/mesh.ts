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
export type NodeType = "hardware" | "virtual" | "uav";
export type NodeRole = "gateway" | "edge" | "virtual" | "uav";
export type PlacementMode = "ORBIT_PATROL" | "POLYGON_INFILL";
export type AcsState = "IDLE" | "CONFIRMING" | "SWITCHING" | "SCANNING";
export type HoldReason = "LOST_LINK" | "RECONNECTING";

export interface MeshNodeState {
  id: string;
  type: NodeType;
  role: NodeRole;
  status: NodeStatus;
  channel: number;
  coords: LatLng;
  /** AGL; non-zero only for airborne relay UAVs */
  alt_m: number;
  rssi: number | null;
  snr: number | null;
  battery_mv: number | null;
  parent_id: string | null;
  hop_path: string[];
  hop_count: number;
  reachable: boolean;
  /** frozen at the position where the link was lost */
  holding: boolean;
  hold_reason: HoldReason | null;
  lost_link_s: number | null;
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
  uav_count: number;
  airborne_uav_count: number;
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

export interface SwarmState {
  mode: PlacementMode;
  polygon: LatLng[] | null;
  count: number;
  min: number;
  max: number;
  /** orbit ring radii (m), innermost first */
  radii: number[];
  speed_mps: number;
  max_ring_gap_m: number;
  link_range_m: number;
  holding_count: number;
  limits: {
    max_rings: number;
    radius_min_m: number;
    radius_max_m: number;
    min_gap_m: number;
    speed_min_mps: number;
    speed_max_mps: number;
  };
}

/** ArduCopter custom modes as used on the MAVLink link. */
export type FlightMode = "AUTO" | "GUIDED" | "LOITER" | "RTL" | "LAND";
export type FlightState = "LANDED" | "TAKING_OFF" | "TRANSIT" | "ON_STATION" | "HOLDING" | "RETURNING" | "LANDING";
export type UavTask = "RELAY" | "RELIEF" | "GOTO";
export type Failsafe = "LOW_BATTERY" | "CRITICAL_BATTERY" | "LOST_LINK" | "GEOFENCE";

export interface UavState {
  id: string;
  /** MAVLink system id; null once the fleet exceeds 255 vehicles */
  sysid: number | null;
  mode: FlightMode;
  flight_state: FlightState;
  armed: boolean;
  coords: LatLng;
  alt_m: number;
  cruise_alt_m: number;
  speed_mps: number;
  cruise_speed_mps: number;
  climb_mps: number;
  bearing_deg: number;
  eta_s: number | null;
  target: LatLng | null;
  home: LatLng;
  task: UavTask | null;
  slot_label: string | null;
  relieving: string | null;
  relief_by: string | null;
  battery_pct: number;
  endurance_remaining_s: number;
  failsafe: Failsafe | null;
  in_mesh: boolean;
  reachable: boolean;
  status: NodeStatus | "STANDBY";
  parent_id: string | null;
  link_rssi: number | null;
  hop_count: number;
  bridged_count: number;
  dist_home_m: number;
  mission_time_s: number;
  c2_lost_s: number | null;
  waypoint_seq: number;
  last_command: string | null;
  remove_on_land: boolean;
}

export interface IsolatedCluster {
  id: number;
  size: number;
  centroid: LatLng;
  anchor_id: string;
  anchor: LatLng;
  entry_id: string;
  entry: LatLng;
  distance_m: number;
  relays_needed: number;
}

export interface GcsLinkState {
  host: string;
  port: number;
  local_port: number;
  backend: string;
  packet_count: number;
  rx_count: number;
  cmd_count: number;
  rx_errors: number;
  connected: boolean;
  gcs_addr: string | null;
  gcs_sysid: number | null;
  heartbeat_age_s: number | null;
  last_command: string | null;
  last_command_age_s: number | null;
}

export interface FleetState {
  uavs: UavState[];
  counts: { total: number; airborne: number; bridging: number; landed: number; low_battery: number; failsafe: number };
  allocator: {
    clusters: IsolatedCluster[];
    isolated: number;
    needed: number;
    assigned: number;
    shortfall: number;
    /** gateway jammed: relays cannot restore C2 */
    suspended: boolean;
    slots: { coords: LatLng; label: string; cluster: number }[];
  };
  geofence: { center: LatLng; radius_m: number; ceiling_m: number };
  config: {
    endurance_min: number;
    soft_limit: number;
    speed_min_mps: number;
    speed_max_mps: number;
    alt_min_m: number;
    alt_max_m: number;
    low_battery_pct: number;
    critical_battery_pct: number;
    relief_battery_pct: number;
    lost_link_s: number;
    relay_range_m: number;
    a2a_range_m: number;
  };
  gcs: GcsLinkState;
}

export interface Snapshot {
  type: "SNAPSHOT";
  ts: string;
  tick: number;
  bridge: { serial_connected: boolean; serial_port: string; baud: number; uptime_s: number };
  gateway: { id: string; coords: LatLng };
  swarm: SwarmState;
  network: NetworkState;
  /** ground nodes plus airborne relay UAVs that are part of the mesh */
  nodes: MeshNodeState[];
  links: MeshLink[];
  threat: ThreatState;
  fleet: FleetState;
}

export type LogTag = "SERIAL" | "RF-ALERT" | "CH-SWITCH" | "FLEET" | "MAVLINK" | "MSG" | "SYSTEM";
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

export type UavAction =
  | "TAKEOFF"
  | "HOLD"
  | "RTL"
  | "LAND"
  | "AUTO"
  | "GUIDED"
  | "GOTO"
  | "SET_ALT"
  | "SET_SPEED"
  | "ARM"
  | "DISARM";
export type FleetAction = "LAUNCH_ALL" | "AUTO_ALL" | "RTL_ALL" | "LAND_ALL";

export type BridgeCommand =
  | { type: "OPERATOR_GPS"; lat: number; lon: number }
  | { type: "SET_SWARM_SIZE"; count: number }
  | { type: "SET_PLACEMENT_MODE"; mode: PlacementMode }
  | { type: "SET_DEPLOYMENT_POLYGON"; vertices: LatLng[] }
  | { type: "SET_ORBIT_CONFIG"; radii: number[]; speed_mps: number }
  | { type: "FLEET_ADD"; count: number }
  | { type: "FLEET_REMOVE"; uav_id: string }
  | {
      type: "UAV_COMMAND";
      uav_id: string;
      action: UavAction;
      lat?: number;
      lng?: number;
      alt_m?: number;
      speed_mps?: number;
    }
  | { type: "FLEET_COMMAND"; action: FleetAction }
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
/** Relay UAV ids are UAV-01, UAV-02, ... (bridge.py UAV_ID_PREFIX). */
export const UAV_ID_PREFIX = "UAV-";
export const isUavId = (id: string) => id.startsWith(UAV_ID_PREFIX);
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
  if (n.role === "uav") return "UAV-RELAY";
  return "V-NODE";
}

export const FLIGHT_MODE_COLORS: Record<FlightMode, string> = {
  AUTO: "#a78bfa",
  GUIDED: "#38bdf8",
  LOITER: "#22d3ee",
  RTL: "#fbbf24",
  LAND: "#fb923c",
};

export const FLIGHT_STATE_LABEL: Record<FlightState, string> = {
  LANDED: "LANDED",
  TAKING_OFF: "TAKEOFF",
  TRANSIT: "TRANSIT",
  ON_STATION: "ON STATION",
  HOLDING: "HOLDING",
  RETURNING: "RETURNING",
  LANDING: "LANDING",
};

export function batteryColor(pct: number, cfg?: Pick<FleetState["config"], "low_battery_pct" | "relief_battery_pct">): string {
  if (pct <= (cfg?.low_battery_pct ?? 30)) return "#f43f5e";
  if (pct <= (cfg?.relief_battery_pct ?? 40)) return "#fbbf24";
  return "#34d399";
}

export function fmtDuration(s: number | null | undefined): string {
  if (s === null || s === undefined || !Number.isFinite(s)) return "--";
  const total = Math.max(0, Math.round(s));
  const m = Math.floor(total / 60);
  const sec = total % 60;
  return m >= 60 ? `${Math.floor(m / 60)}h${String(m % 60).padStart(2, "0")}` : `${m}:${String(sec).padStart(2, "0")}`;
}

export const fmtCoord = (c: LatLng | null | undefined, digits = 5) =>
  c ? `${c[0].toFixed(digits)}, ${c[1].toFixed(digits)}` : "--";
