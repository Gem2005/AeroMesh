"use client";

/**
 * TacticalMap — Leaflet-based geographical tactical display for AeroMesh.
 *
 * Renders mesh nodes as custom glowing divIcon markers on dark tiles,
 * with Polyline links and UAV relay injection animations. All GPS
 * coordinates come pre-calculated from the Python/NetworkX bridge.
 *
 * This component is loaded via next/dynamic (ssr: false) because Leaflet
 * requires the DOM. Re-renders are minimized via React.memo: the map
 * instance persists, only markers/polylines update on topology changes.
 *
 * Node click events are forwarded to the parent via onNodeClick callback
 * so the inspector panel lives OUTSIDE the MapContainer (preventing
 * zoom resets on selectedNode state changes).
 */

import { memo, useEffect, useMemo, useRef } from "react";
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
  type: "gateway" | "online" | "jammed" | "relay"
): L.DivIcon {
  const classes: Record<string, string> = {
    gateway: "marker-gateway",
    online: "marker-online",
    jammed: "marker-jammed",
    relay: "marker-relay",
  };

  const labels: Record<string, string> = {
    gateway: "GW",
    online: "",
    jammed: "JAM",
    relay: "▲",
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
};

/* ------------------------------------------------------------------ */
/* Map auto-fit helper                                                 */
/* ------------------------------------------------------------------ */

function MapAutoFit({ nodes }: { nodes: TopoNode[] }) {
  const map = useMap();
  const fitted = useRef(false);

  useEffect(() => {
    if (nodes.length < 2 || fitted.current) return;
    const bounds = L.latLngBounds(nodes.map((n) => [n.lat, n.lon]));
    map.fitBounds(bounds.pad(0.3), { maxZoom: 18 });
    fitted.current = true;
  }, [nodes, map]);

  return null;
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
  onNodeClick?: (node: TopoNode) => void;
}

function TacticalMap({ center, zoom, nodes, links, relay, onNodeClick }: TacticalMapProps) {
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

      <MapAutoFit nodes={nodes} />

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
