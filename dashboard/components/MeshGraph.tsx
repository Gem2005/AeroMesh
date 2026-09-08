"use client";

/**
 * MeshGraph — the physics-driven force graph for AeroMesh Command.
 *
 * Exists as a separate client component (loaded via next/dynamic from the
 * page) because next/dynamic cannot forward refs, and we need direct access
 * to the ForceGraph2D methods ref to tune the d3 forces:
 *
 *   - RSSI -> link distance mapping (strong signal pulls nodes together)
 *   - mechanical, non-bouncy motion (high velocity decay, firm springs)
 *   - outward velocity "drift" kick for failed nodes
 *
 * All live signal values are read from `telemetryRef` inside per-frame canvas
 * callbacks, so signal changes repaint WITHOUT any React re-render. The
 * component is memoized: it only re-renders on structural graph changes.
 */

import { memo, useCallback, useEffect, useRef } from "react";
import ForceGraph2D, {
  type ForceGraphMethods,
  type LinkObject,
  type NodeObject,
} from "react-force-graph-2d";
import {
  DEGRADED_RSSI,
  DROP_LINGER_MS,
  idOf,
  rssiToDistance,
  type GraphData,
  type GraphNode,
  type TelemetryMap,
} from "@/lib/mesh";

interface MeshGraphProps {
  graphData: GraphData;
  telemetryRef: React.RefObject<TelemetryMap>;
  width: number;
  height: number;
}

const COLOR = {
  root: "#38bdf8", // sky    — gateway / mesh root
  healthy: "#34d399", // emerald
  degraded: "#fbbf24", // amber
  failed: "#f43f5e", // crimson
};

/** Minimal typings for the d3 forces we tune (avoids a d3-force dependency). */
interface D3LinkForce {
  distance: (fn: (link: LinkObject) => number) => D3LinkForce;
  strength: (n: number) => D3LinkForce;
}
interface D3ChargeForce {
  strength: (n: number) => D3ChargeForce;
}

function MeshGraph({ graphData, telemetryRef, width, height }: MeshGraphProps) {
  const fgRef = useRef<ForceGraphMethods | undefined>(undefined);

  /** Live RSSI of a link = the child (target) node's parent_rssi. */
  const linkRssi = useCallback(
    (link: LinkObject): number => {
      const t = telemetryRef.current?.[idOf(link.target as number | GraphNode)];
      return typeof t?.parent_rssi === "number" ? t.parent_rssi : -60;
    },
    [telemetryRef],
  );

  /* -------- d3 force tuning: deliberate, mechanical motion -------- */
  useEffect(() => {
    const fg = fgRef.current;
    if (!fg) return;
    (fg.d3Force("charge") as D3ChargeForce | undefined)?.strength(-220);
    (fg.d3Force("link") as D3LinkForce | undefined)
      ?.distance((link) => rssiToDistance(linkRssi(link)))
      .strength(0.8);
  }, [linkRssi, graphData]); // reapply so the accessor binds to new links

  /* -------- RSSI breathing: reheat only when signal actually moved -------- */
  const rssiSnapshot = useRef("");
  useEffect(() => {
    const interval = setInterval(() => {
      const fg = fgRef.current;
      if (!fg) return;
      const snap = Object.values(telemetryRef.current ?? {})
        .map((t) => `${t.node_id}:${t.parent_rssi}`)
        .sort()
        .join("|");
      if (snap !== rssiSnapshot.current) {
        rssiSnapshot.current = snap;
        fg.d3ReheatSimulation(); // link distances re-evaluate -> spacing breathes
      }
    }, 1000);
    return () => clearInterval(interval);
  }, [telemetryRef]);

  /* -------- failure drift: kick freshly dropped nodes outward -------- */
  useEffect(() => {
    const online = graphData.nodes.filter(
      (n) => n.status === "online" && n.x !== undefined,
    );
    if (graphData.nodes.length === 0) return;
    const cx = online.length
      ? online.reduce((s, n) => s + (n.x ?? 0), 0) / online.length
      : 0;
    const cy = online.length
      ? online.reduce((s, n) => s + (n.y ?? 0), 0) / online.length
      : 0;

    let kicked = false;
    for (const node of graphData.nodes) {
      if (node.status !== "dropped" || node.kicked) continue;
      node.kicked = true;
      kicked = true;
      const dx = (node.x ?? 0) - cx;
      const dy = (node.y ?? 0) - cy;
      const mag = Math.hypot(dx, dy) || 1;
      node.vx = (dx / mag) * 7; // outward velocity: severed node drifts away
      node.vy = (dy / mag) * 7;
    }
    if (kicked) fgRef.current?.d3ReheatSimulation();
  }, [graphData]);

  /* -------- canvas painting: nodes -------- */
  const paintNode = useCallback(
    (nodeObj: NodeObject, ctx: CanvasRenderingContext2D, globalScale: number) => {
      const node = nodeObj as unknown as GraphNode;
      const x = node.x ?? 0;
      const y = node.y ?? 0;

      const t = telemetryRef.current?.[node.id];
      const rssi = typeof t?.parent_rssi === "number" ? t.parent_rssi : undefined;

      const dropped = node.status === "dropped";
      const degraded = !dropped && rssi !== undefined && rssi < DEGRADED_RSSI;
      const color = dropped
        ? COLOR.failed
        : node.isRoot
          ? COLOR.root
          : degraded
            ? COLOR.degraded
            : COLOR.healthy;

      // Failed nodes fade while drifting away.
      const age = dropped ? Date.now() - (node.droppedAt ?? 0) : 0;
      ctx.globalAlpha = dropped ? Math.max(0.1, 1 - age / DROP_LINGER_MS) : 1;

      const radius = node.isRoot ? 7 : 5.5;

      // Glow halo
      ctx.shadowColor = color;
      ctx.shadowBlur = degraded || dropped ? 22 : 14;
      ctx.beginPath();
      ctx.arc(x, y, radius, 0, 2 * Math.PI);
      ctx.fillStyle = color;
      ctx.fill();
      ctx.shadowBlur = 0;

      // Precision ring
      ctx.beginPath();
      ctx.arc(x, y, radius + 3.5, 0, 2 * Math.PI);
      ctx.lineWidth = 1 / globalScale;
      ctx.strokeStyle = `${color}55`;
      ctx.stroke();

      // Root gets an outer targeting reticle
      if (node.isRoot && !dropped) {
        ctx.beginPath();
        ctx.setLineDash([3, 3]);
        ctx.arc(x, y, radius + 8, 0, 2 * Math.PI);
        ctx.strokeStyle = `${COLOR.root}66`;
        ctx.stroke();
        ctx.setLineDash([]);
      }

      // Labels (strict monospace for data)
      const fontSize = 11 / globalScale;
      ctx.textAlign = "center";
      ctx.textBaseline = "top";
      ctx.font = `${fontSize}px ui-monospace, monospace`;
      ctx.fillStyle = dropped ? "#fda4af" : "#e2e8f0";
      ctx.fillText(
        node.isRoot ? `ROOT ${node.id}` : String(node.id),
        x,
        y + radius + 6 / globalScale,
      );

      const sub = dropped
        ? "LOST"
        : node.isRoot
          ? "GATEWAY"
          : rssi !== undefined
            ? `${rssi} dBm`
            : "NO TLM";
      ctx.font = `${fontSize * 0.85}px ui-monospace, monospace`;
      ctx.fillStyle = dropped ? COLOR.failed : degraded ? COLOR.degraded : "#64748b";
      ctx.fillText(sub, x, y + radius + 6 / globalScale + fontSize + 1 / globalScale);

      ctx.globalAlpha = 1;
    },
    [telemetryRef],
  );

  /* -------- canvas painting: links (live color/dash from telemetry) -------- */
  const paintLink = useCallback(
    (linkObj: LinkObject, ctx: CanvasRenderingContext2D, globalScale: number) => {
      const source = linkObj.source as GraphNode | undefined;
      const target = linkObj.target as GraphNode | undefined;
      if (
        typeof source !== "object" ||
        typeof target !== "object" ||
        source.x === undefined ||
        target.x === undefined
      )
        return;

      const rssi = linkRssi(linkObj);
      const degraded = rssi < DEGRADED_RSSI;

      ctx.save();
      ctx.beginPath();
      ctx.setLineDash(degraded ? [5, 4] : []);
      ctx.moveTo(source.x, source.y ?? 0);
      ctx.lineTo(target.x, target.y ?? 0);
      ctx.strokeStyle = degraded
        ? "rgba(251, 191, 36, 0.85)"
        : "rgba(52, 211, 153, 0.35)";
      ctx.lineWidth = (degraded ? 1.8 : 1.2) / globalScale;
      if (degraded) {
        ctx.shadowColor = COLOR.degraded;
        ctx.shadowBlur = 8;
      }
      ctx.stroke();
      ctx.restore();
    },
    [linkRssi],
  );

  return (
    <ForceGraph2D
      ref={fgRef}
      width={width}
      height={height}
      graphData={graphData}
      backgroundColor="rgba(0,0,0,0)"
      nodeCanvasObject={paintNode}
      linkCanvasObject={paintLink}
      linkDirectionalParticles={2}
      linkDirectionalParticleWidth={2}
      linkDirectionalParticleSpeed={0.006}
      linkDirectionalParticleColor={() => "#38bdf8"}
      d3VelocityDecay={0.55}
      cooldownTime={15000}
    />
  );
}

// Memoized: re-renders only on structural graph changes / resize, never on
// telemetry ticks (those repaint through refs on animation frames).
export default memo(MeshGraph);
