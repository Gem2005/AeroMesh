"use client";

import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Circle,
  CircleMarker,
  MapContainer,
  Marker,
  Polygon,
  Polyline,
  TileLayer,
  Tooltip,
  useMapEvents,
} from "react-leaflet";
import L from "leaflet";
import "leaflet/dist/leaflet.css";

import {
  BLEED_RATIO,
  DEFAULT_CENTER,
  DEFAULT_ZOOM,
  FLIGHT_MODE_COLORS,
  FLIGHT_STATE_LABEL,
  RSSI_BUCKETS,
  STATUS_COLORS,
  ZONE_MODE_META,
  batteryColor,
  isUavId,
  roleLabel,
  rssiBucket,
  shepherdFootprintM,
  uavTypeColor,
  type Airframe,
  type FleetState,
  type FlightMode,
  type HoldReason,
  type LatLng,
  type MeshNodeState,
  type NodeStatus,
  type SlotKind,
  type Snapshot,
  type UavState,
} from "@/lib/mesh";
import type { ActiveAnimation } from "@/lib/useBridge";
import type { InteractionMode } from "@/components/NodeOrchestrator";

/* ------------------------------------------------------------------ */
/* SVG marker factories (cached so Leaflet never rebuilds icons)       */
/* ------------------------------------------------------------------ */

const iconCache = new Map<string, L.DivIcon>();

function cachedIcon(key: string, build: () => L.DivIcon): L.DivIcon {
  let icon = iconCache.get(key);
  if (!icon) {
    icon = build();
    iconCache.set(key, icon);
  }
  return icon;
}

const HW_COLORS: Record<NodeStatus, string> = {
  ONLINE: "#f59e0b",
  DEGRADED: "#fb923c",
  JAMMED: "#f43f5e",
  OFFLINE: "#64748b",
};

function gatewayIcon(status: NodeStatus, selected: boolean) {
  return cachedIcon(`gw:${status}:${selected}`, () => {
    const stroke = status === "ONLINE" ? "#facc15" : STATUS_COLORS[status];
    return L.divIcon({
      className: "",
      html: `<div class="mk mk-gateway${selected ? " mk-selected" : ""}" style="--mk:${stroke}">
        <span class="mk-pulse"></span>
        <svg viewBox="0 0 44 44" width="44" height="44">
          <polygon points="22,3 38.5,12.5 38.5,31.5 22,41 5.5,31.5 5.5,12.5" fill="rgba(250,204,21,0.16)" stroke="${stroke}" stroke-width="2.5"/>
          <polygon points="22,11 31,16.2 31,27.8 22,33 13,27.8 13,16.2" fill="none" stroke="${stroke}" stroke-opacity="0.45" stroke-width="1"/>
          <text x="22" y="25.5" text-anchor="middle" font-size="9.5" font-weight="700" fill="#fde68a" font-family="monospace">GW</text>
        </svg>
      </div>`,
      iconSize: [44, 44],
      iconAnchor: [22, 22],
    });
  });
}

function edgeIcon(status: NodeStatus, selected: boolean) {
  return cachedIcon(`hw:${status}:${selected}`, () => {
    const c = HW_COLORS[status];
    return L.divIcon({
      className: "",
      html: `<div class="mk mk-edge${selected ? " mk-selected" : ""}" style="--mk:${c}">
        <svg viewBox="0 0 34 34" width="34" height="34">
          <polygon points="17,2 32,17 17,32 2,17" fill="${c}" fill-opacity="0.2" stroke="${c}" stroke-width="2.2"/>
          <text x="17" y="20.5" text-anchor="middle" font-size="9" font-weight="700" fill="#fef3c7" font-family="monospace">HW</text>
        </svg>
      </div>`,
      iconSize: [34, 34],
      iconAnchor: [17, 17],
    });
  });
}

const HOLD_COLORS: Record<HoldReason, string> = {
  LOST_LINK: "#f43f5e",
  RECONNECTING: "#38bdf8",
};

function virtualIcon(status: NodeStatus, selected: boolean, hold: HoldReason | null) {
  return cachedIcon(`v:${status}:${selected}:${hold}`, () => {
    const c = status === "ONLINE" ? "#2dd4bf" : STATUS_COLORS[status];
    const holdFrame = hold
      ? `<rect x="1" y="1" width="22" height="22" rx="2" fill="none" stroke="${HOLD_COLORS[hold]}" stroke-width="1.4" stroke-dasharray="3 2"/>`
      : "";
    return L.divIcon({
      className: "",
      html: `<div class="mk mk-virtual mk-${status.toLowerCase()}${selected ? " mk-selected" : ""}" style="--mk:${c}">
        <svg viewBox="0 0 24 24" width="24" height="24">
          ${holdFrame}
          <path d="M12 4 L20 19 L12 14.6 L4 19 Z" fill="${c}" fill-opacity="0.85" stroke="${c}" stroke-width="1.2" stroke-linejoin="round"/>
        </svg>
      </div>`,
      iconSize: [24, 24],
      iconAnchor: [12, 12],
    });
  });
}

function rotorSvg(c: string, rotor: string, count: 4 | 6) {
  const pts = Array.from({ length: count }, (_, i) => {
    const a = ((i * 360) / count + (count === 4 ? 45 : 0)) * (Math.PI / 180);
    return [20 + 12 * Math.cos(a), 20 + 12 * Math.sin(a)] as const;
  });
  const arms = pts.map(([x, y]) => `<line x1="20" y1="20" x2="${x.toFixed(1)}" y2="${y.toFixed(1)}"/>`).join("");
  const rotors = pts.map(([x, y]) => `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="${count === 4 ? 5 : 4}" stroke="${rotor}"/>`).join("");
  return `<g stroke="${c}" stroke-width="2" fill="none">${arms}${rotors}</g>
    <rect x="15.5" y="15.5" width="9" height="9" rx="2" fill="${c}" fill-opacity="0.55" stroke="#ede9fe" stroke-width="1.2"/>`;
}

function planeSvg(c: string, rotor: string) {
  return `<g fill="${c}" fill-opacity="0.6" stroke="#ede9fe" stroke-width="1.1" stroke-linejoin="round">
      <path d="M20 4 L22.5 12 L22.5 17 L36 21 L36 24 L22.5 22.5 L22 31 L26 34 L26 36 L20 34.5 L14 36 L14 34 L18 31 L17.5 22.5 L4 24 L4 21 L17.5 17 L17.5 12 Z"/>
    </g>
    <g stroke="${rotor}" stroke-width="1.4" fill="none"><circle cx="10" cy="20.5" r="3"/><circle cx="30" cy="20.5" r="3"/></g>`;
}

/** VTOL heading buckets: 24 x 15 deg keeps the icon cache small. */
const HEADING_STEP_DEG = 15;

function uavIcon(mode: FlightMode, type: string, airframe: Airframe, heading: number, airborne: boolean, selected: boolean, alert: boolean) {
  const fixedWing = airframe === "VTOL_FIXED_WING";
  const hdg = fixedWing && airborne ? (Math.round(heading / HEADING_STEP_DEG) * HEADING_STEP_DEG) % 360 : 0;
  return cachedIcon(`uav:${mode}:${type}:${hdg}:${airborne}:${selected}:${alert}`, () => {
    const c = FLIGHT_MODE_COLORS[mode];
    const size = airborne ? 36 : 26;
    const rotor = alert ? "#f43f5e" : uavTypeColor(type);
    const body = fixedWing ? planeSvg(c, rotor) : rotorSvg(c, rotor, type === "HEXA-H" ? 6 : 4);
    return L.divIcon({
      className: "",
      html: `<div class="mk mk-relay${airborne ? " mk-relay-active" : " mk-offline"}${selected ? " mk-selected" : ""}" style="--mk:${c}">
        <svg viewBox="0 0 40 40" width="${size}" height="${size}" style="transform:rotate(${hdg}deg)">${body}</svg>
      </div>`,
      iconSize: [size, size],
      iconAnchor: [size / 2, size / 2],
    });
  });
}

const COVER_COLOR = "#22d3ee";
const SHEPHERD_COLOR = "#34d399";
const MAST_COLOR = "#38bdf8";
const STRANDED_COLOR = "#fbbf24";
/** bridge.py MAST_HORIZONTAL_RADIUS_M: the mast must hover within this of the gateway */
const MAST_HORIZONTAL_RADIUS_M = 25;
const SLOT_COLORS: Record<SlotKind, string> = { RELAY: "#a78bfa", COVER: COVER_COLOR, SHEPHERD: SHEPHERD_COLOR, MAST: MAST_COLOR };

function slotIcon(kind: SlotKind, assigned: boolean) {
  return cachedIcon(`slot:${kind}:${assigned}`, () => {
    const c = SLOT_COLORS[kind];
    const dash = assigned ? "0" : "2 1.5";
    const shape =
      kind === "COVER"
        ? `<circle cx="8" cy="8" r="6.2" fill="none" stroke="${c}" stroke-width="1.4" stroke-dasharray="${dash}"/>`
        : kind === "SHEPHERD"
          ? `<path d="M8 1.5 L14.5 13 L1.5 13 Z" fill="none" stroke="${c}" stroke-width="1.4" stroke-linejoin="round" stroke-dasharray="${dash}"/>`
          : kind === "MAST"
            ? `<rect x="2" y="2" width="12" height="12" rx="1.5" fill="none" stroke="${c}" stroke-width="1.4" stroke-dasharray="${dash}"/><line x1="8" y1="2" x2="8" y2="14" stroke="${c}" stroke-width="1"/>`
            : `<polygon points="8,1 15,8 8,15 1,8" fill="none" stroke="${c}" stroke-width="1.4" stroke-dasharray="${dash}"/>`;
    return L.divIcon({
      className: "",
      html: `<div class="mk" style="--mk:${c};cursor:default">
        <svg viewBox="0 0 16 16" width="16" height="16">${shape}<circle cx="8" cy="8" r="1.6" fill="${c}"/></svg>
      </div>`,
      iconSize: [16, 16],
      iconAnchor: [8, 8],
    });
  });
}

function crosshairIcon(kind: "wp" | "est") {
  return cachedIcon(`xh:${kind}`, () => {
    const c = kind === "wp" ? "#38bdf8" : "#fb7185";
    return L.divIcon({
      className: "",
      html: `<div class="mk mk-crosshair">
        <svg viewBox="0 0 36 36" width="36" height="36">
          <circle cx="18" cy="18" r="10" fill="none" stroke="${c}" stroke-width="1.6" stroke-dasharray="${kind === "est" ? "3 2" : "0"}"/>
          <line x1="18" y1="2" x2="18" y2="12" stroke="${c}" stroke-width="1.6"/>
          <line x1="18" y1="24" x2="18" y2="34" stroke="${c}" stroke-width="1.6"/>
          <line x1="2" y1="18" x2="12" y2="18" stroke="${c}" stroke-width="1.6"/>
          <line x1="24" y1="18" x2="34" y2="18" stroke="${c}" stroke-width="1.6"/>
          <circle cx="18" cy="18" r="1.8" fill="${c}"/>
        </svg>
      </div>`,
      iconSize: [36, 36],
      iconAnchor: [18, 18],
    });
  });
}

function nodeIcon(n: MeshNodeState, selected: boolean) {
  if (n.role === "gateway") return gatewayIcon(n.status, selected);
  if (n.role === "edge") return edgeIcon(n.status, selected);
  return virtualIcon(n.status, selected, n.holding ? n.hold_reason : null);
}

const BUCKET_COLOR = Object.fromEntries(RSSI_BUCKETS.map((b) => [b.key, b.color]));

/** Tree links are next hops, so the link color is the child node's next-hop band. */
const linkColor = (rssi: number) => BUCKET_COLOR[rssiBucket(rssi)];

/* ------------------------------------------------------------------ */
/* Map helpers                                                         */
/* ------------------------------------------------------------------ */

function ClickCapture({ onMapClick }: { onMapClick: (p: LatLng) => void }) {
  useMapEvents({
    click(e) {
      onMapClick([e.latlng.lat, e.latlng.lng]);
    },
  });
  return null;
}

const NodeMarker = memo(function NodeMarker({
  node,
  selected,
  onNodeClick,
}: {
  node: MeshNodeState;
  selected: boolean;
  onNodeClick: (id: string) => void;
}) {
  const handlers = useMemo(() => ({ click: () => onNodeClick(node.id) }), [node.id, onNodeClick]);
  return (
    <Marker
      position={node.coords}
      icon={nodeIcon(node, selected)}
      eventHandlers={handlers}
      zIndexOffset={node.type === "hardware" ? 1000 : selected ? 800 : 0}
    >
      <Tooltip direction="top" offset={[0, -10]} className="hud-tooltip">
        <div className="font-mono text-[10px]">
          <div className="font-semibold">
            [{roleLabel(node)}] {node.id}
          </div>
          <div>
            {node.status} · CH {node.channel} · next hop {node.rssi !== null ? `${node.rssi.toFixed(0)} dBm` : "--"}
            {node.parent_id ? ` via ${node.parent_id}` : ""} · {node.reachable ? `${node.hop_count} hop(s)` : "no route"}
          </div>
          {node.ground_rssi !== null && node.parent_id !== null && isUavId(node.parent_id) && (
            <div style={{ color: COVER_COLOR }}>UAV coverage · ground next hop would be {node.ground_rssi.toFixed(0)} dBm</div>
          )}
          {node.holding && (
            <div style={{ color: node.hold_reason ? HOLD_COLORS[node.hold_reason] : undefined }}>
              {node.hold_reason === "RECONNECTING" ? "RECONNECTING · resuming orbit" : "LOST LINK · holding position"}
              {node.lost_link_s !== null ? ` · ${node.lost_link_s.toFixed(0)} s` : ""}
            </div>
          )}
          {node.stranded && (
            <div style={{ color: STRANDED_COLOR }}>
              STRANDED on CH {node.channel} · never heard CH_SWITCH
              {node.stranded_s !== null ? ` · ${node.stranded_s.toFixed(0)} s` : ""} · awaiting Shepherd / beacon hunt
            </div>
          )}
          {!node.reachable && node.role !== "gateway" && (
            <div className="text-slate-400">
              Gateway datum: LKGP {node.lkgp[0].toFixed(5)}, {node.lkgp[1].toFixed(5)} ({node.lkgp_age_s.toFixed(0)} s old)
            </div>
          )}
        </div>
      </Tooltip>
    </Marker>
  );
});

/** Dashed uncertainty ring at the Last Known Good Position of a silent / stranded node. */
const LkgpGhost = memo(function LkgpGhost({ node, activeChannel }: { node: MeshNodeState; activeChannel: number }) {
  const stranded = node.stranded;
  const color = stranded ? STRANDED_COLOR : STATUS_COLORS.JAMMED;
  // the datum ages: the ring grows with uncertainty (orbit speed ~3 m/s), capped for readability
  const radius = Math.min(60, 12 + node.lkgp_age_s * 0.6);
  return (
    <>
      <Circle
        center={node.lkgp}
        radius={radius}
        className="lkgp-ring"
        pathOptions={{ color, weight: 1.4, opacity: 0.85, dashArray: "4 6", fillColor: color, fillOpacity: 0.06 }}
        interactive={false}
      >
        {stranded && (
          <Tooltip permanent direction="bottom" offset={[0, 6]} className="hud-tooltip hud-tooltip-amber">
            STRANDED CH {node.channel} (LKGP)
            {activeChannel !== node.channel ? ` · mesh on CH ${activeChannel}` : ""}
          </Tooltip>
        )}
      </Circle>
      <CircleMarker center={node.lkgp} radius={2.5} pathOptions={{ color, weight: 1, fillColor: color, fillOpacity: 0.9 }} interactive={false} />
    </>
  );
});

const UavMarker = memo(function UavMarker({
  uav,
  cfg,
  selected,
  pinLabel,
  mastActive,
  onNodeClick,
}: {
  uav: UavState;
  cfg: FleetState["config"];
  selected: boolean;
  pinLabel: boolean;
  /** this UAV is the HIGH-MAST proxy currently keeping the gateway alive */
  mastActive: boolean;
  onNodeClick: (id: string) => void;
}) {
  const handlers = useMemo(() => ({ click: () => onNodeClick(uav.id) }), [uav.id, onNodeClick]);
  const airborne = uav.flight_state !== "LANDED";
  const alert = uav.failsafe !== null || uav.battery_pct <= cfg.relief_battery_pct;
  const special = airborne && (uav.task === "MAST" || uav.task === "SHEPHERD");
  const permanent = selected || special || (airborne && pinLabel && uav.dist_home_m > PAD_LABEL_CLEARANCE_M);
  const tooltipTone = uav.task === "MAST" ? "hud-tooltip-sky" : uav.task === "SHEPHERD" ? "hud-tooltip-emerald" : "hud-tooltip-violet";
  return (
    <Marker
      position={uav.coords}
      icon={uavIcon(uav.mode, uav.type, uav.airframe, uav.bearing_deg, airborne, selected, alert)}
      eventHandlers={handlers}
      zIndexOffset={airborne ? 2000 : 900}
    >
      <Tooltip
        key={permanent ? "pinned" : "hover"}
        permanent={permanent}
        direction="bottom"
        offset={[0, 14]}
        className={`hud-tooltip ${tooltipTone}`}
      >
        <span className="font-mono text-[10px]">
          {uav.task === "MAST" && airborne && (
            <span className="block font-semibold" style={{ color: MAST_COLOR }}>
              {mastActive ? `HIGH-MAST PROXY (${cfg.mast_alt_m.toFixed(0)} m)` : `HIGH-MAST CLIMBING ${uav.alt_m.toFixed(0)} / ${cfg.mast_alt_m.toFixed(0)} m`}
            </span>
          )}
          {uav.task === "SHEPHERD" && airborne && (
            <span className="block font-semibold" style={{ color: SHEPHERD_COLOR }}>
              SHEPHERD · burn-through {shepherdFootprintM(uav.alt_m).toFixed(0)} m footprint
            </span>
          )}
          {uav.id} {uav.type} · {uav.mode} · {airborne ? `${uav.alt_m.toFixed(0)} m` : FLIGHT_STATE_LABEL[uav.flight_state]}
          {uav.slot_label ? ` · ${uav.slot_label}` : ""}
          {" · "}
          <span style={{ color: batteryColor(uav.battery_pct, cfg) }}>{uav.battery_pct.toFixed(0)}%</span>
          {uav.failsafe ? ` · ${uav.failsafe}` : ""}
        </span>
      </Tooltip>
    </Marker>
  );
});

/** Above this many airborne UAVs, labels show on hover only to keep the map readable. */
const PINNED_LABEL_LIMIT = 12;
/** UAVs climbing out of / descending onto the pad cluster stay hover-only so labels don't stack. */
const PAD_LABEL_CLEARANCE_M = 80;

/* ------------------------------------------------------------------ */
/* Main map                                                            */
/* ------------------------------------------------------------------ */

function MeshMap({
  snapshot,
  animations,
  mode,
  draftPolygon,
  zonePreviewRadius,
  selectedId,
  selectedUavId,
  onMapClick,
  onNodeClick,
}: {
  snapshot: Snapshot | null;
  animations: ActiveAnimation[];
  mode: InteractionMode;
  draftPolygon: LatLng[];
  zonePreviewRadius: number;
  selectedId: string | null;
  selectedUavId: string | null;
  onMapClick: (p: LatLng) => void;
  onNodeClick: (id: string) => void;
}) {
  const [map, setMap] = useState<L.Map | null>(null);
  const fittedRef = useRef(false);

  const nodes = useMemo(() => snapshot?.nodes ?? [], [snapshot]);
  const groundNodes = useMemo(() => nodes.filter((n) => n.type !== "uav"), [nodes]);
  const links = useMemo(() => snapshot?.links ?? [], [snapshot]);
  const threat = snapshot?.threat;
  const fleet = snapshot?.fleet;
  const swarm = snapshot?.swarm;
  const gateway = snapshot?.gateway.coords;
  const uavs = useMemo(() => fleet?.uavs ?? [], [fleet]);
  const pinLabels = (fleet?.counts.airborne ?? 0) <= PINNED_LABEL_LIMIT;
  const activeChannel = snapshot?.network.channel ?? 6;
  /** silent or stranded ground nodes: the gateway only knows their LKGP */
  const ghostNodes = useMemo(
    () => groundNodes.filter((n) => n.role !== "gateway" && (n.stranded || n.status === "JAMMED" || (!n.reachable && n.status !== "OFFLINE"))),
    [groundNodes],
  );
  const barrage = !!threat?.active && threat.barrage;
  const zoneColor = threat?.active ? ZONE_MODE_META[threat.mode].color : "#f43f5e";

  const coordById = useMemo(() => {
    const m = new Map<string, LatLng>();
    for (const n of nodes) m.set(n.id, n.coords);
    for (const u of uavs) m.set(u.id, u.coords);
    return m;
  }, [nodes, uavs]);

  const ringGapTooWide = useMemo(() => {
    if (!swarm) return [] as boolean[];
    return swarm.radii.map((r, i) => (i === 0 ? r : r - swarm.radii[i - 1]) > swarm.link_range_m);
  }, [swarm]);

  const selectedUav = selectedUavId ? uavs.find((u) => u.id === selectedUavId) : undefined;

  const fitAll = useCallback(() => {
    if (!map || coordById.size < 2) return;
    map.fitBounds(L.latLngBounds([...coordById.values()]), { padding: [36, 36], maxZoom: 17 });
  }, [map, coordById]);

  useEffect(() => {
    if (fittedRef.current || !map || coordById.size < 2) return;
    map.fitBounds(L.latLngBounds([...coordById.values()]), { padding: [36, 36], maxZoom: 17 });
    fittedRef.current = true;
  }, [map, coordById]);

  useEffect(() => {
    if (!map) return;
    if (mode === "none") map.doubleClickZoom.enable();
    else map.doubleClickZoom.disable();
  }, [map, mode]);

  const selectedNode = selectedId ? nodes.find((n) => n.id === selectedId) : undefined;
  const selectedRoute = useMemo(() => {
    if (!selectedNode || selectedNode.hop_path.length < 2) return null;
    const pts = selectedNode.hop_path.map((id) => coordById.get(id)).filter((p): p is LatLng => !!p);
    return pts.length >= 2 ? pts : null;
  }, [selectedNode, coordById]);

  return (
    <div className={`relative h-full w-full ${mode !== "none" ? "map-crosshair" : ""}`}>
      <MapContainer
        ref={setMap}
        center={snapshot?.gateway.coords ?? DEFAULT_CENTER}
        zoom={DEFAULT_ZOOM}
        className="h-full w-full"
        style={{ background: "#0a0e17" }}
        zoomControl
      >
        <TileLayer
          url="https://tiles.stadiamaps.com/tiles/alidade_smooth_dark/{z}/{x}/{y}{r}.png"
          attribution='&copy; <a href="https://stadiamaps.com/">Stadia Maps</a> &copy; <a href="https://www.openstreetmap.org/copyright">OSM</a>'
          maxZoom={20}
        />
        <ClickCapture onMapClick={onMapClick} />

        {/* UAV geofence */}
        {fleet && (
          <Circle
            center={fleet.geofence.center}
            radius={fleet.geofence.radius_m}
            pathOptions={{
              color: "#a78bfa",
              weight: mode === "uav_goto" ? 1.8 : 1,
              opacity: mode === "uav_goto" ? 0.8 : 0.35,
              dashArray: "2 8",
              fill: mode === "uav_goto",
              fillColor: "#a78bfa",
              fillOpacity: 0.03,
            }}
            interactive={false}
          />
        )}

        {/* Orbit rings */}
        {swarm?.mode === "ORBIT_PATROL" &&
          gateway &&
          swarm.radii.map((r, i) => (
            <Circle
              key={`ring-${i}`}
              center={gateway}
              radius={r}
              pathOptions={{
                color: ringGapTooWide[i] ? "#f59e0b" : "#22d3ee",
                weight: 1.2,
                opacity: ringGapTooWide[i] ? 0.7 : 0.4,
                dashArray: "3 7",
                fill: false,
              }}
              interactive={false}
            />
          ))}

        {/* Deployment polygon */}
        {snapshot?.swarm.polygon && snapshot.swarm.mode === "POLYGON_INFILL" && (
          <Polygon
            positions={snapshot.swarm.polygon}
            pathOptions={{ color: "#22d3ee", weight: 1.5, dashArray: "6 6", fillColor: "#22d3ee", fillOpacity: 0.04 }}
            interactive={false}
          />
        )}
        {draftPolygon.length > 0 && (
          <>
            <Polyline
              positions={draftPolygon.length >= 3 ? [...draftPolygon, draftPolygon[0]] : draftPolygon}
              pathOptions={{ color: "#fbbf24", weight: 2, dashArray: "4 6" }}
              interactive={false}
            />
            {draftPolygon.map((p, i) => (
              <CircleMarker
                key={`${p[0]}-${p[1]}-${i}`}
                center={p}
                radius={4}
                pathOptions={{ color: "#fbbf24", fillColor: "#0a0e17", fillOpacity: 1, weight: 2 }}
                interactive={false}
              />
            ))}
          </>
        )}

        {/* Interference zone */}
        {threat?.active && threat.epicenter && (
          <>
            <Circle
              center={threat.epicenter}
              radius={threat.bleed_radius_m}
              pathOptions={{ color: "#f59e0b", weight: 1.5, dashArray: "8 8", fillColor: "#f59e0b", fillOpacity: 0.05 }}
              interactive={false}
            >
              <Tooltip permanent direction="top" className="hud-tooltip hud-tooltip-amber" offset={[0, -6]}>
                FRINGE ATTENUATION ZONE {Math.round(threat.bleed_radius_m)} m
              </Tooltip>
            </Circle>
            <Circle
              center={threat.epicenter}
              radius={threat.kill_radius_m}
              className={barrage ? "zone-barrage" : "zone-kill"}
              pathOptions={{ color: zoneColor, weight: 2, fillColor: zoneColor, fillOpacity: 0.16 }}
              interactive={false}
            >
              <Tooltip permanent direction="center" className={`hud-tooltip ${barrage ? "hud-tooltip-violet" : "hud-tooltip-red"}`}>
                {barrage ? "BARRAGE DROP ZONE" : threat.mode === "SWEEPER" ? "SWEEPER DROP ZONE" : "PRIMARY DROP ZONE"} {Math.round(threat.kill_radius_m)} m ·{" "}
                {barrage ? "CH 1-11" : `CH ${threat.channel}`}
                {threat.sweeper_next_hop_s !== null ? ` · hop ${threat.sweeper_next_hop_s.toFixed(0)} s` : ""}
              </Tooltip>
            </Circle>
            {/* Safe perimeter flanking arc: relay slots inside the kill radius are pushed out here */}
            {barrage && fleet && (
              <Circle
                center={threat.epicenter}
                radius={threat.kill_radius_m + fleet.config.flank_margin_m}
                pathOptions={{ color: "#a78bfa", weight: 1.2, opacity: 0.7, dashArray: "10 6", fill: false }}
                interactive={false}
              >
                <Tooltip permanent direction="right" offset={[0, 0]} className="hud-tooltip hud-tooltip-violet">
                  FLANKING ARC +{fleet.config.flank_margin_m.toFixed(0)} m
                </Tooltip>
              </Circle>
            )}
            {threat.estimated_epicenter && (
              <Marker position={threat.estimated_epicenter} icon={crosshairIcon("est")} interactive={false} zIndexOffset={1500}>
                <Tooltip permanent direction="bottom" offset={[0, 14]} className="hud-tooltip hud-tooltip-red">
                  EST. CENTER (LKGP tomography)
                  {threat.estimation_error_m !== null ? ` ±${threat.estimation_error_m.toFixed(0)} m` : ""}
                </Tooltip>
              </Marker>
            )}
            {/* HIGH-MAST vertical burn-through link over the gateway */}
            {threat.mast_active && gateway && (
              <Circle
                center={gateway}
                radius={MAST_HORIZONTAL_RADIUS_M}
                pathOptions={{ color: MAST_COLOR, weight: 2, opacity: 0.9, dashArray: "3 4", fillColor: MAST_COLOR, fillOpacity: 0.12 }}
                interactive={false}
              >
                <Tooltip permanent direction="top" offset={[0, -8]} className="hud-tooltip hud-tooltip-sky">
                  VERTICAL BURN-THROUGH · {threat.mast_uav_id} @ {fleet?.config.mast_alt_m.toFixed(0) ?? 95} m
                </Tooltip>
              </Circle>
            )}
          </>
        )}

        {/* LKGP ghosts: the gateway's datum for every silent / stranded node */}
        {ghostNodes.map((n) => (
          <LkgpGhost key={`lkgp-${n.id}`} node={n} activeChannel={activeChannel} />
        ))}

        {/* Dijkstra tree links */}
        {links.map((l) => {
          const a = coordById.get(l.source);
          const b = coordById.get(l.target);
          if (!a || !b) return null;
          const viaRelay = isUavId(l.source) || isUavId(l.target);
          return (
            <Polyline
              key={`${l.source}>${l.target}`}
              positions={[a, b]}
              pathOptions={{
                color: viaRelay ? "#a78bfa" : linkColor(l.rssi),
                weight: viaRelay ? 2 : 1.4,
                opacity: viaRelay ? 0.9 : 0.5,
                dashArray: viaRelay ? "5 5" : undefined,
              }}
              interactive={false}
            />
          );
        })}

        {selectedRoute && (
          <Polyline positions={selectedRoute} pathOptions={{ color: "#34d399", weight: 3.5, opacity: 0.85 }} interactive={false} />
        )}

        {/* Packet animations */}
        {animations.map((anim) => {
          const pts = anim.path.map((id) => coordById.get(id)).filter((p): p is LatLng => !!p);
          if (pts.length < 2) return null;
          const isP2p = anim.direction === "p2p";
          const color = isP2p ? "#e879f9" : anim.direction === "outbound" ? "#22d3ee" : "#fbbf24";
          return (
            <Polyline
              key={anim.id}
              positions={pts}
              className={`packet-flow packet-${anim.direction}`}
              pathOptions={{
                color,
                weight: isP2p ? 3 : 4,
                opacity: 0.95,
                dashArray: isP2p ? "6 4" : undefined,
              }}
              interactive={false}
            />
          );
        })}

        {/* Relay fleet: home pads, allocator slots, flight paths */}
        {uavs.map((u) => (
          <CircleMarker
            key={`pad-${u.id}`}
            center={u.home}
            radius={3}
            pathOptions={{ color: "#7c3aed", weight: 1, opacity: 0.6, fillColor: "#7c3aed", fillOpacity: 0.25 }}
            interactive={false}
          />
        ))}
        {fleet?.allocator.coverage.areas.map((a) => (
          <Circle
            key={`cover-${a.label}`}
            center={a.centroid}
            radius={fleet.allocator.coverage.plan_radius_m}
            pathOptions={{ color: COVER_COLOR, weight: 1, opacity: 0.45, dashArray: "2 6", fillColor: COVER_COLOR, fillOpacity: 0.04 }}
            interactive={false}
          />
        ))}
        {uavs.map((u) =>
          u.task === "COVER" && u.flight_state === "ON_STATION" && u.cover_radius_m ? (
            <Circle
              key={`footprint-${u.id}`}
              center={u.coords}
              radius={u.cover_radius_m}
              pathOptions={{ color: uavTypeColor(u.type), weight: 0.8, opacity: 0.35, fillColor: uavTypeColor(u.type), fillOpacity: 0.06 }}
              interactive={false}
            />
          ) : null,
        )}
        {/* Shepherd burn-through footprint: ground disc of the slant-range cone under the UAV */}
        {uavs.map((u) =>
          u.task === "SHEPHERD" && u.flight_state !== "LANDED" && u.alt_m >= 10 ? (
            <Circle
              key={`shepherd-${u.id}`}
              center={u.coords}
              radius={shepherdFootprintM(u.alt_m)}
              className="shepherd-footprint"
              pathOptions={{ color: SHEPHERD_COLOR, weight: 1.6, fillColor: SHEPHERD_COLOR, fillOpacity: 0.1 }}
              interactive={false}
            />
          ) : null,
        )}
        {/* Flanking arcs: dashed hop from the unsafe midpoint to the perimeter slot */}
        {fleet?.allocator.slots.map((s) =>
          s.flanked && s.flank_from ? (
            <Polyline
              key={`flank-${s.label}`}
              positions={[s.flank_from, s.coords]}
              pathOptions={{ color: "#a78bfa", weight: 1.4, opacity: 0.7, dashArray: "3 5" }}
              interactive={false}
            />
          ) : null,
        )}
        {fleet?.allocator.slots.map((s) => (
          <Marker key={`slot-${s.kind}-${s.label}`} position={s.coords} icon={slotIcon(s.kind, s.assigned_to !== null)} zIndexOffset={1100}>
            <Tooltip
              direction="top"
              offset={[0, -8]}
              className={`hud-tooltip ${
                s.kind === "COVER" ? "hud-tooltip-sky" : s.kind === "SHEPHERD" ? "hud-tooltip-emerald" : s.kind === "MAST" ? "hud-tooltip-sky" : "hud-tooltip-violet"
              }`}
            >
              {s.kind === "COVER"
                ? `COVERAGE ${s.label} · ${s.size} weak next hop(s)`
                : s.kind === "SHEPHERD"
                  ? `SHEPHERD ${s.label} · ${s.size} stranded node(s) at LKGP`
                  : s.kind === "MAST"
                    ? `HIGH-MAST ${s.label} · ${s.alt_m?.toFixed(0) ?? 95} m over gateway`
                    : `RELAY SLOT ${s.label}${s.flanked ? " · FLANK ARC (pushed out of kill zone)" : ""}`}
              {` · ${s.assigned_to ?? "unassigned"}`}
            </Tooltip>
          </Marker>
        ))}
        {uavs.map((u) =>
          u.target && u.flight_state !== "LANDED" ? (
            <Polyline
              key={`path-${u.id}`}
              positions={[u.coords, u.target]}
              pathOptions={{
                color: u.id === selectedUavId ? "#38bdf8" : FLIGHT_MODE_COLORS[u.mode],
                weight: u.id === selectedUavId ? 2.2 : 1.3,
                dashArray: "6 6",
                opacity: u.id === selectedUavId ? 0.95 : 0.55,
              }}
              interactive={false}
            />
          ) : null,
        )}
        {selectedUav?.target && selectedUav.flight_state !== "LANDED" && (
          <Marker position={selectedUav.target} icon={crosshairIcon("wp")} interactive={false} zIndexOffset={1200}>
            <Tooltip permanent direction="right" offset={[16, 0]} className="hud-tooltip hud-tooltip-sky">
              {selectedUav.id} TARGET · {selectedUav.cruise_alt_m.toFixed(0)} m
              {selectedUav.eta_s !== null ? ` · ETA ${selectedUav.eta_s.toFixed(0)} s` : ""}
            </Tooltip>
          </Marker>
        )}

        {groundNodes.map((n) => (
          <NodeMarker key={n.id} node={n} selected={n.id === selectedId} onNodeClick={onNodeClick} />
        ))}

        {fleet &&
          uavs.map((u) => (
            <UavMarker
              key={u.id}
              uav={u}
              cfg={fleet.config}
              selected={u.id === selectedUavId}
              pinLabel={pinLabels}
              mastActive={!!threat?.mast_active && threat.mast_uav_id === u.id}
              onNodeClick={onNodeClick}
            />
          ))}
      </MapContainer>

      {mode !== "none" && (
        <div className="pointer-events-none absolute left-1/2 top-3 z-[1000] -translate-x-1/2 rounded border border-amber-500/50 bg-[#0a0e17]/90 px-3 py-1.5 text-[10px] font-semibold tracking-wider text-amber-200">
          {mode === "draw_polygon"
            ? "DRAW COVERAGE AREA: click to add vertices, then FINISH AREA"
            : mode === "uav_goto"
              ? `FLY ${selectedUavId ?? "UAV"} TO: click inside the ${fleet?.geofence.radius_m ?? 1500} m geofence`
              : `PLACE INTERFERENCE ZONE: click map (primary ${zonePreviewRadius} m / fringe ${Math.round(zonePreviewRadius * BLEED_RATIO)} m)`}
        </div>
      )}
      <div className="pointer-events-none absolute left-3 top-3 z-[1000] flex flex-col gap-1">
        {(swarm?.holding_count ?? 0) > 0 && (
          <div className="rounded border border-rose-500/40 bg-[#0a0e17]/90 px-2.5 py-1 text-[10px] font-semibold tracking-wider text-rose-200">
            {swarm?.holding_count} NODE{swarm?.holding_count === 1 ? "" : "S"} HOLDING POSITION (LOST LINK)
          </div>
        )}
        {(threat?.stranded_count ?? 0) > 0 && (
          <div className="rounded border border-amber-500/40 bg-[#0a0e17]/90 px-2.5 py-1 text-[10px] font-semibold tracking-wider text-amber-200">
            {threat?.stranded_count} STRANDED ON OLD CHANNEL · {threat?.shepherd_assigned ?? 0} SHEPHERD UAV{threat?.shepherd_assigned === 1 ? "" : "S"} INBOUND
          </div>
        )}
        {threat?.gateway_in_zone && (
          <div
            className={`rounded border bg-[#0a0e17]/90 px-2.5 py-1 text-[10px] font-semibold tracking-wider ${
              threat.mast_active ? "border-sky-500/40 text-sky-200" : "border-rose-500/40 text-rose-200"
            }`}
          >
            {threat.mast_active ? `PROTOCOL HIGH-MAST ACTIVE · ${threat.mast_uav_id} PROXYING GATEWAY` : `GATEWAY ${threat.gateway_rf_state} · PROTOCOL HIGH-MAST ENGAGED`}
          </div>
        )}
      </div>

      <div className="absolute bottom-3 right-3 z-[1000] flex flex-col items-end gap-1.5">
        <button
          type="button"
          onClick={fitAll}
          className="rounded border border-slate-700 bg-[#0a0e17]/90 px-2.5 py-1 text-[10px] font-semibold tracking-wider text-slate-300 hover:border-cyan-500/60 hover:text-cyan-300"
        >
          RECENTER
        </button>
        <div className="rounded border border-slate-800 bg-[#0a0e17]/90 px-2.5 py-1.5 text-[9.5px] leading-relaxed text-slate-400">
          <div><span className="text-amber-300">◆</span> HW EDGE &nbsp;<span className="text-yellow-300">⬢</span> GATEWAY</div>
          <div><span className="text-teal-300">▲</span> ONLINE <span className="text-amber-300">▲</span> DEGRADED <span className="text-rose-400">▲</span> JAMMED</div>
          <div><span className="text-rose-400">⬚</span> LOST-LINK HOLD <span className="text-sky-300">⬚</span> RECONNECTING</div>
          <div><span className="text-violet-300">✚</span> UAV <span className="text-violet-300">◇</span> RELAY SLOT <span className="text-cyan-300">◯</span> COVERAGE AREA</div>
          <div><span className="text-emerald-300">△</span> SHEPHERD SLOT / <span className="text-emerald-300">◉</span> BURN-THROUGH <span className="text-sky-300">▣</span> HIGH-MAST</div>
          <div><span className="text-amber-300">◌</span> LKGP STRANDED <span className="text-rose-400">◌</span> LKGP SILENT <span className="text-violet-300">┅</span> FLANK ARC</div>
          <div>
            NEXT HOP <span className="text-emerald-400">━</span> EXCELLENT <span className="text-cyan-300">━</span> GOOD{" "}
            <span className="text-amber-300">━</span> DEGRADED <span className="text-violet-300">┅</span> VIA UAV
          </div>
        </div>
      </div>
    </div>
  );
}

export default memo(MeshMap);
