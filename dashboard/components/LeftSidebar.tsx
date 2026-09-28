"use client";

import { memo } from "react";
import { type TopoNode, type AerialRelay, GATEWAY_ID } from "@/lib/mesh";

export interface LeftSidebarProps {
  metrics: {
    online: TopoNode[];
    jammed: TopoNode[];
    gateway: TopoNode | undefined;
    total: number;
  };
  nodes: TopoNode[];
  linksCount: number;
  relay: AerialRelay | null;
  operatorPosition: [number, number] | null;
  onSelectNode: (node: TopoNode) => void;
  collapsed: boolean;
  onToggleCollapse: () => void;
}

function LeftSidebar({
  metrics,
  nodes,
  linksCount,
  relay,
  operatorPosition,
  onSelectNode,
  collapsed,
  onToggleCollapse,
}: LeftSidebarProps) {
  // Calculate network strength % — strictly based on connected mesh nodes
  // When no nodes are connected (total = 0), healthPct is null (displays "--" and "STANDBY · NO NODES")
  const healthPct =
    metrics.total > 0
      ? Math.round((metrics.online.length / metrics.total) * 100)
      : null;

  const SEGMENTS = 20;
  const activeSegments =
    healthPct === null ? 0 : Math.round((healthPct / 100) * SEGMENTS);

  const healthTone =
    healthPct === null
      ? "text-slate-400"
      : healthPct >= 80
        ? "text-emerald-400"
        : healthPct >= 50
          ? "text-amber-400"
          : "text-rose-400";

  const barTone =
    healthPct === null
      ? "bg-slate-800/80"
      : healthPct >= 80
        ? "bg-emerald-400 shadow-[0_0_8px_rgba(52,211,153,0.7)]"
        : healthPct >= 50
          ? "bg-amber-400 shadow-[0_0_8px_rgba(251,191,36,0.7)]"
          : "bg-rose-400 shadow-[0_0_8px_rgba(244,63,94,0.7)]";

  const gatewayNode = metrics.gateway;
  const childNodes = nodes.filter((n) => n.id !== GATEWAY_ID);

  /* ================================================================== */
  /* CONTRACTED (SLIM) SIDEBAR                                          */
  /* ================================================================== */
  if (collapsed) {
    return (
      <aside
        className="glass-sidebar relative z-20 flex h-full w-16 shrink-0 flex-col transition-all duration-300 select-none"
        aria-label="Contracted Tactical Sidebar"
      >
        {/* Top Header bar matching TopBar glass aesthetic */}
        <div className="glass-subtle flex h-10 w-full shrink-0 items-center justify-center">
          <button
            onClick={onToggleCollapse}
            className="glass-pill flex h-6 w-6 items-center justify-center rounded text-slate-300 transition-all hover:border-cyan-400/50 hover:text-cyan-300 hover:shadow-[0_0_10px_rgba(34,211,238,0.3)]"
            title="Expand sidebar"
            aria-label="Expand sidebar"
          >
            <svg width="12" height="12" viewBox="0 0 16 16" fill="currentColor">
              <path d="M6 3l5 5-5 5V3z" />
            </svg>
          </button>
        </div>

        {/* 1. Strength percentage badge */}
        <div
          className="flex w-full flex-col items-center border-b border-white/10 py-2.5 text-center"
          title={`Network Strength: ${healthPct === null ? "Standby (No Nodes Connected)" : `${healthPct}%`}`}
        >
          <span className={`font-mono text-base font-extrabold leading-tight ${healthTone}`}>
            {healthPct === null ? "--" : `${healthPct}%`}
          </span>
          <span className="mt-0.5 font-mono text-[7px] font-bold tracking-widest text-slate-400">
            STR %
          </span>
        </div>

        {/* Vertical Metric Strip — styled as glossy pill badges */}
        <div className="flex flex-1 flex-col items-center justify-evenly py-2">
          {/* 2. Online nodes */}
          <div
            className="glass-pill flex w-12 flex-col items-center justify-center rounded py-1.5 text-center cursor-default group hover:border-emerald-400/40"
            title={`${metrics.online.length} Online Nodes`}
          >
            <div className="flex items-center gap-1">
              <span
                className={`h-1.5 w-1.5 rounded-full ${
                  metrics.online.length > 0
                    ? "bg-emerald-400 shadow-[0_0_8px_rgba(52,211,153,0.9)]"
                    : "bg-slate-700"
                }`}
              />
              <span
                className={`font-mono text-xs font-bold ${
                  metrics.online.length > 0 ? "text-emerald-300" : "text-slate-400"
                }`}
              >
                {metrics.online.length}
              </span>
            </div>
            <span className="mt-0.5 font-mono text-[7px] font-semibold tracking-wider text-slate-400 group-hover:text-slate-200">
              ONLINE
            </span>
          </div>

          {/* 3. Jammed nodes */}
          <div
            className="glass-pill flex w-12 flex-col items-center justify-center rounded py-1.5 text-center cursor-default group hover:border-rose-400/40"
            title={`${metrics.jammed.length} Jammed Nodes`}
          >
            <div className="flex items-center gap-1">
              <span
                className={`h-1.5 w-1.5 rounded-full ${
                  metrics.jammed.length > 0
                    ? "animate-pulse bg-rose-500 shadow-[0_0_10px_rgba(244,63,94,0.9)]"
                    : "bg-slate-700"
                }`}
              />
              <span
                className={`font-mono text-xs font-bold ${
                  metrics.jammed.length > 0 ? "text-rose-300" : "text-slate-400"
                }`}
              >
                {metrics.jammed.length}
              </span>
            </div>
            <span className="mt-0.5 font-mono text-[7px] font-semibold tracking-wider text-slate-400 group-hover:text-slate-200">
              JAMMED
            </span>
          </div>

          {/* 4. Root Gateway */}
          <div
            className="glass-pill flex w-12 flex-col items-center justify-center rounded py-1.5 text-center cursor-default group hover:border-cyan-400/40"
            title={
              gatewayNode
                ? `Root Gateway: 0x${gatewayNode.id.toString(16).toUpperCase()}`
                : "Root Gateway Searching"
            }
          >
            <div className="flex items-center gap-1">
              <span className={`text-[10px] ${gatewayNode ? "text-cyan-400" : "text-slate-600"}`}>
                ⬡
              </span>
              <span
                className={`font-mono text-xs font-bold ${
                  gatewayNode ? "text-cyan-300" : "text-slate-400"
                }`}
              >
                {gatewayNode ? "1" : "0"}
              </span>
            </div>
            <span className="mt-0.5 font-mono text-[7px] font-semibold tracking-wider text-slate-400 group-hover:text-slate-200">
              ROOT
            </span>
          </div>

          {/* 5. Operator Station */}
          <div
            className="glass-pill flex w-12 flex-col items-center justify-center rounded py-1.5 text-center cursor-default group hover:border-amber-400/40"
            title={operatorPosition ? "Operator Station Online" : "Operator Standby"}
          >
            <div className="flex items-center gap-1">
              <span className={`rotate-45 text-[8px] ${operatorPosition ? "text-amber-400" : "text-slate-600"}`}>
                ◆
              </span>
              <span
                className={`font-mono text-xs font-bold ${
                  operatorPosition ? "text-amber-300" : "text-slate-400"
                }`}
              >
                {operatorPosition ? "1" : "0"}
              </span>
            </div>
            <span className="mt-0.5 font-mono text-[7px] font-semibold tracking-wider text-slate-400 group-hover:text-slate-200">
              OP (C2)
            </span>
          </div>

          {/* 6. Aerial Relay */}
          <div
            className="glass-pill flex w-12 flex-col items-center justify-center rounded py-1.5 text-center cursor-default group hover:border-cyan-400/40"
            title={relay ? "Aerial Relay Active" : "Relay Standby"}
          >
            <div className="flex items-center gap-1">
              <span className={`text-[9px] ${relay ? "text-cyan-300" : "text-slate-600"}`}>
                ▲
              </span>
              <span
                className={`font-mono text-xs font-bold ${
                  relay ? "text-cyan-300" : "text-slate-400"
                }`}
              >
                {relay ? "1" : "0"}
              </span>
            </div>
            <span className="mt-0.5 font-mono text-[7px] font-semibold tracking-wider text-slate-400 group-hover:text-slate-200">
              RELAY
            </span>
          </div>

          {/* 7. Mesh Links */}
          <div
            className="glass-pill flex w-12 flex-col items-center justify-center rounded py-1.5 text-center cursor-default group hover:border-cyan-400/40"
            title={`${linksCount} Mesh Links Active`}
          >
            <span
              className={`font-mono text-xs font-bold ${
                linksCount > 0 ? "text-cyan-400" : "text-slate-400"
              }`}
            >
              {linksCount}
            </span>
            <span className="mt-0.5 font-mono text-[7px] font-semibold tracking-wider text-slate-400 group-hover:text-slate-200">
              LINKS
            </span>
          </div>
        </div>
      </aside>
    );
  }

  /* ================================================================== */
  /* EXPANDED SIDEBAR                                                   */
  /* ================================================================== */
  return (
    <aside
      className="glass-sidebar relative z-20 flex h-full w-80 shrink-0 flex-col transition-all duration-300 select-none"
      aria-label="Expanded Tactical Sidebar"
    >
      {/* ---------------------------------------------------- */}
      {/* SECTION 1 HEADER: NETWORK STRENGTH (Glassy Header)   */}
      {/* ---------------------------------------------------- */}
      <div className="glass-subtle flex h-10 w-full shrink-0 items-center justify-between px-3">
        <div className="flex items-center gap-2">
          <span
            className={`h-1.5 w-1.5 rounded-full ${
              metrics.total > 0
                ? "animate-pulse bg-cyan-400 shadow-[0_0_10px_rgba(34,211,238,0.9)]"
                : "bg-slate-600"
            }`}
          />
          <h2 className="font-mono text-[10px] font-bold tracking-[0.2em] text-slate-200">
            NETWORK STRENGTH
          </h2>
        </div>
        <button
          onClick={onToggleCollapse}
          className="glass-pill flex h-6 w-6 items-center justify-center rounded text-slate-300 transition-all hover:border-cyan-400/50 hover:text-cyan-300 hover:shadow-[0_0_10px_rgba(34,211,238,0.3)]"
          title="Contract sidebar"
          aria-label="Contract sidebar"
        >
          <svg width="12" height="12" viewBox="0 0 16 16" fill="currentColor">
            <path d="M10 13l-5-5 5-5v10z" />
          </svg>
        </button>
      </div>

      {/* Strength Body (Glassy frosted container) */}
      <div className="p-3.5 space-y-3 border-b border-white/10 bg-white/[0.015]">
        {/* Big Health % Display */}
        <div className="flex items-baseline justify-between">
          <span className={`font-mono text-3xl font-extrabold tracking-tight ${healthTone}`}>
            {healthPct === null ? "--" : `${healthPct}%`}
          </span>
          <span className="font-mono text-[9px] font-medium tracking-wider text-slate-400">
            {healthPct === null
              ? "STANDBY · NO NODES"
              : healthPct >= 80
                ? "OPTIMAL LINK"
                : healthPct >= 50
                  ? "DEGRADED"
                  : "CRITICAL"}
          </span>
        </div>

        {/* 20-segment tactical LED bar */}
        <div className="flex gap-[2px]">
          {Array.from({ length: SEGMENTS }).map((_, i) => (
            <div
              key={i}
              className={`h-2 flex-1 rounded-[1px] transition-all duration-200 ${
                i < activeSegments ? barTone : "bg-slate-800/60"
              }`}
            />
          ))}
        </div>

        {/* Quick summary grid (Glassy pills) */}
        <div className="grid grid-cols-2 gap-1.5 pt-1 font-mono text-[10px]">
          <div className="glass-pill flex justify-between rounded px-2.5 py-1.5">
            <span className="text-slate-400">ONLINE</span>
            <span className={`font-bold ${metrics.online.length > 0 ? "text-emerald-400" : "text-slate-400"}`}>
              {metrics.online.length}
            </span>
          </div>
          <div className="glass-pill flex justify-between rounded px-2.5 py-1.5">
            <span className="text-slate-400">JAMMED</span>
            <span
              className={`font-bold ${
                metrics.jammed.length > 0 ? "animate-pulse text-rose-400" : "text-slate-400"
              }`}
            >
              {metrics.jammed.length}
            </span>
          </div>
          <div className="glass-pill flex justify-between rounded px-2.5 py-1.5">
            <span className="text-slate-400">LINKS</span>
            <span className={`font-bold ${linksCount > 0 ? "text-cyan-400" : "text-slate-400"}`}>
              {linksCount}
            </span>
          </div>
          <div className="glass-pill flex justify-between rounded px-2.5 py-1.5">
            <span className="text-slate-400">RELAY</span>
            <span className={`font-bold ${relay ? "text-cyan-400" : "text-slate-400"}`}>
              {relay ? "DEPLOYED" : "STANDBY"}
            </span>
          </div>
        </div>
      </div>

      {/* ---------------------------------------------------- */}
      {/* SECTION 2 HEADER: NETWORK ROSTER (Glassy Header)     */}
      {/* ---------------------------------------------------- */}
      <div className="glass-subtle flex h-10 w-full shrink-0 items-center justify-between px-3">
        <div className="flex items-center gap-2">
          <h3 className="font-mono text-[10px] font-bold tracking-[0.2em] text-slate-200">
            NETWORK ROSTER
          </h3>
          <span className="glass-pill rounded px-1.5 py-0.5 font-mono text-[9px] text-slate-300">
            {nodes.length + (operatorPosition ? 1 : 0)}
          </span>
        </div>
        <span className="font-mono text-[8px] tracking-wider text-slate-400">
          CLICK TO INSPECT
        </span>
      </div>

      {/* Scrollable Entity List (Rich glass cards) */}
      <div className="terminal-scroll flex-1 space-y-2 overflow-y-auto p-3">
        {/* 1. OPERATOR (OP) STATION */}
        {operatorPosition && (
          <div className="glass-card-operator rounded-md p-2.5">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <div className="flex h-5 w-5 items-center justify-center rounded bg-amber-500/25 text-amber-300 shadow-[0_0_10px_rgba(245,158,11,0.4)]">
                  <span className="rotate-45 text-[9px]">◆</span>
                </div>
                <div>
                  <div className="font-mono text-[11px] font-bold text-amber-200">
                    OPERATOR STATION (C2)
                  </div>
                  <div className="font-mono text-[9px] text-amber-400/90">
                    LOCAL GROUND COMMAND
                  </div>
                </div>
              </div>
              <span className="glass-pill rounded border-amber-500/40 bg-amber-500/10 px-1.5 py-0.5 font-mono text-[8px] font-bold tracking-wider text-amber-300">
                ONLINE
              </span>
            </div>
            <div className="mt-1.5 flex items-center justify-between font-mono text-[9px] text-slate-300">
              <span>
                GPS: {operatorPosition[0].toFixed(4)}, {operatorPosition[1].toFixed(4)}
              </span>
              <span className="text-amber-400/90 font-medium">C2 UPLINK</span>
            </div>
          </div>
        )}

        {/* 2. ROOT NODE (GATEWAY) */}
        {gatewayNode && (
          <div
            onClick={() => onSelectNode(gatewayNode)}
            className="glass-card-gateway group cursor-pointer rounded-md p-2.5"
          >
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <div className="flex h-5 w-5 items-center justify-center rounded bg-cyan-500/25 text-cyan-300 shadow-[0_0_10px_rgba(34,211,238,0.5)]">
                  <span className="font-mono text-[10px] font-bold">GW</span>
                </div>
                <div>
                  <div className="font-mono text-[11px] font-bold text-cyan-200 group-hover:text-cyan-100">
                    ROOT NODE (GATEWAY)
                  </div>
                  <div className="font-mono text-[9px] text-cyan-400/90">
                    0x{gatewayNode.id.toString(16).toUpperCase()}
                  </div>
                </div>
              </div>
              <span className="glass-pill rounded border-cyan-500/40 bg-cyan-500/10 px-1.5 py-0.5 font-mono text-[8px] font-bold tracking-wider text-cyan-300">
                ROOT
              </span>
            </div>
            <div className="mt-1.5 flex items-center justify-between font-mono text-[9px] text-slate-300">
              <span>
                GPS: {gatewayNode.lat.toFixed(4)}, {gatewayNode.lon.toFixed(4)}
              </span>
              <span className="font-semibold text-emerald-400">100% HEALTH</span>
            </div>
          </div>
        )}

        {/* 3. CHILD NODES */}
        {childNodes.map((node) => {
          const isJammed = node.status === "JAMMED";
          return (
            <div
              key={node.id}
              onClick={() => onSelectNode(node)}
              className={`group cursor-pointer rounded-md p-2.5 ${
                isJammed ? "glass-card-jammed" : "glass-card"
              }`}
            >
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <div
                    className={`flex h-5 w-5 items-center justify-center rounded text-[10px] ${
                      isJammed
                        ? "animate-pulse bg-rose-500/25 text-rose-400 shadow-[0_0_8px_rgba(244,63,94,0.6)]"
                        : "bg-emerald-500/25 text-emerald-400 shadow-[0_0_8px_rgba(52,211,153,0.4)]"
                    }`}
                  >
                    {isJammed ? "⚠" : "⬢"}
                  </div>
                  <div>
                    <div className="font-mono text-[11px] font-bold text-slate-200 group-hover:text-white">
                      CHILD NODE
                    </div>
                    <div className="font-mono text-[9px] text-slate-400">
                      0x{node.id.toString(16).toUpperCase()}
                    </div>
                  </div>
                </div>
                <span
                  className={`glass-pill rounded px-1.5 py-0.5 font-mono text-[8px] font-bold tracking-wider ${
                    isJammed
                      ? "animate-pulse border-rose-500/50 bg-rose-500/20 text-rose-300"
                      : "border-emerald-500/40 bg-emerald-500/10 text-emerald-300"
                  }`}
                >
                  {isJammed ? "JAMMED" : "ONLINE"}
                </span>
              </div>
              <div className="mt-1.5 flex items-center justify-between font-mono text-[9px] text-slate-300">
                <span>
                  GPS: {node.lat.toFixed(4)}, {node.lon.toFixed(4)}
                </span>
                <span className={isJammed ? "font-semibold text-rose-400" : "text-slate-400"}>
                  {isJammed ? "LINK LOST" : "FSPL ACTIVE"}
                </span>
              </div>
            </div>
          );
        })}

        {/* 4. AERIAL RELAY (if active) */}
        {relay && (
          <div className="glass-card-relay rounded-md p-2.5">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <div className="flex h-5 w-5 animate-pulse items-center justify-center rounded bg-cyan-400/25 text-cyan-300 shadow-[0_0_10px_rgba(34,211,238,0.5)]">
                  ▲
                </div>
                <div>
                  <div className="font-mono text-[11px] font-bold text-cyan-200">
                    AERIAL RELAY (UAV)
                  </div>
                  <div className="font-mono text-[9px] text-cyan-400">
                    BRIDGING NODE 0x{relay.targetNodeId.toString(16).toUpperCase()}
                  </div>
                </div>
              </div>
              <span className="glass-pill rounded border-cyan-400/50 bg-cyan-500/20 px-1.5 py-0.5 font-mono text-[8px] font-bold tracking-wider text-cyan-300">
                BRIDGING
              </span>
            </div>
            <div className="mt-1.5 flex items-center justify-between font-mono text-[9px] text-slate-300">
              <span>
                GPS: {relay.lat.toFixed(4)}, {relay.lon.toFixed(4)}
              </span>
              <span className="font-semibold text-cyan-400">ACTIVE</span>
            </div>
          </div>
        )}

        {/* Hint when awaiting child nodes */}
        {childNodes.length === 0 && (
          <div className="glass-pill rounded-md border-dashed border-white/20 p-3.5 text-center">
            <div className="font-mono text-[10px] text-slate-300">
              AWAITING CHILD NODES…
            </div>
            <div className="mt-0.5 font-mono text-[8px] text-slate-400">
              Secondary UAV / sensor nodes will appear here
            </div>
          </div>
        )}
      </div>
    </aside>
  );
}

export default memo(LeftSidebar);
