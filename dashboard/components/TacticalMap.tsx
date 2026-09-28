"use client";

/**
 * TacticalMap — Leaflet-based geographical tactical display for AeroMesh.
 *
 * Renders mesh nodes as custom glowing divIcon markers on dark tiles,
 * with Polyline links, UAV relay injection, and operator position.
 *
 * Features:
 *   - Operator marker: shows the presenter's laptop GPS location
 *   - Auto-zoom: fits all visible entities (nodes + operator) on every
 *     topology update so distances are shown accurately
 *   - Recenter button: snaps the map back to fit all entities
 *   - Node click: forwarded to parent via onNodeClick callback
 */

import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  MapContainer,
  TileLayer,
  Marker,
  Polyline,
  Tooltip,
  useMap,
} from "react-leaflet";
import L from "leaflet";
import "leaflet/dist/leaflet.css";

import {
  type TopoNode,
  type TopoLink,
  type AerialRelay,
  GATEWAY_ID,
} from "@/lib/mesh";

/* ------------------------------------------------------------------ */
/* Marker icon factories                                               */
/* ------------------------------------------------------------------ */

function createNodeIcon(
  type: "gateway" | "online" | "jammed" | "relay" | "operator"
): L.DivIcon {
  const classes: Record<string, string> = {
    gateway: "marker-gateway",
    online: "marker-online",
    jammed: "marker-jammed",
    relay: "marker-relay",
    operator: "marker-operator",
  };

  const labels: Record<string, string> = {
    gateway: "GW",
    online: "",
    jammed: "JAM",
    relay: "▲",
    operator: "OP",
  };

  return L.divIcon({
    className: "", // suppress default leaflet-div-icon styling
    html: `<div class="tactical-marker ${classes[type]}">
             <div class="marker-core">${labels[type]}</div>
             <div class="marker-ring"></div>
             <div class="marker-pulse"></div>
           </div>`,
    iconSize: [40, 40],
    iconAnchor: [20, 20],
  });
}

const ICONS = {
  gateway: createNodeIcon("gateway"),
  online: createNodeIcon("online"),
  jammed: createNodeIcon("jammed"),
  relay: createNodeIcon("relay"),
  operator: createNodeIcon("operator"),
};

/* ------------------------------------------------------------------ */
/* Map controller: auto-zoom + recenter                                */
/* ------------------------------------------------------------------ */

function MapController({
  nodes,
  operatorPosition,
  relay,
}: {
  nodes: TopoNode[];
  operatorPosition: [number, number] | null;
  relay: AerialRelay | null;
}) {
  const map = useMap();
  const [userInteracted, setUserInteracted] = useState(false);
  const prevSignature = useRef("");

  /** Collect all visible points into a LatLngBounds. */
  const computePoints = useCallback((): [number, number][] => {
    const points: [number, number][] = [];
    if (operatorPosition) points.push(operatorPosition);
    for (const n of nodes) points.push([n.lat, n.lon]);
    if (relay) points.push([relay.lat, relay.lon]);
    return points;
  }, [nodes, operatorPosition, relay]);

  /** Fit all entities in view with distance-calculated zoom and comfortable padding. */
  const fitAllEntities = useCallback(
    (smooth = true) => {
      const points = computePoints();
      if (points.length === 0) return;

      if (points.length === 1) {
        if (smooth) {
          map.flyTo(points[0], 17, { duration: 0.8 });
        } else {
          map.setView(points[0], 17);
        }
      } else {
        const bounds = L.latLngBounds(points);
        map.fitBounds(bounds.pad(0.18), { maxZoom: 18, animate: smooth });
      }
      setUserInteracted(false);
    },
    [computePoints, map]
  );

  /** Auto-zoom on network topology or operator position changes when user hasn't panned away. */
  useEffect(() => {
    const nodeSig = nodes
      .map((n) => `${n.id}:${n.lat.toFixed(4)},${n.lon.toFixed(4)}`)
      .sort()
      .join(";");
    const opSig = operatorPosition
      ? `${operatorPosition[0].toFixed(4)},${operatorPosition[1].toFixed(4)}`
      : "none";
    const relaySig = relay ? `${relay.lat.toFixed(4)},${relay.lon.toFixed(4)}` : "none";
    const currentSignature = `${opSig}|${nodeSig}|${relaySig}`;

    if (currentSignature === prevSignature.current) return;
    const isFirstTime = prevSignature.current === "";
    prevSignature.current = currentSignature;

    // Auto-fit immediately on first entity acquisition or if presenter hasn't manually panned
    if (isFirstTime || !userInteracted) {
      fitAllEntities(true);
    }
  }, [nodes, operatorPosition, relay, userInteracted, fitAllEntities]);

  /** Track manual pan or zoom so auto-zoom doesn't fight the presenter. */
  useEffect(() => {
    const onUserInteraction = () => setUserInteracted(true);
    map.on("dragstart", onUserInteraction);
    map.on("zoomstart", onUserInteraction);
    return () => {
      map.off("dragstart", onUserInteraction);
      map.off("zoomstart", onUserInteraction);
    };
  }, [map]);

  /** Smoothly invalidate map size on container width change (sidebar expand/collapse) */
  useEffect(() => {
    const container = map.getContainer();
    if (!container) return;

    const ro = new ResizeObserver(() => {
      map.invalidateSize({ animate: false });
    });
    ro.observe(container);
    return () => ro.disconnect();
  }, [map]);

  return (
    <div className="leaflet-top leaflet-right" style={{ pointerEvents: "auto" }}>
      <div className="leaflet-control recenter-control">
        <button
          onClick={() => fitAllEntities(true)}
          className={`recenter-btn ${userInteracted ? "is-panned" : ""}`}
          title="Recenter view — auto-fit all active nodes and operator position"
          aria-label="Recenter map"
        >
          <svg
            width="16"
            height="16"
            viewBox="0 0 16 16"
            fill="none"
            xmlns="http://www.w3.org/2000/svg"
          >
            <circle cx="8" cy="8" r="3" stroke="currentColor" strokeWidth="1.5" />
            <line x1="8" y1="0" x2="8" y2="4" stroke="currentColor" strokeWidth="1.5" />
            <line x1="8" y1="12" x2="8" y2="16" stroke="currentColor" strokeWidth="1.5" />
            <line x1="0" y1="8" x2="4" y2="8" stroke="currentColor" strokeWidth="1.5" />
            <line x1="12" y1="8" x2="16" y2="8" stroke="currentColor" strokeWidth="1.5" />
          </svg>
          {userInteracted && <span className="recenter-indicator" />}
        </button>
      </div>
    </div>
  );
}

/** Interactive Operator Marker: zooms directly to laptop on click */
function OperatorMarker({
  position,
}: {
  position: [number, number];
}) {
  const map = useMap();
  return (
    <Marker
      position={position}
      icon={ICONS.operator}
      zIndexOffset={1000}
      eventHandlers={{
        click: () => {
          map.flyTo(position, 18, { duration: 0.8 });
        },
      }}
    >
      <Tooltip
        permanent
        direction="bottom"
        offset={[0, 18]}
        className="tactical-tooltip operator-tooltip"
      >
        <div className="tooltip-content">
          <span className="tooltip-id operator-label">OPERATOR (C2)</span>
          <span className="tooltip-coords">
            {position[0].toFixed(4)}, {position[1].toFixed(4)}
          </span>
        </div>
      </Tooltip>
    </Marker>
  );
}

/* ------------------------------------------------------------------ */
/* Component                                                           */
/* ------------------------------------------------------------------ */

interface TacticalMapProps {
  center: [number, number];
  zoom: number;
  nodes: TopoNode[];
  links: TopoLink[];
  relay: AerialRelay | null;
  operatorPosition: [number, number] | null;
  onNodeClick?: (node: TopoNode) => void;
}

function TacticalMap({
  center,
  zoom,
  nodes,
  links,
  relay,
  operatorPosition,
  onNodeClick,
}: TacticalMapProps) {
  /* Build a lookup for node positions by id. */
  const nodeMap = useMemo(() => {
    const map = new Map<number, TopoNode>();
    for (const n of nodes) map.set(n.id, n);
    return map;
  }, [nodes]);

  /* Resolve link endpoints to LatLng pairs. */
  const linkLines = useMemo(() => {
    const result: { key: string; positions: [number, number][]; isJammed: boolean }[] = [];
    for (const link of links) {
      const src = nodeMap.get(link.source);
      const tgt = nodeMap.get(link.target);
      if (!src || !tgt) continue;
      const isJammed = src.status === "JAMMED" || tgt.status === "JAMMED";
      result.push({
        key: `${link.source}-${link.target}`,
        positions: [
          [src.lat, src.lon],
          [tgt.lat, tgt.lon],
        ],
        isJammed,
      });
    }
    return result;
  }, [links, nodeMap]);

  /* UAV relay bridge lines (relay -> gateway, relay -> jammed node). */
  const relayLines = useMemo(() => {
    if (!relay) return [];
    const lines: { key: string; positions: [number, number][] }[] = [];
    const gateway = nodeMap.get(GATEWAY_ID);
    const target = nodeMap.get(relay.targetNodeId);

    if (gateway) {
      lines.push({
        key: `relay-gw`,
        positions: [
          [relay.lat, relay.lon],
          [gateway.lat, gateway.lon],
        ],
      });
    }
    if (target) {
      lines.push({
        key: `relay-target`,
        positions: [
          [relay.lat, relay.lon],
          [target.lat, target.lon],
        ],
      });
    }
    return lines;
  }, [relay, nodeMap]);

  return (
    <MapContainer
      center={center}
      zoom={zoom}
      className="h-full w-full"
      zoomControl={false}
      attributionControl={false}
      style={{ background: "#0a0f1a" }}
    >
      <TileLayer
        url="https://tiles.stadiamaps.com/tiles/alidade_smooth_dark/{z}/{x}/{y}{r}.png"
        maxZoom={20}
      />

      <MapController
        nodes={nodes}
        operatorPosition={operatorPosition}
        relay={relay}
      />

      {/* Operator position marker */}
      {operatorPosition && (
        <OperatorMarker position={operatorPosition} />
      )}

      {/* Mesh topology links */}
      {linkLines.map((line) => (
        <Polyline
          key={line.key}
          positions={line.positions}
          pathOptions={{
            color: line.isJammed ? "#f43f5e" : "#34d39988",
            weight: line.isJammed ? 2 : 1.5,
            opacity: line.isJammed ? 0.7 : 0.5,
            dashArray: line.isJammed ? "8 6" : undefined,
          }}
        />
      ))}

      {/* Node markers */}
      {nodes.map((node) => {
        const isGateway = node.id === GATEWAY_ID;
        const isJammed = node.status === "JAMMED";
        const icon = isGateway
          ? ICONS.gateway
          : isJammed
            ? ICONS.jammed
            : ICONS.online;

        return (
          <Marker
            key={node.id}
            position={[node.lat, node.lon]}
            icon={icon}
            eventHandlers={{
              click: () => onNodeClick?.(node),
            }}
          >
            <Tooltip
              permanent
              direction="bottom"
              offset={[0, 18]}
              className="tactical-tooltip"
            >
              <div className="tooltip-content">
                <span className="tooltip-id">
                  {isGateway ? "GATEWAY" : isJammed ? "JAMMED" : "NODE"}
                </span>
                <span className="tooltip-hex">{node.id.toString(16).toUpperCase()}</span>
                <span className="tooltip-coords">
                  {node.lat.toFixed(4)}, {node.lon.toFixed(4)}
                </span>
              </div>
            </Tooltip>
          </Marker>
        );
      })}

      {/* UAV Relay marker */}
      {relay && (
        <>
          <Marker
            position={[relay.lat, relay.lon]}
            icon={ICONS.relay}
          >
            <Tooltip
              permanent
              direction="bottom"
              offset={[0, 18]}
              className="tactical-tooltip relay-tooltip"
            >
              <div className="tooltip-content">
                <span className="tooltip-id relay-label">AERIAL_RELAY</span>
                <span className="tooltip-coords">
                  {relay.lat.toFixed(4)}, {relay.lon.toFixed(4)}
                </span>
              </div>
            </Tooltip>
          </Marker>

          {/* Relay bridge lines */}
          {relayLines.map((line) => (
            <Polyline
              key={line.key}
              positions={line.positions}
              pathOptions={{
                color: "#22d3ee",
                weight: 2.5,
                opacity: 0.8,
                dashArray: "6 4",
              }}
            />
          ))}
        </>
      )}
    </MapContainer>
  );
}

export default memo(TacticalMap);
