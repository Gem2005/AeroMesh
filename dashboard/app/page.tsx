"use client";

/**
 * AeroMesh Command — C2-style dashboard for the Self-Healing Ad-Hoc
 * Wireless Network with UAV Bridge.
 *
 * Setup:
 *   cd dashboard
 *   npm install
 *   npm run dev                 # dashboard at http://localhost:3000
 *
 * Requires the Python bridge to be running first:
 *   pip install pyserial websockets
 *   python ../bridge.py         # serves ws://localhost:8765 from COM9
 *
 * Performance strategy for rapid WebSocket updates:
 *   - The graphData object identity changes ONLY on structural changes
 *     (join / drop / reroute), never on telemetry ticks.
 *   - Telemetry is mirrored into a ref that canvas callbacks read every
 *     animation frame — live dBm labels & link colors without re-renders.
 *   - Node lifecycle transitions (healthy/degraded/failed) are detected in
 *     the WS handlers, not during render; panels derive via useMemo.
 */

import dynamic from "next/dynamic";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  DEGRADED_RSSI,
  DROP_LINGER_MS,
  STALE_MS,
  flattenTopology,
  idOf,
  rssiPercent,
  type GraphData,
  type GraphLink,
  type GraphNode,
  type TelemetryEntry,
  type TelemetryMap,
  type TelemetryPacket,
  type TopologyPacket,
} from "@/lib/mesh";

const MeshGraph = dynamic(() => import("@/components/MeshGraph"), {
  ssr: false,
  loading: () => (
    <div className="flex h-full items-center justify-center font-mono text-xs tracking-[0.3em] text-slate-600">
      INITIALIZING PHYSICS ENGINE
    </div>
  ),
});

const WS_URL = "ws://localhost:8765"; // must match bridge.py --ws-port
const WS_RETRY_MS = 2000;
const MAX_EVENTS = 50;
/** UAV panel stays in critical mode this long after a fracture. */
const FRACTURE_HOLD_MS = 10000;

/* ------------------------------------------------------------------ */
/* Local types                                                         */
/* ------------------------------------------------------------------ */

function isTelemetry(msg: unknown): msg is TelemetryPacket {
  return typeof msg === "object" && msg !== null && "node_id" in msg;
}
function isTopology(msg: unknown): msg is TopologyPacket {
  return typeof msg === "object" && msg !== null && "nodeId" in msg;
}

type Severity = "info" | "ok" | "warn" | "critical";

interface MeshEvent {
  id: number;
  ts: string;
  severity: Severity;
  text: string;
}

type NodeLifecycle = "healthy" | "degraded" | "failed";

interface Fracture {
  nodeId: number;
  mid: { x: number; y: number } | null;
  at: number;
}

type WsStatus = "connecting" | "connected" | "disconnected";

const timestamp = () => new Date().toLocaleTimeString("en-GB", { hour12: false });

/* ================================================================== */
/* Page                                                                */
/* ================================================================== */

export default function Home() {
  const [graphData, setGraphData] = useState<GraphData>({ nodes: [], links: [] });
  const [telemetry, setTelemetry] = useState<TelemetryMap>({});
  const [wsStatus, setWsStatus] = useState<WsStatus>("connecting");
  const [events, setEvents] = useState<MeshEvent[]>([]);
  const [fracture, setFracture] = useState<Fracture | null>(null);
  const [hasTopology, setHasTopology] = useState(false);

  // Refs readable from canvas callbacks / imperative handlers.
  const telemetryRef = useRef<TelemetryMap>({});
  const graphRef = useRef<GraphData>({ nodes: [], links: [] });
  const nodeStateRef = useRef<Record<number, NodeLifecycle>>({});
  const hasTopologyRef = useRef(false);
  const eventIdRef = useRef(0);
  const purgeTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  /* ---------------- Event log ring buffer ---------------- */

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
        : [...prev, event],
    );
  }, []);

  /* ---------------- Graph structural updates ---------------- */

  const linksSignature = (links: GraphLink[]) =>
    links
      .map((l) => `${idOf(l.source)}>${idOf(l.target)}`)
      .sort()
      .join(",");

  /**
   * Single entry point for structural changes (real topology packets AND the
   * telemetry-inferred fallback). Detects joins, drops, and self-heals;
   * emits events; keeps failed nodes lingering for the drift animation.
   */
  const updateGraph = useCallback(
    (liveNodes: { id: number; isRoot: boolean }[], links: GraphLink[]) => {
      const prev = graphRef.current;
      const prevById = new Map(prev.nodes.map((n) => [n.id, n]));
      const liveIds = new Set(liveNodes.map((n) => n.id));
      const now = Date.now();

      // Skip commit entirely when nothing structural changed — the force
      // simulation is never reheated by routine telemetry sweeps.
      const prevOnline = prev.nodes.filter((n) => n.status === "online");
      const unchanged =
        prevOnline.length === liveNodes.length &&
        liveNodes.every(({ id, isRoot }) => {
          const ex = prevById.get(id);
          return ex !== undefined && ex.status === "online" && ex.isRoot === isRoot;
        }) &&
        linksSignature(prev.links) === linksSignature(links) &&
        prev.nodes.every(
          (n) =>
            n.status !== "dropped" ||
            now - (n.droppedAt ?? 0) < DROP_LINGER_MS,
        );
      if (unchanged) return;

      // Live nodes: reuse objects so the physics engine keeps positions.
      const nodes: GraphNode[] = liveNodes.map(({ id, isRoot }) => {
        const existing = prevById.get(id);
        if (existing) {
          if (existing.status === "dropped") {
            addEvent("ok", `SELF-HEAL: Node ${id} re-established. Topology restored.`);
            nodeStateRef.current[id] = "healthy";
            delete existing.droppedAt;
            existing.kicked = false;
          }
          existing.isRoot = isRoot;
          existing.status = "online";
          return existing;
        }
        if (nodeStateRef.current[id] === "failed") {
          addEvent("ok", `SELF-HEAL: Node ${id} re-established. Topology restored.`);
        } else {
          addEvent("info", `Node ${id} joined the mesh.`);
        }
        nodeStateRef.current[id] = "healthy";
        return { id, isRoot, status: "online" };
      });

      // Freshly dropped nodes: crimson, severed, linger for the drift.
      const root = prev.nodes.find((n) => n.isRoot && n.status === "online");
      for (const node of prev.nodes) {
        if (liveIds.has(node.id)) continue;
        if (node.status !== "dropped") {
          node.status = "dropped";
          node.droppedAt = now;
          node.kicked = false;
          nodeStateRef.current[node.id] = "failed";
          addEvent(
            "critical",
            `CRITICAL: Node ${node.id} lost. Topology fractured. Rerouting…`,
          );
          setFracture({
            nodeId: node.id,
            mid:
              node.x !== undefined && root?.x !== undefined
                ? {
                    x: (node.x + root.x) / 2,
                    y: ((node.y ?? 0) + (root.y ?? 0)) / 2,
                  }
                : null,
            at: now,
          });
        }
        if (now - (node.droppedAt ?? now) < DROP_LINGER_MS) nodes.push(node);
      }

      graphRef.current = { nodes, links };
      setGraphData({ nodes: [...nodes], links });

      // Purge drifted-out failed nodes after the linger window.
      if (purgeTimer.current) clearTimeout(purgeTimer.current);
      purgeTimer.current = setTimeout(() => {
        const g = graphRef.current;
        const kept = g.nodes.filter(
          (n) =>
            n.status !== "dropped" ||
            Date.now() - (n.droppedAt ?? 0) < DROP_LINGER_MS,
        );
        if (kept.length !== g.nodes.length) {
          graphRef.current = { nodes: kept, links: g.links };
          setGraphData({ nodes: [...kept], links: g.links });
        }
      }, DROP_LINGER_MS + 250);
    },
    [addEvent],
  );

  /** Real painlessMesh topology packet. */
  const applyTopology = useCallback(
    (packet: TopologyPacket) => {
      if (!hasTopologyRef.current) {
        hasTopologyRef.current = true;
        setHasTopology(true);
      }
      const { nodes, links } = flattenTopology(packet);
      updateGraph(nodes, links);
    },
    [updateGraph],
  );

  /**
   * Fallback while the firmware doesn't print subConnectionJson(): infer a
   * star topology from telemetry. The node reporting "disconnected" is the
   * root; entries silent for STALE_MS are treated as lost.
   */
  const applyTelemetryFallback = useCallback(() => {
    const now = Date.now();
    const entries = Object.values(telemetryRef.current).filter(
      (e) => now - e.lastSeen < STALE_MS,
    );
    const rootId = entries.find((e) => e.parent_rssi === "disconnected")?.node_id;
    const liveNodes = entries.map((e) => ({
      id: e.node_id,
      isRoot: e.node_id === rootId,
    }));
    const links: GraphLink[] =
      rootId === undefined
        ? []
        : entries
            .filter((e) => e.node_id !== rootId)
            .map((e) => ({ source: rootId, target: e.node_id }));
    updateGraph(liveNodes, links);
  }, [updateGraph]);

  /* ---------------- Telemetry + lifecycle transitions ---------------- */

  const handleTelemetry = useCallback(
    (msg: TelemetryPacket) => {
      const id = msg.node_id;
      const entry: TelemetryEntry = { ...msg, lastSeen: Date.now() };
      telemetryRef.current = { ...telemetryRef.current, [id]: entry };
      setTelemetry(telemetryRef.current);

      const prevState = nodeStateRef.current[id];
      if (typeof msg.parent_rssi === "number") {
        if (msg.parent_rssi < DEGRADED_RSSI) {
          if (prevState !== "degraded" && prevState !== "failed") {
            nodeStateRef.current[id] = "degraded";
            addEvent("warn", `WARN: Node ${id} signal degraded (${msg.parent_rssi} dBm).`);
          }
        } else if (prevState === "degraded") {
          nodeStateRef.current[id] = "healthy";
          addEvent("ok", `Node ${id} signal restored (${msg.parent_rssi} dBm).`);
        }
      }

      if (!hasTopologyRef.current) applyTelemetryFallback();
    },
    [addEvent, applyTelemetryFallback],
  );

  // Periodic stale sweep so silent nodes drop out even with no packets at all.
  useEffect(() => {
    const interval = setInterval(() => {
      if (!hasTopologyRef.current) applyTelemetryFallback();
    }, 2000);
    return () => clearInterval(interval);
  }, [applyTelemetryFallback]);

  /* ---------------- WebSocket lifecycle ---------------- */

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
        let msg: unknown;
        try {
          msg = JSON.parse(event.data as string);
        } catch {
          return;
        }
        if (isTelemetry(msg)) handleTelemetry(msg);
        else if (isTopology(msg)) applyTopology(msg);
      };

      ws.onclose = () => {
        if (disposed) return;
        setWsStatus("disconnected");
        if (wasConnected) addEvent("warn", "WARN: Bridge uplink lost. Reconnecting…");
        retryTimer = setTimeout(connect, WS_RETRY_MS);
      };

      ws.onerror = () => ws?.close();
    };

    connect();
    return () => {
      disposed = true;
      if (retryTimer) clearTimeout(retryTimer);
      if (purgeTimer.current) clearTimeout(purgeTimer.current);
      ws?.close();
    };
  }, [addEvent, applyTopology, handleTelemetry]);

  /* ---------------- Graph sizing ---------------- */

  const graphContainer = useRef<HTMLDivElement>(null);
  const [dims, setDims] = useState({ width: 800, height: 600 });

  useEffect(() => {
    const el = graphContainer.current;
    if (!el) return;
    const observer = new ResizeObserver(() =>
      setDims({ width: el.clientWidth, height: el.clientHeight }),
    );
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  /* ---------------- Derived metrics (memoized) ---------------- */

  const metrics = useMemo(() => {
    const onlineNodes = graphData.nodes.filter((n) => n.status === "online");
    const rssis = onlineNodes
      .map((n) => telemetry[n.id]?.parent_rssi)
      .filter((r): r is number => typeof r === "number");
    const health = rssis.length
      ? Math.round(rssis.reduce((sum, r) => sum + rssiPercent(r), 0) / rssis.length)
      : null;
    const degradedIds = onlineNodes
      .filter((n) => {
        const r = telemetry[n.id]?.parent_rssi;
        return typeof r === "number" && r < DEGRADED_RSSI;
      })
      .map((n) => n.id);
    const droppedNodes = graphData.nodes.filter((n) => n.status === "dropped");
    return { onlineNodes, health, degradedIds, droppedNodes };
  }, [graphData, telemetry]);

  const uav = useMemo(() => {
    const recentFracture =
      fracture && Date.now() - fracture.at < FRACTURE_HOLD_MS ? fracture : null;

    if (metrics.droppedNodes.length > 0 || recentFracture) {
      return {
        level: "critical" as const,
        targetId: metrics.droppedNodes[0]?.id ?? recentFracture?.nodeId,
        mid: recentFracture?.mid ?? null,
      };
    }
    if (metrics.degradedIds.length > 0) {
      const nodeId = metrics.degradedIds[0];
      const node = graphData.nodes.find((n) => n.id === nodeId);
      const root = graphData.nodes.find((n) => n.isRoot && n.status === "online");
      const mid =
        node?.x !== undefined && root?.x !== undefined
          ? { x: (node.x + root.x) / 2, y: ((node.y ?? 0) + (root.y ?? 0)) / 2 }
          : null;
      return { level: "warn" as const, targetId: nodeId, mid };
    }
    return { level: "standby" as const, targetId: undefined, mid: null };
  }, [metrics, fracture, graphData]);

  const telemetryRows = useMemo(() => {
    const ids = new Set<number>(graphData.nodes.map((n) => n.id));
    Object.keys(telemetry).forEach((id) => ids.add(Number(id)));
    return [...ids]
      .map((id) => {
        const node = graphData.nodes.find((n) => n.id === id);
        const rssi = telemetry[id]?.parent_rssi;
        const dropped = node === undefined || node.status === "dropped";
        const degraded =
          !dropped && typeof rssi === "number" && rssi < DEGRADED_RSSI;
        return { id, isRoot: node?.isRoot ?? false, dropped, degraded, rssi };
      })
      .sort((a, b) => Number(b.isRoot) - Number(a.isRoot) || a.id - b.id);
  }, [graphData, telemetry]);

  /* ---------------- Render ---------------- */

  return (
    <main className="bg-grid relative h-screen w-screen overflow-hidden bg-slate-950 text-slate-300">
      {/* Full-viewport physics graph */}
      <div ref={graphContainer} className="absolute inset-0">
        <MeshGraph
          graphData={graphData}
          telemetryRef={telemetryRef}
          width={dims.width}
          height={dims.height}
        />
      </div>

      <TopBar
        wsStatus={wsStatus}
        nodeCount={metrics.onlineNodes.length}
        linkCount={graphData.links.length}
        inferred={!hasTopology && graphData.nodes.length > 0}
      />

      {/* Top-left: Network Health Index */}
      <HealthPanel health={metrics.health} />

      {/* Right column: UAV intervention + telemetry rail */}
      <div className="pointer-events-none absolute top-16 right-4 bottom-48 flex w-72 flex-col gap-3">
        <UavPanel uav={uav} />
        <TelemetryRail rows={telemetryRows} />
      </div>

      {/* Bottom: live event log */}
      <EventLog events={events} />

      {/* Empty-state hint */}
      {graphData.nodes.length === 0 && (
        <div className="pointer-events-none absolute inset-0 flex items-center justify-center">
          <p className="font-mono text-xs tracking-[0.35em] text-slate-600">
            {wsStatus === "connected"
              ? "AWAITING MESH TELEMETRY"
              : `NO UPLINK — ${WS_URL}`}
          </p>
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
  inferred,
}: {
  wsStatus: WsStatus;
  nodeCount: number;
  linkCount: number;
  inferred: boolean;
}) {
  return (
    <header className="glass absolute inset-x-0 top-0 flex h-12 items-center justify-between border-x-0 border-t-0 px-4">
      <div className="flex items-baseline gap-3">
        <h1 className="text-sm font-semibold tracking-[0.2em] text-slate-100">
          AEROMESH <span className="text-sky-400">COMMAND</span>
        </h1>
        <span className="hidden font-mono text-[10px] tracking-wider text-slate-500 sm:inline">
          SELF-HEALING AD-HOC MESH · UAV BRIDGE
        </span>
      </div>
      <div className="flex items-center gap-4 font-mono text-[11px]">
        <span className="text-slate-400">
          NODES <span className="text-slate-100">{nodeCount}</span>
        </span>
        <span className="text-slate-400">
          LINKS <span className="text-slate-100">{linkCount}</span>
        </span>
        {inferred && (
          <span className="text-amber-400/80">TOPOLOGY: INFERRED</span>
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

function HealthPanel({ health }: { health: number | null }) {
  const SEGMENTS = 24;
  const active = health === null ? 0 : Math.round((health / 100) * SEGMENTS);
  const tone =
    health === null
      ? "text-slate-500"
      : health >= 60
        ? "text-emerald-400"
        : health >= 30
          ? "text-amber-400"
          : "text-rose-400";
  const barTone =
    health === null
      ? "bg-slate-700"
      : health >= 60
        ? "bg-emerald-400"
        : health >= 30
          ? "bg-amber-400"
          : "bg-rose-400";

  return (
    <section className="glass absolute top-16 left-4 w-64 rounded-sm p-4">
      <h2 className="text-[10px] font-semibold tracking-[0.25em] text-slate-400">
        NETWORK HEALTH INDEX
      </h2>
      <p className={`mt-2 font-mono text-4xl font-bold ${tone}`}>
        {health === null ? "--" : `${health}%`}
      </p>
      <div className="mt-3 flex gap-[2px]">
        {Array.from({ length: SEGMENTS }).map((_, i) => (
          <div
            key={i}
            className={`h-3 flex-1 rounded-[1px] ${i < active ? barTone : "bg-slate-800"}`}
          />
        ))}
      </div>
      <p className="mt-2 font-mono text-[10px] text-slate-500">
        MEAN RSSI QUALITY · −100…−40 dBm
      </p>
    </section>
  );
}

function UavPanel({
  uav,
}: {
  uav: {
    level: "standby" | "warn" | "critical";
    targetId: number | undefined;
    mid: { x: number; y: number } | null;
  };
}) {
  const critical = uav.level === "critical";
  const warn = uav.level === "warn";

  return (
    <section
      className={`pointer-events-auto rounded-sm border p-4 ${
        critical
          ? "animate-flash-critical"
          : warn
            ? "glass border-amber-500/50"
            : "glass"
      }`}
    >
      <h2 className="text-[10px] font-semibold tracking-[0.25em] text-slate-400">
        UAV BRIDGE INTERVENTION
      </h2>
      {critical ? (
        <>
          <p className="mt-2 font-mono text-lg font-bold tracking-wider text-rose-300">
            UAV DISPATCH REQUIRED
          </p>
          <p className="mt-1 font-mono text-[11px] text-rose-200/80">
            TARGET NODE {uav.targetId ?? "—"} · LINK SEVERED
          </p>
        </>
      ) : warn ? (
        <>
          <p className="mt-2 font-mono text-lg font-bold tracking-wider text-amber-300">
            LINK STRAIN DETECTED
          </p>
          <p className="mt-1 font-mono text-[11px] text-amber-200/80">
            NODE {uav.targetId} BELOW {DEGRADED_RSSI} dBm
          </p>
        </>
      ) : (
        <>
          <p className="mt-2 font-mono text-lg font-bold tracking-wider text-emerald-400">
            STANDBY
          </p>
          <p className="mt-1 font-mono text-[11px] text-slate-500">
            MESH NOMINAL · NO INTERVENTION
          </p>
        </>
      )}
      <div className="mt-3 border-t border-slate-700/50 pt-2 font-mono text-[11px] text-slate-400">
        <span className="text-slate-500">DEPLOY MIDPOINT </span>
        {uav.mid ? (
          <span className={critical ? "text-rose-300" : warn ? "text-amber-300" : ""}>
            X {uav.mid.x.toFixed(1)} · Y {uav.mid.y.toFixed(1)}
          </span>
        ) : (
          <span>—</span>
        )}
      </div>
    </section>
  );
}

function TelemetryRail({
  rows,
}: {
  rows: {
    id: number;
    isRoot: boolean;
    dropped: boolean;
    degraded: boolean;
    rssi: number | "disconnected" | undefined;
  }[];
}) {
  return (
    <aside className="glass terminal-scroll pointer-events-auto min-h-0 flex-1 overflow-y-auto rounded-sm p-3">
      <h2 className="mb-2 text-[10px] font-semibold tracking-[0.25em] text-slate-400">
        NODE TELEMETRY
      </h2>
      {rows.length === 0 && (
        <p className="font-mono text-[11px] text-slate-600">NO CONTACTS</p>
      )}
      <div className="space-y-2">
        {rows.map((row) => {
          const dot = row.dropped
            ? "bg-rose-500"
            : row.isRoot
              ? "bg-sky-400"
              : row.degraded
                ? "bg-amber-400"
                : "bg-emerald-400";
          const pct = typeof row.rssi === "number" ? rssiPercent(row.rssi) : 0;
          const barColor =
            pct >= 60 ? "bg-emerald-400" : pct >= 30 ? "bg-amber-400" : "bg-rose-400";
          return (
            <div
              key={row.id}
              className={`rounded-sm border px-2.5 py-2 ${
                row.dropped
                  ? "border-rose-900/60 bg-rose-950/30"
                  : row.degraded
                    ? "border-amber-800/50 bg-amber-950/20"
                    : "border-slate-800 bg-slate-900/50"
              }`}
            >
              <div className="flex items-center justify-between">
                <span className="flex items-center gap-2 font-mono text-[11px] text-slate-200">
                  <span className={`h-1.5 w-1.5 rounded-full ${dot}`} />
                  {row.id}
                </span>
                <span className="font-mono text-[9px] tracking-wider text-slate-500">
                  {row.isRoot
                    ? "GATEWAY"
                    : row.dropped
                      ? "LOST"
                      : row.degraded
                        ? "DEGRADED"
                        : "NODE"}
                </span>
              </div>
              <div className="mt-1.5 flex items-center gap-2">
                {typeof row.rssi === "number" ? (
                  <>
                    <div className="h-1 flex-1 overflow-hidden rounded-full bg-slate-800">
                      <div
                        className={`h-full rounded-full transition-all duration-300 ${barColor}`}
                        style={{ width: `${pct}%` }}
                      />
                    </div>
                    <span className="w-14 text-right font-mono text-[11px] text-slate-300">
                      {row.rssi} dBm
                    </span>
                  </>
                ) : (
                  <span className="font-mono text-[10px] text-slate-600">
                    {row.rssi === "disconnected" ? "ROOT · NO PARENT LINK" : "NO TELEMETRY"}
                  </span>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </aside>
  );
}

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
    <section className="glass absolute inset-x-4 bottom-4 h-40 rounded-sm">
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
            <span className={e.severity === "critical" ? "text-rose-200" : "text-slate-300"}>
              {e.text}
            </span>
          </p>
        ))}
      </div>
    </section>
  );
}
