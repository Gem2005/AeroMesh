"use client";

/**
 * AeroMesh Dashboard — real-time visualization of the Self-Healing Ad-Hoc
 * Wireless Network with UAV Bridge.
 *
 * Setup:
 *   cd dashboard
 *   npm install                 # installs next, react, tailwind, react-force-graph-2d
 *   npm run dev                 # dashboard at http://localhost:3000
 *
 * Requires the Python bridge to be running first:
 *   pip install pyserial websockets
 *   python ../bridge.py         # serves ws://localhost:8765 from COM9
 *
 * Data flow:
 *   ESP32 Gateway (COM9) → bridge.py → WebSocket → this page
 *
 * Re-render strategy (important for rapid serial updates):
 *   - Topology packets are the ONLY thing that replaces the graphData object,
 *     so the force simulation is never reset by telemetry spam.
 *   - Node objects are REUSED across topology updates so nodes keep their
 *     simulated positions when the mesh self-heals.
 *   - Telemetry is mirrored into a ref that the canvas paint callback reads on
 *     every animation frame — RSSI labels update live on the graph without a
 *     single React re-render of the ForceGraph component.
 */

import dynamic from "next/dynamic";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { NodeObject } from "react-force-graph-2d";

// ForceGraph2D touches `window`, so it must be loaded client-side only.
const ForceGraph2D = dynamic(() => import("react-force-graph-2d"), {
  ssr: false,
  loading: () => (
    <div className="flex h-full items-center justify-center text-slate-500">
      Loading graph engine…
    </div>
  ),
});

// 8765 instead of 8080: Apache/XAMPP occupies 8080 on the dev machine.
// Must match the --ws-port of bridge.py.
const WS_URL = "ws://localhost:8765";
const WS_RETRY_MS = 2000;
/** How long a dropped node lingers (fading red) before it is purged. */
const DROP_LINGER_MS = 4000;

/* ------------------------------------------------------------------ */
/* Incoming packet types (from bridge.py)                              */
/* ------------------------------------------------------------------ */

/** {"node_id":1693866525,"status":"online","parent_rssi":-43} */
interface TelemetryPacket {
  node_id: number;
  status: string;
  /** The root/gateway node reports "disconnected" (it has no parent). */
  parent_rssi: number | "disconnected";
}

/** painlessMesh subConnectionJson(): {"nodeId":123,"subs":[{...}]} */
interface TopologyPacket {
  nodeId: number;
  subs?: TopologyPacket[];
}

function isTelemetry(msg: unknown): msg is TelemetryPacket {
  return typeof msg === "object" && msg !== null && "node_id" in msg;
}

function isTopology(msg: unknown): msg is TopologyPacket {
  return typeof msg === "object" && msg !== null && "nodeId" in msg;
}

/* ------------------------------------------------------------------ */
/* Graph types                                                         */
/* ------------------------------------------------------------------ */

interface GraphNode {
  id: number;
  isRoot: boolean;
  status: "online" | "dropped";
  droppedAt?: number;
  // Injected by the force engine at runtime:
  x?: number;
  y?: number;
}

interface GraphLink {
  source: number | GraphNode;
  target: number | GraphNode;
}

interface GraphData {
  nodes: GraphNode[];
  links: GraphLink[];
}

type TelemetryEntry = TelemetryPacket & { lastSeen: number };
type TelemetryMap = Record<number, TelemetryEntry>;

/** Recursively flatten the nested painlessMesh topology into nodes + links. */
function flattenTopology(root: TopologyPacket): {
  nodes: { id: number; isRoot: boolean }[];
  links: { source: number; target: number }[];
} {
  const nodes: { id: number; isRoot: boolean }[] = [];
  const links: { source: number; target: number }[] = [];

  const walk = (node: TopologyPacket, parent: number | null) => {
    nodes.push({ id: node.nodeId, isRoot: parent === null });
    if (parent !== null) links.push({ source: parent, target: node.nodeId });
    node.subs?.forEach((sub) => walk(sub, node.nodeId));
  };

  walk(root, null);
  return { nodes, links };
}

/* ------------------------------------------------------------------ */
/* RSSI helpers (for the sidebar signal bars)                          */
/* ------------------------------------------------------------------ */

function rssiPercent(rssi: number): number {
  // Map -90 dBm (poor) … -30 dBm (excellent) to 0…100 %.
  return Math.max(0, Math.min(100, Math.round(((rssi + 90) / 60) * 100)));
}

function rssiBarColor(rssi: number): string {
  if (rssi > -55) return "bg-emerald-400";
  if (rssi > -70) return "bg-yellow-400";
  return "bg-red-400";
}

/* ------------------------------------------------------------------ */
/* Page component                                                      */
/* ------------------------------------------------------------------ */

type WsStatus = "connecting" | "connected" | "disconnected";

export default function Home() {
  const [graphData, setGraphData] = useState<GraphData>({ nodes: [], links: [] });
  const [telemetry, setTelemetry] = useState<TelemetryMap>({});
  const [wsStatus, setWsStatus] = useState<WsStatus>("connecting");

  // Mirror of `telemetry` readable from canvas callbacks without re-rendering.
  const telemetryRef = useRef<TelemetryMap>({});
  const purgeTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // True once a real {"nodeId":..,"subs":[..]} packet has arrived. Until then
  // we infer a star topology from telemetry so nodes appear immediately even
  // if the firmware doesn't print subConnectionJson().
  const [hasTopology, setHasTopology] = useState(false);
  const hasTopologyRef = useRef(false);

  /* ---------------- Topology handling ---------------- */

  const applyTopology = useCallback((packet: TopologyPacket) => {
    if (!hasTopologyRef.current) {
      hasTopologyRef.current = true;
      setHasTopology(true);
    }
    const { nodes: liveNodes, links } = flattenTopology(packet);
    const liveIds = new Set(liveNodes.map((n) => n.id));
    const now = Date.now();

    setGraphData((prev) => {
      const prevById = new Map(prev.nodes.map((n) => [n.id, n]));

      // Reuse existing node objects so the force layout keeps positions.
      const nodes: GraphNode[] = liveNodes.map(({ id, isRoot }) => {
        const existing = prevById.get(id);
        if (existing) {
          existing.isRoot = isRoot;
          existing.status = "online";
          delete existing.droppedAt;
          return existing;
        }
        return { id, isRoot, status: "online" };
      });

      // Nodes missing from the new topology linger briefly in red,
      // visualizing the failure before the mesh view settles.
      for (const node of prev.nodes) {
        if (liveIds.has(node.id)) continue;
        if (node.status !== "dropped") {
          node.status = "dropped";
          node.droppedAt = now;
        }
        if (now - (node.droppedAt ?? now) < DROP_LINGER_MS) nodes.push(node);
      }

      return { nodes, links };
    });

    // Purge lingering dropped nodes after the fade-out window.
    if (purgeTimer.current) clearTimeout(purgeTimer.current);
    purgeTimer.current = setTimeout(() => {
      setGraphData((prev) => ({
        nodes: prev.nodes.filter(
          (n) =>
            n.status !== "dropped" ||
            Date.now() - (n.droppedAt ?? 0) < DROP_LINGER_MS,
        ),
        links: prev.links,
      }));
    }, DROP_LINGER_MS + 250);
  }, []);

  /** Fallback: build a star topology (gateway at center) from telemetry only. */
  const applyTelemetryFallback = useCallback(() => {
    const entries = Object.values(telemetryRef.current);
    const rootId = entries.find((e) => e.parent_rssi === "disconnected")?.node_id;

    setGraphData((prev) => {
      // Skip the update entirely if nothing structural changed (avoids
      // reheating the force simulation on every 2s telemetry tick).
      const unchanged =
        prev.nodes.length === entries.length &&
        entries.every((e) =>
          prev.nodes.some(
            (n) => n.id === e.node_id && n.isRoot === (e.node_id === rootId),
          ),
        );
      if (unchanged) return prev;

      const prevById = new Map(prev.nodes.map((n) => [n.id, n]));
      const nodes: GraphNode[] = entries.map((e) => {
        const isRoot = e.node_id === rootId;
        const existing = prevById.get(e.node_id);
        if (existing) {
          existing.isRoot = isRoot;
          existing.status = "online";
          delete existing.droppedAt;
          return existing;
        }
        return { id: e.node_id, isRoot, status: "online" };
      });

      const links: GraphLink[] =
        rootId === undefined
          ? []
          : entries
              .filter((e) => e.node_id !== rootId)
              .map((e) => ({ source: rootId, target: e.node_id }));

      return { nodes, links };
    });
  }, []);

  /* ---------------- WebSocket lifecycle ---------------- */

  useEffect(() => {
    let ws: WebSocket | null = null;
    let retryTimer: ReturnType<typeof setTimeout> | undefined;
    let disposed = false;

    const connect = () => {
      setWsStatus("connecting");
      ws = new WebSocket(WS_URL);

      ws.onopen = () => setWsStatus("connected");

      ws.onmessage = (event) => {
        let msg: unknown;
        try {
          msg = JSON.parse(event.data as string);
        } catch {
          return; // bridge already filters, but be defensive
        }

        if (isTelemetry(msg)) {
          const entry: TelemetryEntry = { ...msg, lastSeen: Date.now() };
          // New object identity only for the map — node/link objects untouched.
          telemetryRef.current = { ...telemetryRef.current, [msg.node_id]: entry };
          setTelemetry(telemetryRef.current);
          if (!hasTopologyRef.current) applyTelemetryFallback();
        } else if (isTopology(msg)) {
          applyTopology(msg);
        }
      };

      ws.onclose = () => {
        if (disposed) return;
        setWsStatus("disconnected");
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
  }, [applyTopology, applyTelemetryFallback]);

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

  /* ---------------- Canvas painting ---------------- */

  const paintNode = useCallback(
    (nodeObj: NodeObject, ctx: CanvasRenderingContext2D, globalScale: number) => {
      const node = nodeObj as unknown as GraphNode;
      const x = node.x ?? 0;
      const y = node.y ?? 0;
      const dropped = node.status === "dropped";
      const radius = node.isRoot ? 8 : 6;

      // Dropped nodes fade out over the linger window.
      const age = dropped ? Date.now() - (node.droppedAt ?? 0) : 0;
      ctx.globalAlpha = dropped ? Math.max(0.15, 1 - age / DROP_LINGER_MS) : 1;

      // Soft glow
      ctx.beginPath();
      ctx.arc(x, y, radius + 4, 0, 2 * Math.PI);
      ctx.fillStyle = dropped
        ? "rgba(239,68,68,0.15)"
        : node.isRoot
          ? "rgba(56,189,248,0.18)"
          : "rgba(34,197,94,0.18)";
      ctx.fill();

      // Node body
      ctx.beginPath();
      ctx.arc(x, y, radius, 0, 2 * Math.PI);
      ctx.fillStyle = dropped ? "#ef4444" : node.isRoot ? "#38bdf8" : "#22c55e";
      ctx.fill();
      ctx.lineWidth = 1.5 / globalScale;
      ctx.strokeStyle = "rgba(255,255,255,0.7)";
      ctx.stroke();

      // Labels
      const fontSize = 12 / globalScale;
      ctx.font = `${fontSize}px monospace`;
      ctx.textAlign = "center";
      ctx.textBaseline = "top";
      ctx.fillStyle = dropped ? "#fca5a5" : "#e2e8f0";
      const label = node.isRoot ? `GW ${node.id}` : String(node.id);
      ctx.fillText(label, x, y + radius + 3 / globalScale);

      // Live RSSI straight from the telemetry ref (no React re-render needed).
      const t = telemetryRef.current[node.id];
      if (!dropped && t && typeof t.parent_rssi === "number") {
        ctx.font = `${fontSize * 0.85}px monospace`;
        ctx.fillStyle = "#94a3b8";
        ctx.fillText(`${t.parent_rssi} dBm`, x, y + radius + 3 / globalScale + fontSize + 1);
      }

      ctx.globalAlpha = 1;
    },
    [],
  );

  /* ---------------- Sidebar rows ---------------- */

  const sidebarRows = useMemo(() => {
    // Union of nodes known from topology and nodes only seen via telemetry.
    const ids = new Set<number>(graphData.nodes.map((n) => n.id));
    Object.keys(telemetry).forEach((id) => ids.add(Number(id)));

    return [...ids]
      .map((id) => {
        const graphNode = graphData.nodes.find((n) => n.id === id);
        return {
          id,
          isRoot: graphNode?.isRoot ?? false,
          dropped: graphNode ? graphNode.status === "dropped" : true,
          rssi: telemetry[id]?.parent_rssi,
        };
      })
      .sort((a, b) => Number(b.isRoot) - Number(a.isRoot) || a.id - b.id);
  }, [graphData, telemetry]);

  const onlineCount = graphData.nodes.filter((n) => n.status === "online").length;

  /* ---------------- Render ---------------- */

  return (
    <main className="flex h-screen w-full overflow-hidden bg-slate-950 text-slate-100">
      {/* Graph area */}
      <div ref={graphContainer} className="relative min-w-0 flex-1">
        <ForceGraph2D
          width={dims.width}
          height={dims.height}
          graphData={graphData}
          backgroundColor="#020617"
          nodeCanvasObject={paintNode}
          nodeRelSize={6}
          linkColor={() => "rgba(148,163,184,0.45)"}
          linkWidth={1.5}
          linkDirectionalParticles={2}
          linkDirectionalParticleWidth={2.5}
          linkDirectionalParticleColor={() => "#38bdf8"}
          cooldownTime={3000}
        />

        {/* Top-left status overlay */}
        <div className="pointer-events-none absolute left-4 top-4 rounded-lg border border-slate-800 bg-slate-900/80 px-4 py-3 backdrop-blur">
          <h1 className="text-sm font-semibold tracking-wide text-sky-300">
            AeroMesh — Self-Healing UAV Mesh
          </h1>
          <p className="mt-1 text-xs text-slate-400">
            {onlineCount} node{onlineCount === 1 ? "" : "s"} online ·{" "}
            {graphData.links.length} link{graphData.links.length === 1 ? "" : "s"}
            {!hasTopology && graphData.nodes.length > 0 && (
              <span className="text-amber-400/80"> · topology inferred from telemetry</span>
            )}
          </p>
        </div>

        {/* Waiting hint */}
        {graphData.nodes.length === 0 && (
          <div className="pointer-events-none absolute inset-0 flex items-center justify-center">
            <p className="text-sm text-slate-600">
              {wsStatus === "connected"
                ? "Connected — waiting for mesh topology…"
                : "Waiting for bridge at " + WS_URL + " …"}
            </p>
          </div>
        )}
      </div>

      {/* Sidebar */}
      <aside className="flex w-80 shrink-0 flex-col border-l border-slate-800 bg-slate-900/60">
        <div className="border-b border-slate-800 px-5 py-4">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-semibold uppercase tracking-wider text-slate-300">
              Node Telemetry
            </h2>
            <span
              className={`flex items-center gap-1.5 rounded-full px-2 py-0.5 text-[11px] font-medium ${
                wsStatus === "connected"
                  ? "bg-emerald-500/15 text-emerald-300"
                  : wsStatus === "connecting"
                    ? "bg-yellow-500/15 text-yellow-300"
                    : "bg-red-500/15 text-red-300"
              }`}
            >
              <span
                className={`h-1.5 w-1.5 rounded-full ${
                  wsStatus === "connected"
                    ? "bg-emerald-400"
                    : wsStatus === "connecting"
                      ? "animate-pulse bg-yellow-400"
                      : "bg-red-400"
                }`}
              />
              {wsStatus === "connected"
                ? "Bridge Live"
                : wsStatus === "connecting"
                  ? "Connecting"
                  : "Bridge Down"}
            </span>
          </div>
        </div>

        <div className="flex-1 space-y-3 overflow-y-auto px-4 py-4">
          {sidebarRows.length === 0 && (
            <p className="px-1 text-xs text-slate-500">
              No nodes discovered yet. Power up the mesh and start bridge.py.
            </p>
          )}

          {sidebarRows.map((row) => (
            <div
              key={row.id}
              className={`rounded-lg border px-3 py-2.5 transition-colors ${
                row.dropped
                  ? "border-red-900/60 bg-red-950/30"
                  : "border-slate-800 bg-slate-900"
              }`}
            >
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <span
                    className={`h-2 w-2 rounded-full ${
                      row.dropped
                        ? "bg-red-500"
                        : row.isRoot
                          ? "bg-sky-400"
                          : "bg-emerald-400"
                    }`}
                  />
                  <span className="font-mono text-xs text-slate-200">{row.id}</span>
                </div>
                <span className="text-[10px] uppercase tracking-wide text-slate-500">
                  {row.isRoot ? "Gateway" : row.dropped ? "Lost" : "Node"}
                </span>
              </div>

              <div className="mt-2">
                {typeof row.rssi === "number" ? (
                  <div className="flex items-center gap-2">
                    <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-slate-800">
                      <div
                        className={`h-full rounded-full transition-all duration-300 ${rssiBarColor(row.rssi)}`}
                        style={{ width: `${rssiPercent(row.rssi)}%` }}
                      />
                    </div>
                    <span className="w-16 text-right font-mono text-xs text-slate-300">
                      {row.rssi} dBm
                    </span>
                  </div>
                ) : (
                  <span className="text-xs text-slate-500">
                    {row.rssi === "disconnected"
                      ? "Root node — no parent link"
                      : "No telemetry yet"}
                  </span>
                )}
              </div>
            </div>
          ))}
        </div>

        <div className="border-t border-slate-800 px-5 py-3 text-[11px] text-slate-600">
          ESP32 painlessMesh · COM9 @ 115200 · {WS_URL}
        </div>
      </aside>
    </main>
  );
}
