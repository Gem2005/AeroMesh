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
  const [mapCenter, setMapCenter] = useState<[number, number]>(DEFAULT_CENTER);
  const [nodes, setNodes] = useState<TopoNode[]>([]);
  const [links, setLinks] = useState<TopoLink[]>([]);
  const [relay, setRelay] = useState<AerialRelay | null>(null);
  const [wsStatus, setWsStatus] = useState<WsStatus>("connecting");
  const [events, setEvents] = useState<MeshEvent[]>([]);

  /* Feature 1: Node inspector */
  const [selectedNode, setSelectedNode] = useState<TopoNode | null>(null);

  /* Feature 2: Human-in-the-loop dispatch */
  const [pendingDispatch, setPendingDispatch] = useState<PendingDispatch | null>(null);

  const eventIdRef = useRef(0);

  /* ---- Geolocation on mount ---- */
  useEffect(() => {
    if (!navigator.geolocation) return;
    navigator.geolocation.getCurrentPosition(
      (pos) => setMapCenter([pos.coords.latitude, pos.coords.longitude]),
      () => { /* denied — keep DEFAULT_CENTER */ }
    );
  }, []);

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

  /* ---- Handle TOPOLOGY payload ---- */
  const handleTopology = useCallback(
    (payload: TopologyPayload) => {
      setNodes((prev) => {
        // Log new nodes joining
        const prevIds = new Set(prev.map((n) => n.id));
        for (const n of payload.nodes) {
          if (!prevIds.has(n.id)) {
            addEvent("info", `NODE JOINED: ${n.id.toString(16).toUpperCase()} (${n.id})`);
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
    return nodes.find((n) => n.id === selectedNode.id) ?? selectedNode;
  }, [selectedNode, nodes]);

  /* ---- Derived metrics ---- */
  const metrics = useMemo(() => {
    const online = nodes.filter((n) => n.status === "online");
    const jammed = nodes.filter((n) => n.status === "JAMMED");
    const gateway = nodes.find((n) => n.id === GATEWAY_ID);
    return { online, jammed, gateway, total: nodes.length };
  }, [nodes]);

  /* ---- Render ---- */
  return (
    <main className="relative h-screen w-screen overflow-hidden bg-slate-950 text-slate-300">
      {/* Full-viewport tactical map */}
      <div className="absolute inset-0 z-0">
        <TacticalMap
          center={mapCenter}
          zoom={DEFAULT_ZOOM}
          nodes={nodes}
          links={links}
          relay={relay}
          onNodeClick={handleNodeClick}
        />
      </div>

      {/* Feature 1: Node Inspector — sliding left panel (outside MapContainer) */}
      <NodeInspector
        node={inspectedNode}
        onClose={() => setSelectedNode(null)}
        onForceSignalLoss={forceSignalLoss}
      />

      {/* Top bar */}
      <TopBar
        wsStatus={wsStatus}
        nodeCount={metrics.total}
        linkCount={links.length}
        onlineCount={metrics.online.length}
        jammedCount={metrics.jammed.length}
      />

      {/* Tactical status panel — top left */}
      <StatusPanel metrics={metrics} relay={relay} />

      {/* Feature 2: Human-in-the-loop dispatch panel — top right */}
      {pendingDispatch && (
        <DispatchPanel
          dispatch={pendingDispatch}
          relay={relay}
          onAuthorize={authorizeDeployment}
          onDismiss={() => setPendingDispatch(null)}
        />
      )}

      {/* Node roster — right side */}
      <NodeRoster nodes={nodes} />

      {/* Feature 3: Live event terminal — bottom anchored */}
      <EventTerminal events={events} onClear={clearEvents} />

      {/* Empty-state hint */}
      {nodes.length === 0 && (
        <div className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center">
          <div className="glass rounded-lg px-8 py-6 text-center">
            <div className="mb-2 font-mono text-sm tracking-[0.3em] text-slate-500">
              {wsStatus === "connected"
                ? "AWAITING MESH TELEMETRY"
                : wsStatus === "connecting"
                  ? "ESTABLISHING UPLINK"
                  : `NO UPLINK — ${WS_URL}`}
            </div>
            <div className="font-mono text-[10px] text-slate-600">
              Ensure bridge.py is running on the gateway ESP32
            </div>
          </div>
        </div>
      )}
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

  return (
    <div
      className={`inspector-panel absolute top-12 bottom-0 left-0 z-40 w-80 border-r border-slate-800 transition-transform duration-300 ease-out ${
        isOpen ? "translate-x-0" : "-translate-x-full"
      }`}
    >
      {node && (
        <div className="flex h-full flex-col p-5">
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
                        : "bg-emerald-400 shadow-[0_0_8px_rgba(52,211,153,0.6)]"
                  }`}
                />
                <span
                  className={`font-mono text-xs font-bold tracking-wider ${
                    isGateway
                      ? "text-cyan-400"
                      : isJammed
                        ? "text-rose-400"
                        : "text-emerald-400"
                  }`}
                >
                  {isGateway ? "GATEWAY" : isJammed ? "JAMMED" : "ONLINE"}
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
                      : "text-emerald-400"
                }`}
              >
                {isGateway ? "GATEWAY (ROOT)" : isJammed ? "JAMMED — SIGNAL LOST" : "ONLINE — NOMINAL"}
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
          </div>

          {/* Spacer */}
          <div className="flex-1" />

          {/* Force Signal Loss button — only for non-gateway, non-jammed nodes */}
          {!isGateway && !isJammed && (
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
}: {
  wsStatus: WsStatus;
  nodeCount: number;
  linkCount: number;
  onlineCount: number;
  jammedCount: number;
}) {
  return (
    <header className="glass absolute inset-x-0 top-0 z-30 flex h-12 items-center justify-between border-x-0 border-t-0 px-4">
      <div className="flex items-baseline gap-3">
        <h1 className="text-sm font-semibold tracking-[0.2em] text-slate-100">
          AEROMESH <span className="text-cyan-400">COMMAND</span>
        </h1>
        <span className="hidden font-mono text-[10px] tracking-wider text-slate-500 sm:inline">
          TACTICAL C2 · SELF-HEALING MESH
        </span>
      </div>
      <div className="flex items-center gap-4 font-mono text-[11px]">
        <span className="text-slate-400">
          NODES{" "}
          <span className="text-slate-100">{nodeCount}</span>
        </span>
        <span className="text-slate-400">
          LINKS{" "}
          <span className="text-slate-100">{linkCount}</span>
        </span>
        <span className="text-emerald-400/80">
          ONLINE{" "}
          <span className="text-emerald-300">{onlineCount}</span>
        </span>
        {jammedCount > 0 && (
          <span className="animate-pulse text-rose-400">
            JAMMED{" "}
            <span className="text-rose-300">{jammedCount}</span>
          </span>
        )}
        <MissionClock />
        <span
          className={`flex items-center gap-1.5 rounded-sm border px-2 py-0.5 tracking-wider ${
            wsStatus === "connected"
              ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-300"
              : wsStatus === "connecting"
                ? "border-amber-500/40 bg-amber-500/10 text-amber-300"
                : "border-rose-500/40 bg-rose-500/10 text-rose-300"
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

function StatusPanel({
  metrics,
  relay,
}: {
  metrics: {
    online: TopoNode[];
    jammed: TopoNode[];
    gateway: TopoNode | undefined;
    total: number;
  };
  relay: AerialRelay | null;
}) {
  const healthPct = metrics.total > 0
    ? Math.round((metrics.online.length / metrics.total) * 100)
    : null;

  const SEGMENTS = 24;
  const active = healthPct === null ? 0 : Math.round((healthPct / 100) * SEGMENTS);
  const tone = healthPct === null ? "text-slate-500" : healthPct >= 80 ? "text-emerald-400" : healthPct >= 50 ? "text-amber-400" : "text-rose-400";
  const barTone = healthPct === null ? "bg-slate-700" : healthPct >= 80 ? "bg-emerald-400" : healthPct >= 50 ? "bg-amber-400" : "bg-rose-400";

  return (
    <section className="glass absolute top-16 left-4 z-20 w-72 rounded-sm p-4">
      <h2 className="text-[10px] font-semibold tracking-[0.25em] text-slate-400">
        NETWORK STATUS
      </h2>

      <p className={`mt-2 font-mono text-4xl font-bold ${tone}`}>
        {healthPct === null ? "--" : `${healthPct}%`}
      </p>

      <div className="mt-3 flex gap-[2px]">
        {Array.from({ length: SEGMENTS }).map((_, i) => (
          <div
            key={i}
            className={`h-3 flex-1 rounded-[1px] ${i < active ? barTone : "bg-slate-800"}`}
          />
        ))}
      </div>

      <div className="mt-3 space-y-1 border-t border-slate-700/50 pt-3 font-mono text-[11px]">
        <div className="flex justify-between">
          <span className="text-slate-500">GATEWAY</span>
          <span className={metrics.gateway ? "text-cyan-400" : "text-slate-600"}>
            {metrics.gateway ? metrics.gateway.id.toString(16).toUpperCase() : "—"}
          </span>
        </div>
        <div className="flex justify-between">
          <span className="text-slate-500">ONLINE</span>
          <span className="text-emerald-400">{metrics.online.length}</span>
        </div>
        <div className="flex justify-between">
          <span className="text-slate-500">JAMMED</span>
          <span className={metrics.jammed.length > 0 ? "text-rose-400" : "text-slate-600"}>
            {metrics.jammed.length}
          </span>
        </div>
        <div className="flex justify-between">
          <span className="text-slate-500">AERIAL RELAY</span>
          <span className={relay ? "text-cyan-400" : "text-slate-600"}>
            {relay ? "ACTIVE" : "STANDBY"}
          </span>
        </div>
      </div>
    </section>
  );
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
    <section className="dispatch-panel absolute top-16 right-4 z-30 w-80 rounded-sm border-2 border-rose-500/80 p-5">
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
/* Node Roster                                                         */
/* ================================================================== */

function NodeRoster({ nodes }: { nodes: TopoNode[] }) {
  if (nodes.length === 0) return null;

  const sorted = [...nodes].sort((a, b) => {
    if (a.id === GATEWAY_ID) return -1;
    if (b.id === GATEWAY_ID) return 1;
    if (a.status === "JAMMED" && b.status !== "JAMMED") return -1;
    if (a.status !== "JAMMED" && b.status === "JAMMED") return 1;
    return a.id - b.id;
  });

  return (
    <aside className="glass terminal-scroll pointer-events-auto absolute top-64 right-4 bottom-48 z-20 w-72 overflow-y-auto rounded-sm p-3">
      <h2 className="mb-2 text-[10px] font-semibold tracking-[0.25em] text-slate-400">
        NODE ROSTER
      </h2>
      <div className="space-y-2">
        {sorted.map((node) => {
          const isGateway = node.id === GATEWAY_ID;
          const isJammed = node.status === "JAMMED";
          const dot = isGateway
            ? "bg-cyan-400 shadow-[0_0_6px_rgba(34,211,238,0.6)]"
            : isJammed
              ? "bg-rose-500 shadow-[0_0_6px_rgba(244,63,94,0.6)]"
              : "bg-emerald-400 shadow-[0_0_6px_rgba(52,211,153,0.6)]";

          return (
            <div
              key={node.id}
              className={`rounded-sm border px-2.5 py-2 ${
                isJammed
                  ? "border-rose-900/60 bg-rose-950/30"
                  : "border-slate-800 bg-slate-900/50"
              }`}
            >
              <div className="flex items-center justify-between">
                <span className="flex items-center gap-2 font-mono text-[11px] text-slate-200">
                  <span className={`h-1.5 w-1.5 rounded-full ${dot}`} />
                  {node.id.toString(16).toUpperCase()}
                </span>
                <span className={`font-mono text-[9px] tracking-wider ${
                  isGateway
                    ? "text-cyan-400"
                    : isJammed
                      ? "text-rose-400"
                      : "text-emerald-400/70"
                }`}>
                  {isGateway ? "GATEWAY" : isJammed ? "JAMMED" : "ONLINE"}
                </span>
              </div>
              <div className="mt-1 font-mono text-[10px] text-slate-500">
                {node.lat.toFixed(4)}, {node.lon.toFixed(4)}
              </div>
            </div>
          );
        })}
      </div>
    </aside>
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
    <section className="event-terminal absolute inset-x-4 bottom-4 z-20 rounded-sm">
      <div className="flex h-7 items-center justify-between border-b border-slate-700/50 px-3">
        <div className="flex items-center gap-3">
          <h2 className="text-[10px] font-semibold tracking-[0.25em] text-slate-400">
            EVENT TERMINAL
          </h2>
          <div className="flex gap-1">
            <span className="h-1.5 w-1.5 rounded-full bg-rose-500/80" />
            <span className="h-1.5 w-1.5 rounded-full bg-amber-500/80" />
            <span className="h-1.5 w-1.5 rounded-full bg-emerald-500/80" />
          </div>
        </div>
        <div className="flex items-center gap-3">
          <span className="font-mono text-[9px] text-slate-600">
            {events.length}/{MAX_EVENTS}
          </span>
          <button
            onClick={onClear}
            className="rounded border border-slate-700/60 px-2 py-0.5 font-mono text-[9px] tracking-wider text-slate-500 transition-all hover:border-slate-500 hover:text-slate-300"
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
