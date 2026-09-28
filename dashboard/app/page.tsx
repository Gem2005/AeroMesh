"use client";

/**
 * AeroMesh Command — Palantir-inspired C2 tactical dashboard.
 *
 * Replaces the abstract physics graph with a real-time Leaflet map
 * showing GPS-projected mesh nodes. All FSPL math and graph topology
 * lives in the Python/NetworkX bridge; this frontend strictly renders
 * the map and UI panels based on WebSocket instructions.
 *
 * WebSocket payloads:
 *   TOPOLOGY    → full node/link snapshot with GPS coords
 *   UAV_DISPATCH → jamming event, triggers tactical alert + UAV injection
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
  UAV_INJECT_DELAY_MS,
  DISPATCH_PANEL_DURATION_MS,
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
const MAX_EVENTS = 60;

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

interface DispatchAlert {
  targetNode: number;
  midpoint: [number, number];
  receivedAt: number;
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
  const [dispatchAlert, setDispatchAlert] = useState<DispatchAlert | null>(null);

  const eventIdRef = useRef(0);
  const dispatchTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const panelTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  /* ---- Geolocation on mount ---- */
  useEffect(() => {
    if (!navigator.geolocation) return;
    navigator.geolocation.getCurrentPosition(
      (pos) => setMapCenter([pos.coords.latitude, pos.coords.longitude]),
      () => { /* denied — keep DEFAULT_CENTER */ }
    );
  }, []);

  /* ---- Event log ring buffer ---- */
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

  /* ---- Handle TOPOLOGY payload ---- */
  const handleTopology = useCallback(
    (payload: TopologyPayload) => {
      setNodes(payload.nodes);
      setLinks(payload.links);
    },
    []
  );

  /* ---- Handle UAV_DISPATCH payload ---- */
  const handleDispatch = useCallback(
    (payload: DispatchPayload) => {
      const alert: DispatchAlert = {
        targetNode: payload.target_node,
        midpoint: payload.midpoint,
        receivedAt: Date.now(),
      };
      setDispatchAlert(alert);

      addEvent(
        "critical",
        `JAMMING DETECTED: Node ${payload.target_node}. UAV dispatch required.`
      );

      // Clear any pending timers
      if (dispatchTimerRef.current) clearTimeout(dispatchTimerRef.current);
      if (panelTimerRef.current) clearTimeout(panelTimerRef.current);

      // Inject UAV relay marker after 3 seconds
      dispatchTimerRef.current = setTimeout(() => {
        const newRelay: AerialRelay = {
          lat: payload.midpoint[0],
          lon: payload.midpoint[1],
          targetNodeId: payload.target_node,
          injectedAt: Date.now(),
        };
        setRelay(newRelay);
        addEvent(
          "ok",
          `AERIAL_RELAY deployed at [${payload.midpoint[0].toFixed(4)}, ${payload.midpoint[1].toFixed(4)}]. Network gap bridged.`
        );
      }, UAV_INJECT_DELAY_MS);

      // Auto-dismiss alert panel
      panelTimerRef.current = setTimeout(() => {
        setDispatchAlert(null);
      }, DISPATCH_PANEL_DURATION_MS);
    },
    [addEvent]
  );

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
        addEvent("info", `Uplink established (${WS_URL}).`);
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
          addEvent("warn", "WARN: Bridge uplink lost. Reconnecting…");
        retryTimer = setTimeout(connect, WS_RETRY_MS);
      };

      ws.onerror = () => ws?.close();
    };

    connect();
    return () => {
      disposed = true;
      if (retryTimer) clearTimeout(retryTimer);
      if (dispatchTimerRef.current) clearTimeout(dispatchTimerRef.current);
      if (panelTimerRef.current) clearTimeout(panelTimerRef.current);
      ws?.close();
    };
  }, [addEvent, handleTopology, handleDispatch]);

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
        />
      </div>

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

      {/* UAV Dispatch tactical alert — top right, glassmorphism overlay */}
      {dispatchAlert && (
        <DispatchPanel
          alert={dispatchAlert}
          relay={relay}
          onDismiss={() => setDispatchAlert(null)}
        />
      )}

      {/* Node roster — right side */}
      <NodeRoster nodes={nodes} />

      {/* Bottom: live event log */}
      <EventLog events={events} />

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

function DispatchPanel({
  alert,
  relay,
  onDismiss,
}: {
  alert: DispatchAlert;
  relay: AerialRelay | null;
  onDismiss: () => void;
}) {
  const elapsed = Date.now() - alert.receivedAt;
  const injecting = elapsed < UAV_INJECT_DELAY_MS && !relay;

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
      <div className="dispatch-flash mb-3 flex items-center gap-2">
        <span className="h-2.5 w-2.5 rounded-full bg-rose-500 shadow-[0_0_12px_rgba(244,63,94,0.8)]" />
        <h2 className="font-mono text-sm font-bold tracking-[0.2em] text-rose-300">
          UAV DISPATCH REQUIRED
        </h2>
      </div>

      {/* Target info */}
      <div className="space-y-2 font-mono text-[11px]">
        <div className="flex justify-between">
          <span className="text-slate-500">TARGET NODE</span>
          <span className="text-rose-300">
            {alert.targetNode.toString(16).toUpperCase()}
          </span>
        </div>
        <div className="flex justify-between">
          <span className="text-slate-500">NODE ID (DEC)</span>
          <span className="text-rose-300">{alert.targetNode}</span>
        </div>
        <div className="flex justify-between">
          <span className="text-slate-500">DEPLOY COORDS</span>
          <span className="text-cyan-300">
            {alert.midpoint[0].toFixed(4)}, {alert.midpoint[1].toFixed(4)}
          </span>
        </div>
      </div>

      {/* Status bar */}
      <div className="mt-4 border-t border-rose-800/50 pt-3">
        {injecting ? (
          <div className="flex items-center gap-2">
            <span className="h-2 w-2 animate-pulse rounded-full bg-amber-400" />
            <span className="font-mono text-[10px] tracking-wider text-amber-300">
              DEPLOYING AERIAL RELAY…
            </span>
          </div>
        ) : relay ? (
          <div className="flex items-center gap-2">
            <span className="h-2 w-2 animate-pulse-glow rounded-full bg-cyan-400" />
            <span className="font-mono text-[10px] tracking-wider text-cyan-300">
              AERIAL_RELAY ACTIVE — GAP BRIDGED
            </span>
          </div>
        ) : (
          <div className="flex items-center gap-2">
            <span className="h-2 w-2 rounded-full bg-rose-500" />
            <span className="font-mono text-[10px] tracking-wider text-rose-300">
              AWAITING RELAY DEPLOYMENT
            </span>
          </div>
        )}
      </div>
    </section>
  );
}

function NodeRoster({ nodes }: { nodes: TopoNode[] }) {
  if (nodes.length === 0) return null;

  const sorted = [...nodes].sort((a, b) => {
    // Gateway first
    if (a.id === GATEWAY_ID) return -1;
    if (b.id === GATEWAY_ID) return 1;
    // Jammed nodes next
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

/* ------------------------------------------------------------------ */
/* Event Log                                                           */
/* ------------------------------------------------------------------ */

const SEVERITY_STYLE: Record<Severity, { tag: string; cls: string }> = {
  info: { tag: "INFO", cls: "text-slate-400" },
  ok: { tag: " OK ", cls: "text-emerald-400" },
  warn: { tag: "WARN", cls: "text-amber-400" },
  critical: { tag: "CRIT", cls: "text-rose-400" },
};

function EventLog({ events }: { events: MeshEvent[] }) {
  const scrollRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [events]);

  return (
    <section className="glass absolute inset-x-4 bottom-4 z-20 h-40 rounded-sm">
      <div className="flex h-7 items-center justify-between border-b border-slate-700/50 px-3">
        <h2 className="text-[10px] font-semibold tracking-[0.25em] text-slate-400">
          LIVE EVENT LOG
        </h2>
        <span className="font-mono text-[9px] text-slate-600">
          {events.length}/{MAX_EVENTS} · AUTO-SCROLL
        </span>
      </div>
      <div
        ref={scrollRef}
        className="terminal-scroll h-[calc(100%-1.75rem)] overflow-y-auto px-3 py-1.5 font-mono text-[11px] leading-5"
      >
        {events.length === 0 && (
          <p className="text-slate-600">-- no events recorded --</p>
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
