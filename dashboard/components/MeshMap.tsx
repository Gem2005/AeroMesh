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
  RELAY_ID,
  STATUS_COLORS,
  roleLabel,
  type LatLng,
  type MeshNodeState,
  type NodeStatus,
  type Snapshot,
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

function virtualIcon(status: NodeStatus, selected: boolean) {
  return cachedIcon(`v:${status}:${selected}`, () => {
    const c = status === "ONLINE" ? "#2dd4bf" : STATUS_COLORS[status];
    return L.divIcon({
      className: "",
      html: `<div class="mk mk-virtual mk-${status.toLowerCase()}${selected ? " mk-selected" : ""}" style="--mk:${c}">
        <svg viewBox="0 0 20 20" width="20" height="20">
          <path d="M10 2 L18 17 L10 12.6 L2 17 Z" fill="${c}" fill-opacity="0.85" stroke="${c}" stroke-width="1.2" stroke-linejoin="round"/>
        </svg>
      </div>`,
      iconSize: [20, 20],
      iconAnchor: [10, 10],
    });
  });
}

function relayIcon(state: string) {
  return cachedIcon(`relay:${state}`, () =>
    L.divIcon({
      className: "",
      html: `<div class="mk mk-relay${state !== "LOITERING" ? " mk-relay-active" : ""}">
        <svg viewBox="0 0 40 40" width="40" height="40">
          <g stroke="#a78bfa" stroke-width="2" fill="none">
            <line x1="11" y1="11" x2="29" y2="29"/><line x1="29" y1="11" x2="11" y2="29"/>
            <circle cx="11" cy="11" r="5" stroke="#38bdf8"/><circle cx="29" cy="11" r="5" stroke="#38bdf8"/>
            <circle cx="11" cy="29" r="5" stroke="#38bdf8"/><circle cx="29" cy="29" r="5" stroke="#38bdf8"/>
          </g>
          <rect x="15.5" y="15.5" width="9" height="9" rx="2" fill="#7c3aed" stroke="#c4b5fd" stroke-width="1.2"/>
        </svg>
      </div>`,
      iconSize: [40, 40],
      iconAnchor: [20, 20],
    }),
  );
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
  return virtualIcon(n.status, selected);
}

function linkColor(quality: number) {
  if (quality >= 0.5) return "#22d3ee";
  if (quality >= 0.2) return "#14b8a6";
  return "#f59e0b";
}

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
            {node.status} · CH {node.channel} · {node.rssi !== null ? `${node.rssi.toFixed(0)} dBm` : "--"} ·{" "}
            {node.reachable ? `${node.hop_count} hop(s)` : "no route"}
          </div>
        </div>
      </Tooltip>
    </Marker>
  );
});

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
  onMapClick,
  onNodeClick,
}: {
  snapshot: Snapshot | null;
  animations: ActiveAnimation[];
  mode: InteractionMode;
  draftPolygon: LatLng[];
  zonePreviewRadius: number;
  selectedId: string | null;
  onMapClick: (p: LatLng) => void;
  onNodeClick: (id: string) => void;
}) {
  const [map, setMap] = useState<L.Map | null>(null);
  const fittedRef = useRef(false);

  const nodes = useMemo(() => snapshot?.nodes ?? [], [snapshot]);
  const links = useMemo(() => snapshot?.links ?? [], [snapshot]);
  const threat = snapshot?.threat;
  const autopilot = snapshot?.autopilot;

  const coordById = useMemo(() => {
    const m = new Map<string, LatLng>();
    for (const n of nodes) m.set(n.id, n.coords);
    if (autopilot) m.set(RELAY_ID, autopilot.coords);
    return m;
  }, [nodes, autopilot]);

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

  const relayDispatched = autopilot && autopilot.mission_state !== "LOITERING" && autopilot.target_waypoint;

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
              className="zone-kill"
              pathOptions={{ color: "#f43f5e", weight: 2, fillColor: "#f43f5e", fillOpacity: 0.16 }}
              interactive={false}
            >
              <Tooltip permanent direction="center" className="hud-tooltip hud-tooltip-red">
                PRIMARY DROP ZONE {Math.round(threat.kill_radius_m)} m · CH {threat.channel}
              </Tooltip>
            </Circle>
            {threat.estimated_epicenter && (
              <Marker position={threat.estimated_epicenter} icon={crosshairIcon("est")} interactive={false} zIndexOffset={1500}>
                <Tooltip permanent direction="bottom" offset={[0, 14]} className="hud-tooltip hud-tooltip-red">
                  EST. INTERFERENCE CENTER
                  {threat.estimation_error_m !== null ? ` (±${threat.estimation_error_m.toFixed(0)} m)` : ""}
                </Tooltip>
              </Marker>
            )}
          </>
        )}

        {/* Dijkstra tree links */}
        {links.map((l) => {
          const a = coordById.get(l.source);
          const b = coordById.get(l.target);
          if (!a || !b) return null;
          const viaRelay = l.source === RELAY_ID || l.target === RELAY_ID;
          return (
            <Polyline
              key={`${l.source}>${l.target}`}
              positions={[a, b]}
              pathOptions={{
                color: viaRelay ? "#a78bfa" : linkColor(l.quality),
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
          const color = anim.direction === "outbound" ? "#22d3ee" : "#fbbf24";
          return (
            <Polyline
              key={anim.id}
              positions={pts}
              className={`packet-flow packet-${anim.direction}`}
              pathOptions={{ color, weight: 4, opacity: 0.95 }}
              interactive={false}
            />
          );
        })}

        {/* Mobile relay */}
        {relayDispatched && autopilot?.target_waypoint && (
          <>
            <Polyline
              positions={[autopilot.coords, autopilot.target_waypoint]}
              pathOptions={{ color: "#38bdf8", weight: 1.8, dashArray: "6 6", opacity: 0.9 }}
              interactive={false}
            />
            <Marker position={autopilot.target_waypoint} icon={crosshairIcon("wp")} interactive={false} zIndexOffset={1200}>
              <Tooltip permanent direction="right" offset={[16, 0]} className="hud-tooltip hud-tooltip-sky">
                WP-ALPHA · {autopilot.alt_m.toFixed(0)} m
              </Tooltip>
            </Marker>
          </>
        )}

        {nodes.map((n) => (
          <NodeMarker key={n.id} node={n} selected={n.id === selectedId} onNodeClick={onNodeClick} />
        ))}

        {autopilot && (
          <Marker position={autopilot.coords} icon={relayIcon(autopilot.mission_state)} zIndexOffset={2000}>
            <Tooltip
              key={autopilot.mission_state === "LOITERING" ? "hover" : "pinned"}
              permanent={autopilot.mission_state !== "LOITERING"}
              direction="bottom"
              offset={[0, 16]}
              className="hud-tooltip hud-tooltip-violet"
            >
              {RELAY_ID} · {autopilot.mission_state}
            </Tooltip>
          </Marker>
        )}
      </MapContainer>

      {mode !== "none" && (
        <div className="pointer-events-none absolute left-1/2 top-3 z-[1000] -translate-x-1/2 rounded border border-amber-500/50 bg-[#0a0e17]/90 px-3 py-1.5 text-[10px] font-semibold tracking-wider text-amber-200">
          {mode === "draw_polygon"
            ? "DRAW COVERAGE AREA: click to add vertices, then FINISH AREA"
            : `PLACE INTERFERENCE ZONE: click map (primary ${zonePreviewRadius} m / fringe ${Math.round(zonePreviewRadius * BLEED_RATIO)} m)`}
        </div>
      )}

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
          <div><span className="text-violet-300">✚</span> RELAY &nbsp;<span className="text-cyan-300">━</span> DIJKSTRA LINK</div>
        </div>
      </div>
    </div>
  );
}

export default memo(MeshMap);
