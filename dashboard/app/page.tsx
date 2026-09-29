"use client";

/**
 * AeroMesh Command — Palantir-inspired C2 tactical dashboard.
 *
 * Three interactive features for the 75% jury review:
 *
 *   1. TACTICAL NODE INSPECTOR — Click any node on the map to open a
 *      sliding left-side panel with full node details and a manual
 *      "FORCE SIGNAL LOSS" override button.
 *
 *   2. HUMAN-IN-THE-LOOP UAV AUTHORIZATION — UAV_DISPATCH payloads
 *      (and manual overrides) do NOT auto-inject the relay. The system
 *      waits indefinitely for the presenter to click [AUTHORIZE DEPLOYMENT].
 *
 *   3. LIVE EVENT TERMINAL — Bottom-anchored terminal with timestamped
 *      log entries and a [CLEAR] button for jury resets.
 *
 * The selectedNode state is managed entirely outside <MapContainer> to
 * prevent Leaflet zoom/pan resets. The map component is memoized and
 * only re-renders on topology or relay changes.
 */

import dynamic from "next/dynamic";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  type TopoNode,
  type TopoLink,
  type TopologyPayload,
  type DispatchPayload,
  type AerialRelay,
  type BridgeMessage,
  DEFAULT_CENTER,
  DEFAULT_ZOOM,
  GATEWAY_ID,
} from "@/lib/mesh";

/* ------------------------------------------------------------------ */
/* Dynamic import — Leaflet requires DOM, cannot SSR                   */
/* ------------------------------------------------------------------ */

const TacticalMap = dynamic(() => import("@/components/TacticalMap"), {
  ssr: false,
  loading: () => (
    <div className="flex h-full w-full items-center justify-center bg-slate-950 font-mono text-xs tracking-[0.3em] text-slate-600">
      INITIALIZING TACTICAL MAP ENGINE
    </div>
  ),
});

import LeftSidebar from "@/components/LeftSidebar";

/* ------------------------------------------------------------------ */
/* Config                                                              */
/* ------------------------------------------------------------------ */

const WS_URL = "ws://localhost:8765"; // must match bridge.py --ws-port
const WS_RETRY_MS = 2000;
const MAX_EVENTS = 80;

/* ------------------------------------------------------------------ */
/* Types                                                               */
/* ------------------------------------------------------------------ */

type WsStatus = "connecting" | "connected" | "disconnected";
type Severity = "info" | "ok" | "warn" | "critical";

interface MeshEvent {
  id: number;
  ts: string;
  severity: Severity;
  text: string;
}

/** Pending dispatch awaiting human authorization. */
interface PendingDispatch {
  targetNode: number;
  midpoint: [number, number];
  receivedAt: number;
  /** "awaiting" = waiting for button click; "transit" = authorized, UAV deploying */
  phase: "awaiting" | "transit";
}

const timestamp = () =>
  new Date().toLocaleTimeString("en-GB", { hour12: false });

/* ================================================================== */
/* Page                                                                */
/* ================================================================== */

export default function Home() {
  /* ---- Core state ---- */
  const [mapCenter] = useState<[number, number]>(DEFAULT_CENTER);
  const [operatorPosition, setOperatorPosition] = useState<[number, number] | null>(null);
  const [nodes, setNodes] = useState<TopoNode[]>([]);
  const [links, setLinks] = useState<TopoLink[]>([]);
  const [relay, setRelay] = useState<AerialRelay | null>(null);
  const [wsStatus, setWsStatus] = useState<WsStatus>("connecting");
  const [events, setEvents] = useState<MeshEvent[]>([]);

  /* Feature 1: Node inspector */
  const [selectedNode, setSelectedNode] = useState<TopoNode | null>(null);

  /* Feature 2: Human-in-the-loop dispatch */
  const [pendingDispatch, setPendingDispatch] = useState<PendingDispatch | null>(null);

  /* Sidebar state: expanded vs contracted */
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);

  const eventIdRef = useRef(0);
  const wsRef = useRef<WebSocket | null>(null);
  const operatorPositionRef = useRef<[number, number] | null>(null);

  // Keep the ref in sync with state so the WS onopen handler can access it
  useEffect(() => {
    operatorPositionRef.current = operatorPosition;
  }, [operatorPosition]);

  /* ---- Event log ---- */
  const addEvent = useCallback((severity: Severity, text: string) => {
    const event: MeshEvent = {
      id: ++eventIdRef.current,
      ts: timestamp(),
      severity,
      text,
    };
    setEvents((prev) =>
      prev.length >= MAX_EVENTS
        ? [...prev.slice(prev.length - MAX_EVENTS + 1), event]
        : [...prev, event]
    );
  }, []);

  const clearEvents = useCallback(() => setEvents([]), []);

  /* ---- Geolocation on mount & live watch (operator position) ---- */
  useEffect(() => {
    if (!navigator.geolocation) {
      addEvent("warn", "Geolocation API not supported by browser");
      return;
    }

    const sendGpsToBridge = (coords: [number, number]) => {
      if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
        wsRef.current.send(
          JSON.stringify({ type: "OPERATOR_GPS", lat: coords[0], lon: coords[1] })
        );
      }
    };

    navigator.geolocation.getCurrentPosition(
      (pos) => {
        const coords: [number, number] = [pos.coords.latitude, pos.coords.longitude];
        setOperatorPosition(coords);
        sendGpsToBridge(coords);
        addEvent("ok", `OPERATOR GPS LOCK: ${coords[0].toFixed(4)}, ${coords[1].toFixed(4)}`);
      },
      (err) => {
        addEvent("warn", `Operator GPS pending/denied: ${err.message}`);
      },
      { enableHighAccuracy: true, timeout: 10000 }
    );

    const watchId = navigator.geolocation.watchPosition(
      (pos) => {
        const coords: [number, number] = [pos.coords.latitude, pos.coords.longitude];
        setOperatorPosition(coords);
        sendGpsToBridge(coords);
      },
      () => {},
      { enableHighAccuracy: true, timeout: 15000, maximumAge: 5000 }
    );

    return () => {
      navigator.geolocation.clearWatch(watchId);
    };
  }, [addEvent]);

  /* ---- Handle TOPOLOGY payload ---- */
  const handleTopology = useCallback(
    (payload: TopologyPayload) => {
      setNodes((prev) => {
        const prevById = new Map(prev.map((n) => [n.id, n]));
        for (const n of payload.nodes) {
          const before = prevById.get(n.id);
          if (!before) {
            // New node joined
            const sigInfo = n.rssi != null
              ? ` | RSSI: ${n.rssi} dBm | DIST: ${n.distance_m?.toFixed(1)}m`
              : "";
            addEvent("info", `NODE JOINED: ${n.id.toString(16).toUpperCase()} (${n.id})${sigInfo}`);
          } else if (before.status !== n.status) {
            // Status transition — the self-healing story
            const hex = n.id.toString(16).toUpperCase();
            if (n.status === "OFFLINE") {
              addEvent("critical", `NODE LOST: ${hex} — telemetry timeout. Topology fractured.`);
            } else if (n.status === "online" && before.status === "OFFLINE") {
              addEvent("ok", `SELF-HEAL: ${hex} re-established. Topology restored.`);
            } else if (n.status === "online" && before.status === "JAMMED") {
              addEvent("ok", `SIGNAL RESTORED: ${hex} back online.`);
            }
          }
        }
        // Nodes removed from the topology entirely
        for (const old of prev) {
          if (!payload.nodes.some((n) => n.id === old.id)) {
            addEvent("warn", `NODE REMOVED: ${old.id.toString(16).toUpperCase()} dropped from topology.`);
          }
        }
        return payload.nodes;
      });
      setLinks(payload.links);
    },
    [addEvent]
  );

  /* ---- Handle UAV_DISPATCH payload (human-in-the-loop) ---- */
  const handleDispatch = useCallback(
    (payload: DispatchPayload) => {
      const pending: PendingDispatch = {
        targetNode: payload.target_node,
        midpoint: payload.midpoint,
        receivedAt: Date.now(),
        phase: "awaiting",
      };
      setPendingDispatch(pending);

      addEvent(
        "critical",
        `INTERFERENCE DETECTED: ${payload.target_node.toString(16).toUpperCase()}. Awaiting authorization.`
      );
    },
    [addEvent]
  );

  /* ---- Authorize deployment (Feature 2 action) ---- */
  const authorizeDeployment = useCallback(() => {
    if (!pendingDispatch) return;

    // Transition to "transit" phase
    setPendingDispatch((prev) => prev ? { ...prev, phase: "transit" } : null);
    addEvent("ok", "UAV AUTHORIZED. Aerial relay in transit…");

    // Inject relay marker after short transit animation (1.5s)
    const mid = pendingDispatch.midpoint;
    const target = pendingDispatch.targetNode;
    setTimeout(() => {
      setRelay({
        lat: mid[0],
        lon: mid[1],
        targetNodeId: target,
        injectedAt: Date.now(),
      });
      addEvent(
        "ok",
        `AERIAL_RELAY deployed at [${mid[0].toFixed(4)}, ${mid[1].toFixed(4)}]. Network gap bridged.`
      );
      // Dismiss panel after deployment
      setTimeout(() => setPendingDispatch(null), 3000);
    }, 1500);
  }, [pendingDispatch, addEvent]);

  /* ---- Force signal loss (Feature 1 action) ---- */
  const forceSignalLoss = useCallback(
    (nodeId: number) => {
      // Locally jam the node
      setNodes((prev) =>
        prev.map((n) =>
          n.id === nodeId ? { ...n, status: "JAMMED" as const } : n
        )
      );

      addEvent("critical", `INTERFERENCE DETECTED: ${nodeId.toString(16).toUpperCase()} (manual override)`);

      // Find the node and gateway for midpoint calculation
      const node = nodes.find((n) => n.id === nodeId);
      const gateway = nodes.find((n) => n.id === GATEWAY_ID);

      if (node && gateway) {
        const midLat = (gateway.lat + node.lat) / 2;
        const midLon = (gateway.lon + node.lon) / 2;

        // Trigger human-in-the-loop dispatch
        const pending: PendingDispatch = {
          targetNode: nodeId,
          midpoint: [midLat, midLon],
          receivedAt: Date.now(),
          phase: "awaiting",
        };
        setPendingDispatch(pending);
      }

      // Close inspector
      setSelectedNode(null);
    },
    [nodes, addEvent]
  );

  /* ---- Node click handler (stable ref for TacticalMap memo) ---- */
  const handleNodeClick = useCallback((node: TopoNode) => {
    setSelectedNode((prev) => (prev?.id === node.id ? null : node));
  }, []);

  /* ---- WebSocket lifecycle ---- */
  useEffect(() => {
    let ws: WebSocket | null = null;
    let retryTimer: ReturnType<typeof setTimeout> | undefined;
    let disposed = false;

    const connect = () => {
      setWsStatus("connecting");
      ws = new WebSocket(WS_URL);
      let wasConnected = false;

      ws.onopen = () => {
        wasConnected = true;
        setWsStatus("connected");
        addEvent("info", `WS CONNECTED (${WS_URL})`);
        wsRef.current = ws;

        // Send current operator GPS to bridge immediately on connect
        if (operatorPositionRef.current) {
          ws?.send(
            JSON.stringify({
              type: "OPERATOR_GPS",
              lat: operatorPositionRef.current[0],
              lon: operatorPositionRef.current[1],
            })
          );
        }
      };

      ws.onmessage = (event) => {
        let msg: BridgeMessage;
        try {
          msg = JSON.parse(event.data as string);
        } catch {
          return;
        }

        if (msg.type === "TOPOLOGY") {
          handleTopology(msg as TopologyPayload);
        } else if (msg.type === "UAV_DISPATCH") {
          handleDispatch(msg as DispatchPayload);
        }
      };

      ws.onclose = () => {
        if (disposed) return;
        setWsStatus("disconnected");
        wsRef.current = null;
        if (wasConnected)
          addEvent("warn", "Bridge uplink lost. Reconnecting…");
        retryTimer = setTimeout(connect, WS_RETRY_MS);
      };

      ws.onerror = () => ws?.close();
    };

    connect();
    return () => {
      disposed = true;
      if (retryTimer) clearTimeout(retryTimer);
      ws?.close();
    };
  }, [addEvent, handleTopology, handleDispatch]);

  /* ---- Keep selectedNode in sync with live topology ---- */
  const inspectedNode = useMemo(() => {
    if (!selectedNode) return null;
    return nodes.find((n) => n.id === selectedNode.id) ?? null;
  }, [selectedNode, nodes]);

  useEffect(() => {
    if (selectedNode && !nodes.some((n) => n.id === selectedNode.id)) {
      setSelectedNode(null);
    }
  }, [nodes, selectedNode]);

  /* ---- Derived metrics ---- */
  const metrics = useMemo(() => {
    const online = nodes.filter((n) => n.status === "online");
    const jammed = nodes.filter((n) => n.status === "JAMMED");
    const offline = nodes.filter((n) => n.status === "OFFLINE");
    const gateway = nodes.find((n) => n.id === GATEWAY_ID);
    return { online, jammed, offline, gateway, total: nodes.length };
  }, [nodes]);

  /* ---- Render ---- */
  return (
    <main className="relative h-screen w-screen overflow-hidden bg-slate-950 text-slate-300">
      {/* Tactical Leaflet Map Engine - Full Screen Base Layer under Glass HUD */}
      <div className="absolute inset-0 z-0">
        <TacticalMap
          center={operatorPosition ?? mapCenter}
          zoom={DEFAULT_ZOOM}
          nodes={nodes}
          links={links}
          relay={relay}
          operatorPosition={operatorPosition}
          onNodeClick={handleNodeClick}
        />
      </div>

      {/* Top bar (Header - Frosted Glass HUD) */}
      <TopBar
        wsStatus={wsStatus}
        nodeCount={metrics.total}
        linkCount={links.length}
        onlineCount={metrics.online.length}
        jammedCount={metrics.jammed.length}
        offlineCount={metrics.offline.length}
      />

      {/* Main split viewport: Left Sidebar | Node Inspector | Map & Tactical Space */}
      <div className="pointer-events-none relative flex h-[calc(100vh-3rem)] w-full overflow-hidden">
        {/* Left Column: Dedicated Tactical Sidebar (Frosted Glass Panel) */}
        <div className="pointer-events-auto h-full shrink-0">
          <LeftSidebar
            metrics={metrics}
            nodes={nodes}
            linksCount={links.length}
            relay={relay}
            operatorPosition={operatorPosition}
            onSelectNode={handleNodeClick}
            collapsed={sidebarCollapsed}
            onToggleCollapse={() => setSidebarCollapsed((prev) => !prev)}
          />
        </div>

        {/* Feature 1: Node Inspector — slides in beside the sidebar, not on top */}
        <div className="pointer-events-auto h-full shrink-0">
          <NodeInspector
            node={inspectedNode}
            onClose={() => setSelectedNode(null)}
            onForceSignalLoss={forceSignalLoss}
          />
        </div>

        {/* Right Column: Tactical Map HUD Area */}
        <div className="pointer-events-none relative flex-1 h-full overflow-hidden">
          {/* Telemetry status hint banner — docked at top-center of map area */}
          {nodes.length === 0 && (
            <div className="pointer-events-auto absolute top-4 inset-x-0 z-20 flex justify-center px-4">
              <div className="glass flex items-center gap-3 rounded-full border border-white/15 px-5 py-2 shadow-2xl backdrop-blur-md">
                <span
                  className={`h-2 w-2 rounded-full ${
                    wsStatus === "connected"
                      ? "animate-pulse bg-emerald-400 shadow-[0_0_10px_rgba(52,211,153,0.9)]"
                      : wsStatus === "connecting"
                        ? "animate-pulse bg-amber-400 shadow-[0_0_10px_rgba(251,191,36,0.9)]"
                        : "bg-rose-500 shadow-[0_0_10px_rgba(244,63,94,0.9)]"
                  }`}
                />
                <div className="flex flex-col text-left">
                  <span className="font-mono text-[11px] font-semibold tracking-[0.2em] text-slate-200">
                    {wsStatus === "connected"
                      ? "AWAITING MESH TELEMETRY"
                      : wsStatus === "connecting"
                        ? "ESTABLISHING UPLINK"
                        : `NO UPLINK — ${WS_URL}`}
                  </span>
                  <span className="font-mono text-[9px] tracking-wider text-slate-400">
                    {wsStatus === "connected"
                      ? "Operator GPS locked · Listening for ESP32 gateway telemetry"
                      : "Ensure bridge.py is running on the gateway ESP32"}
                  </span>
                </div>
              </div>
            </div>
          )}

          {/* Feature 2: Human-in-the-loop dispatch panel — alert when dispatch requested */}
          {pendingDispatch && (
            <div className="pointer-events-auto">
              <DispatchPanel
                dispatch={pendingDispatch}
                relay={relay}
                onAuthorize={authorizeDeployment}
                onDismiss={() => setPendingDispatch(null)}
              />
            </div>
          )}

          {/* Feature 3: Live event terminal — docked at bottom of map area */}
          <div className="pointer-events-auto">
            <EventTerminal events={events} onClear={clearEvents} />
          </div>
        </div>
      </div>
    </main>
  );
}

/* ================================================================== */
/* Feature 1: Node Inspector Panel                                     */
/* ================================================================== */

function NodeInspector({
  node,
  onClose,
  onForceSignalLoss,
}: {
  node: TopoNode | null;
  onClose: () => void;
  onForceSignalLoss: (nodeId: number) => void;
}) {
  const isOpen = node !== null;
  const isGateway = node?.id === GATEWAY_ID;
  const isJammed = node?.status === "JAMMED";
  const isOffline = node?.status === "OFFLINE";

  return (
    <div
      className={`inspector-panel h-full border-r border-slate-800 transition-all duration-300 ease-out overflow-hidden ${
        isOpen ? "w-80 opacity-100" : "w-0 opacity-0"
      }`}
    >
      {node && (
        <div className="flex h-full w-80 flex-col p-5">
          {/* Header */}
          <div className="mb-5 flex items-start justify-between">
            <div>
              <h2 className="text-[10px] font-semibold tracking-[0.25em] text-slate-400">
                TACTICAL NODE INSPECTOR
              </h2>
              <div className="mt-1 flex items-center gap-2">
                <span
                  className={`h-2 w-2 rounded-full ${
                    isGateway
                      ? "bg-cyan-400 shadow-[0_0_8px_rgba(34,211,238,0.6)]"
                      : isJammed
                        ? "bg-rose-500 shadow-[0_0_8px_rgba(244,63,94,0.6)] animate-pulse"
                        : isOffline
                          ? "bg-slate-500"
                          : "bg-emerald-400 shadow-[0_0_8px_rgba(52,211,153,0.6)]"
                  }`}
                />
                <span
                  className={`font-mono text-xs font-bold tracking-wider ${
                    isGateway
                      ? "text-cyan-400"
                      : isJammed
                        ? "text-rose-400"
                        : isOffline
                          ? "text-slate-400"
                          : "text-emerald-400"
                  }`}
                >
                  {isGateway
                    ? "GATEWAY"
                    : isJammed
                      ? "JAMMED"
                      : isOffline
                        ? "OFFLINE"
                        : "ONLINE"}
                </span>
              </div>
            </div>
            <button
              onClick={onClose}
              className="rounded border border-slate-700 px-2 py-1 font-mono text-[10px] text-slate-500 transition-colors hover:border-slate-500 hover:text-slate-300"
              aria-label="Close inspector"
            >
              ESC ✕
            </button>
          </div>

          {/* Node Details */}
          <div className="space-y-3 font-mono text-[11px]">
            <div className="rounded border border-slate-800 bg-slate-900/60 p-3">
              <div className="mb-2 text-[9px] font-bold tracking-[0.2em] text-slate-500">
                NODE ID
              </div>
              <div className="text-lg font-bold text-slate-100">
                {node.id}
              </div>
              <div className="mt-0.5 text-[10px] text-slate-400">
                0x{node.id.toString(16).toUpperCase()}
              </div>
            </div>

            <div className="rounded border border-slate-800 bg-slate-900/60 p-3">
              <div className="mb-2 text-[9px] font-bold tracking-[0.2em] text-slate-500">
                STATUS
              </div>
              <div
                className={`text-sm font-bold ${
                  isGateway
                    ? "text-cyan-400"
                    : isJammed
                      ? "text-rose-400"
                      : isOffline
                        ? "text-slate-400"
                        : "text-emerald-400"
                }`}
              >
                {isGateway
                  ? "GATEWAY (ROOT)"
                  : isJammed
                    ? "JAMMED — SIGNAL LOST"
                    : isOffline
                      ? "OFFLINE — TELEMETRY LOST"
                      : "ONLINE — NOMINAL"}
              </div>
            </div>

            <div className="rounded border border-slate-800 bg-slate-900/60 p-3">
              <div className="mb-2 text-[9px] font-bold tracking-[0.2em] text-slate-500">
                COORDINATES
              </div>
              <div className="space-y-1">
                <div className="flex justify-between">
                  <span className="text-slate-500">LAT</span>
                  <span className="text-slate-200">{node.lat.toFixed(6)}</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-slate-500">LON</span>
                  <span className="text-slate-200">{node.lon.toFixed(6)}</span>
                </div>
              </div>
            </div>

            <div className="rounded border border-slate-800 bg-slate-900/60 p-3">
              <div className="mb-2 text-[9px] font-bold tracking-[0.2em] text-slate-500">
                ROLE
              </div>
              <div className="text-[11px] text-slate-300">
                {isGateway ? "Mesh Root · Serial Bridge" : "Mesh Endpoint · Sensor Node"}
              </div>
            </div>

            {/* RSSI & Signal Monitoring */}
            {!isGateway && (
              <div className="rounded border border-slate-800 bg-slate-900/60 p-3">
                <div className="mb-2 text-[9px] font-bold tracking-[0.2em] text-slate-500">
                  SIGNAL MONITORING
                </div>
                <div className="space-y-2">
                  <div className="flex justify-between">
                    <span className="text-slate-500">RSSI</span>
                    <span className={`font-bold ${
                      node.rssi == null ? "text-slate-500"
                        : node.rssi > -50 ? "text-emerald-400"
                        : node.rssi > -70 ? "text-cyan-400"
                        : node.rssi > -85 ? "text-amber-400"
                        : "text-rose-400"
                    }`}>
                      {node.rssi != null ? `${node.rssi} dBm` : "N/A"}
                    </span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-slate-500">QUALITY</span>
                    <span className={`font-bold text-[10px] ${
                      node.rssi == null ? "text-slate-500"
                        : node.rssi > -50 ? "text-emerald-400"
                        : node.rssi > -70 ? "text-cyan-400"
                        : node.rssi > -85 ? "text-amber-400"
                        : "text-rose-400"
                    }`}>
                      {node.rssi == null ? "NO SIGNAL"
                        : node.rssi > -50 ? "EXCELLENT"
                        : node.rssi > -70 ? "GOOD"
                        : node.rssi > -85 ? "FAIR"
                        : "WEAK"}
                    </span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-slate-500">FSPL DIST</span>
                    <span className="text-slate-200">
                      {node.distance_m != null ? `${node.distance_m.toFixed(1)} m` : "N/A"}
                    </span>
                  </div>
                  {/* Signal Strength Bar */}
                  {node.rssi != null && (() => {
                    // Map RSSI (-100 to -30 range) to 0-10 segments
                    const clamped = Math.max(-100, Math.min(-30, node.rssi));
                    const pct = ((clamped + 100) / 70);
                    const segments = Math.round(pct * 10);
                    const barColor = node.rssi > -50
                      ? "bg-emerald-400 shadow-[0_0_4px_rgba(52,211,153,0.6)]"
                      : node.rssi > -70
                        ? "bg-cyan-400 shadow-[0_0_4px_rgba(34,211,238,0.6)]"
                        : node.rssi > -85
                          ? "bg-amber-400 shadow-[0_0_4px_rgba(251,191,36,0.6)]"
                          : "bg-rose-400 shadow-[0_0_4px_rgba(244,63,94,0.6)]";
                    return (
                      <div className="pt-1">
                        <div className="flex gap-[2px]">
                          {Array.from({ length: 10 }).map((_, i) => (
                            <div
                              key={i}
                              className={`h-1.5 flex-1 rounded-[1px] transition-all duration-200 ${
                                i < segments ? barColor : "bg-slate-800/60"
                              }`}
                            />
                          ))}
                        </div>
                      </div>
                    );
                  })()}
                </div>
              </div>
            )}
            {isGateway && (
              <div className="rounded border border-slate-800 bg-slate-900/60 p-3">
                <div className="mb-2 text-[9px] font-bold tracking-[0.2em] text-slate-500">
                  SIGNAL MONITORING
                </div>
                <div className="flex justify-between">
                  <span className="text-slate-500">RSSI</span>
                  <span className="text-cyan-400 font-bold">ROOT (LOCAL)</span>
                </div>
              </div>
            )}
          </div>

          {/* Spacer */}
          <div className="flex-1" />

          {/* Force Signal Loss button — only for online non-gateway nodes */}
          {!isGateway && !isJammed && !isOffline && (
            <button
              onClick={() => onForceSignalLoss(node.id)}
              className="force-loss-btn mt-4 w-full rounded border-2 border-rose-600/60 bg-rose-950/30 px-4 py-3 font-mono text-xs font-bold tracking-[0.15em] text-rose-400 transition-all hover:border-rose-500 hover:bg-rose-950/60 hover:text-rose-300 hover:shadow-[0_0_20px_rgba(244,63,94,0.2)]"
            >
              ⚠ FORCE SIGNAL LOSS
            </button>
          )}

          {isJammed && (
            <div className="mt-4 rounded border border-rose-800/40 bg-rose-950/20 px-4 py-3 text-center font-mono text-[10px] tracking-wider text-rose-400/80">
              NODE INTERFERENCE ACTIVE
            </div>
          )}

          {isOffline && (
            <div className="mt-4 rounded border border-slate-700/60 bg-slate-900/40 px-4 py-3 text-center font-mono text-[10px] tracking-wider text-slate-400">
              NODE UNREACHABLE — AWAITING SELF-HEAL
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/* ================================================================== */
/* Panels                                                              */
/* ================================================================== */

function TopBar({
  wsStatus,
  nodeCount,
  linkCount,
  onlineCount,
  jammedCount,
  offlineCount,
}: {
  wsStatus: WsStatus;
  nodeCount: number;
  linkCount: number;
  onlineCount: number;
  jammedCount: number;
  offlineCount: number;
}) {
  return (
    <header className="glass relative z-30 flex h-12 w-full shrink-0 items-center justify-between border-b border-white/10 px-4">
      <div className="flex items-baseline gap-3">
        <h1 className="text-sm font-semibold tracking-[0.2em] text-slate-100">
          AEROMESH <span className="text-cyan-400">COMMAND</span>
        </h1>
        <span className="hidden font-mono text-[10px] tracking-wider text-slate-400 sm:inline">
          TACTICAL C2 · SELF-HEALING MESH
        </span>
      </div>
      <div className="flex items-center gap-3 font-mono text-[11px]">
        <span className="glass-pill rounded px-2 py-0.5 text-slate-300">
          NODES <span className="text-white font-bold">{nodeCount}</span>
        </span>
        <span className="glass-pill rounded px-2 py-0.5 text-slate-300">
          LINKS <span className="text-cyan-300 font-bold">{linkCount}</span>
        </span>
        <span className="glass-pill rounded px-2 py-0.5 border-emerald-500/30 text-emerald-300">
          ONLINE <span className="text-emerald-200 font-bold">{onlineCount}</span>
        </span>
        {jammedCount > 0 && (
          <span className="glass-pill animate-pulse rounded px-2 py-0.5 border-rose-500/40 text-rose-300 shadow-[0_0_10px_rgba(244,63,94,0.3)]">
            JAMMED <span className="text-rose-200 font-bold">{jammedCount}</span>
          </span>
        )}
        {offlineCount > 0 && (
          <span className="glass-pill rounded px-2 py-0.5 border-slate-500/40 text-slate-300">
            OFFLINE <span className="text-slate-200 font-bold">{offlineCount}</span>
          </span>
        )}
        <span className="glass-pill rounded px-2.5 py-0.5">
          <MissionClock />
        </span>
        <span
          className={`glass-pill flex items-center gap-1.5 rounded px-2.5 py-0.5 tracking-wider font-semibold ${
            wsStatus === "connected"
              ? "border-emerald-500/50 text-emerald-300 shadow-[0_0_12px_rgba(52,211,153,0.25)]"
              : wsStatus === "connecting"
                ? "border-amber-500/50 text-amber-300 shadow-[0_0_12px_rgba(251,191,36,0.25)]"
                : "border-rose-500/50 text-rose-300 shadow-[0_0_12px_rgba(244,63,94,0.25)]"
          }`}
        >
          <span
            className={`h-1.5 w-1.5 rounded-full ${
              wsStatus === "connected"
                ? "animate-pulse-glow bg-emerald-400"
                : wsStatus === "connecting"
                  ? "animate-pulse bg-amber-400"
                  : "bg-rose-400"
            }`}
          />
          {wsStatus === "connected"
            ? "UPLINK"
            : wsStatus === "connecting"
              ? "SYNC"
              : "NO LINK"}
        </span>
      </div>
    </header>
  );
}

function MissionClock() {
  const [now, setNow] = useState("");
  useEffect(() => {
    const tick = () => setNow(timestamp());
    tick();
    const interval = setInterval(tick, 1000);
    return () => clearInterval(interval);
  }, []);
  return <span className="tracking-widest text-slate-300">{now}</span>;
}



/* ================================================================== */
/* Feature 2: Human-in-the-Loop Dispatch Panel                         */
/* ================================================================== */

function DispatchPanel({
  dispatch,
  relay,
  onAuthorize,
  onDismiss,
}: {
  dispatch: PendingDispatch;
  relay: AerialRelay | null;
  onAuthorize: () => void;
  onDismiss: () => void;
}) {
  const isAwaiting = dispatch.phase === "awaiting";
  const isTransit = dispatch.phase === "transit";
  const isDeployed = relay !== null;

  return (
    <section className="dispatch-panel glass absolute top-16 right-4 z-30 w-80 rounded-md border border-rose-500/80 p-5 shadow-[0_8px_32px_rgba(244,63,94,0.35)]">
      {/* Close button */}
      <button
        onClick={onDismiss}
        className="absolute top-2 right-3 font-mono text-[10px] text-slate-500 transition-colors hover:text-slate-300"
        aria-label="Dismiss"
      >
        ✕
      </button>

      {/* Flash header */}
      <div className={`mb-3 flex items-center gap-2 ${isAwaiting ? "dispatch-flash" : ""}`}>
        <span
          className={`h-2.5 w-2.5 rounded-full ${
            isDeployed
              ? "bg-cyan-400 shadow-[0_0_12px_rgba(34,211,238,0.8)]"
              : isTransit
                ? "bg-amber-400 shadow-[0_0_12px_rgba(251,191,36,0.8)] animate-pulse"
                : "bg-rose-500 shadow-[0_0_12px_rgba(244,63,94,0.8)]"
          }`}
        />
        <h2
          className={`font-mono text-sm font-bold tracking-[0.2em] ${
            isDeployed
              ? "text-cyan-300"
              : isTransit
                ? "text-amber-300"
                : "text-rose-300"
          }`}
        >
          {isDeployed
            ? "RELAY DEPLOYED"
            : isTransit
              ? "UAV IN TRANSIT"
              : "UAV DISPATCH REQUIRED"}
        </h2>
      </div>

      {/* Target info */}
      <div className="space-y-2 font-mono text-[11px]">
        <div className="flex justify-between">
          <span className="text-slate-500">TARGET NODE</span>
          <span className="text-rose-300">
            {dispatch.targetNode.toString(16).toUpperCase()}
          </span>
        </div>
        <div className="flex justify-between">
          <span className="text-slate-500">NODE ID (DEC)</span>
          <span className="text-rose-300">{dispatch.targetNode}</span>
        </div>
        <div className="flex justify-between">
          <span className="text-slate-500">DEPLOY COORDS</span>
          <span className="text-cyan-300">
            {dispatch.midpoint[0].toFixed(4)}, {dispatch.midpoint[1].toFixed(4)}
          </span>
        </div>
      </div>

      {/* Authorization / status area */}
      <div className="mt-4 border-t border-rose-800/50 pt-4">
        {isAwaiting && (
          <button
            onClick={onAuthorize}
            className="authorize-btn w-full rounded border-2 border-cyan-400/60 bg-cyan-950/40 px-4 py-3 font-mono text-sm font-bold tracking-[0.2em] text-cyan-300 transition-all hover:border-cyan-400 hover:bg-cyan-900/50 hover:shadow-[0_0_30px_rgba(34,211,238,0.3)]"
          >
            ▶ AUTHORIZE DEPLOYMENT
          </button>
        )}

        {isTransit && (
          <div className="flex items-center justify-center gap-3 py-2">
            <span className="h-2 w-2 animate-pulse rounded-full bg-amber-400 shadow-[0_0_8px_rgba(251,191,36,0.6)]" />
            <span className="font-mono text-[11px] tracking-wider text-amber-300">
              DEPLOYING AERIAL RELAY…
            </span>
          </div>
        )}

        {isDeployed && (
          <div className="flex items-center justify-center gap-3 py-2">
            <span className="h-2 w-2 animate-pulse-glow rounded-full bg-cyan-400 shadow-[0_0_8px_rgba(34,211,238,0.6)]" />
            <span className="font-mono text-[11px] tracking-wider text-cyan-300">
              AERIAL_RELAY ACTIVE — GAP BRIDGED
            </span>
          </div>
        )}
      </div>
    </section>
  );
}



/* ================================================================== */
/* Feature 3: Live Event Terminal                                      */
/* ================================================================== */

const SEVERITY_STYLE: Record<Severity, { tag: string; cls: string }> = {
  info: { tag: "INFO", cls: "text-slate-400" },
  ok: { tag: " OK ", cls: "text-emerald-400" },
  warn: { tag: "WARN", cls: "text-amber-400" },
  critical: { tag: "CRIT", cls: "text-rose-400" },
};

function EventTerminal({
  events,
  onClear,
}: {
  events: MeshEvent[];
  onClear: () => void;
}) {
  const scrollRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [events]);

  return (
    <section className="event-terminal glass-terminal absolute left-4 right-4 bottom-4 z-20 rounded-md overflow-hidden">
      <div className="flex h-7 items-center justify-between border-b border-white/10 px-3">
        <div className="flex items-center gap-3">
          <h2 className="text-[10px] font-semibold tracking-[0.25em] text-slate-200">
            EVENT TERMINAL
          </h2>
          <div className="flex gap-1.5">
            <span className="h-1.5 w-1.5 rounded-full bg-rose-500/90 shadow-[0_0_6px_rgba(244,63,94,0.7)]" />
            <span className="h-1.5 w-1.5 rounded-full bg-amber-500/90 shadow-[0_0_6px_rgba(251,191,36,0.7)]" />
            <span className="h-1.5 w-1.5 rounded-full bg-emerald-500/90 shadow-[0_0_6px_rgba(52,211,153,0.7)]" />
          </div>
        </div>
        <div className="flex items-center gap-3">
          <span className="glass-pill rounded px-1.5 py-0.2 font-mono text-[9px] text-slate-300">
            {events.length}/{MAX_EVENTS}
          </span>
          <button
            onClick={onClear}
            className="glass-pill rounded px-2 py-0.5 font-mono text-[9px] tracking-wider text-slate-300 transition-all hover:border-white/30 hover:text-white"
          >
            CLEAR
          </button>
        </div>
      </div>
      <div
        ref={scrollRef}
        className="terminal-scroll overflow-y-auto px-3 py-1.5 font-mono text-[11px] leading-5"
        style={{ height: "calc(100% - 1.75rem)" }}
      >
        {events.length === 0 && (
          <p className="text-slate-600">
            root@aeromesh:~$ <span className="animate-pulse text-slate-500">_</span>
          </p>
        )}
        {events.map((e) => (
          <p key={e.id} className="whitespace-pre-wrap">
            <span className="text-slate-600">[{e.ts}]</span>{" "}
            <span className={SEVERITY_STYLE[e.severity].cls}>
              {SEVERITY_STYLE[e.severity].tag}
            </span>{" "}
            <span
              className={
                e.severity === "critical" ? "text-rose-200" : "text-slate-300"
              }
            >
              {e.text}
            </span>
          </p>
        ))}
      </div>
    </section>
  );
}
