"use client";

import { useSyncExternalStore } from "react";
import { DEFAULT_CHANNEL, type Snapshot } from "@/lib/mesh";
import type { WsStatus } from "@/lib/useBridge";
import { Badge } from "@/components/ui";

function subscribeClock(cb: () => void) {
  const id = setInterval(cb, 1000);
  return () => clearInterval(id);
}
const clockSnapshot = () => Math.floor(Date.now() / 1000);
const serverClockSnapshot = () => 0;

function UtcClock() {
  const epoch = useSyncExternalStore(subscribeClock, clockSnapshot, serverClockSnapshot);
  const text = epoch ? new Date(epoch * 1000).toISOString().slice(11, 19) : "--:--:--";
  return (
    <div className="flex items-baseline gap-1.5">
      <span className="text-sm font-semibold tabular-nums text-slate-100">{text}</span>
      <span className="text-[10px] tracking-widest text-slate-500">UTC</span>
    </div>
  );
}

export default function HeaderBar({
  wsStatus,
  snapshot,
  onAnchorGps,
}: {
  wsStatus: WsStatus;
  snapshot: Snapshot | null;
  onAnchorGps: () => void;
}) {
  const net = snapshot?.network;
  const channel = net?.channel ?? DEFAULT_CHANNEL;
  const freq = net?.carrier_freq_mhz ?? 2437;
  const serialUp = snapshot?.bridge.serial_connected ?? false;
  const auto = snapshot?.threat.eccm_auto ?? true;
  const hw = net?.hardware_count ?? 0;
  const virt = net?.virtual_count ?? 0;
  const acsState = snapshot?.threat.acs_state ?? "IDLE";

  const wsTone = wsStatus === "connected" ? "emerald" : wsStatus === "connecting" ? "amber" : "red";

  return (
    <header className="col-span-3 flex items-center gap-4 border-b border-slate-800 bg-[#0d1220] px-4 py-2">
      <div className="flex items-center gap-2.5">
        <svg viewBox="0 0 24 24" className="h-6 w-6 text-cyan-400" aria-hidden>
          <polygon points="12,2 21,7 21,17 12,22 3,17 3,7" fill="none" stroke="currentColor" strokeWidth="1.6" />
          <circle cx="12" cy="12" r="2.6" fill="currentColor" />
        </svg>
        <div className="leading-tight">
          <div className="text-sm font-bold tracking-[0.3em] text-slate-100">AEROMESH</div>
          <div className="text-[9px] tracking-[0.25em] text-slate-500">HIL MESH DIGITAL TWIN</div>
        </div>
      </div>

      <div className="h-8 w-px bg-slate-800" />
      <UtcClock />
      <div className="h-8 w-px bg-slate-800" />

      <div className="flex flex-wrap items-center gap-2">
        <Badge tone={wsTone} pulse={wsStatus !== "connected"}>
          WS {wsStatus.toUpperCase()}
        </Badge>
        <Badge tone={serialUp ? "emerald" : "red"}>
          SERIAL {snapshot?.bridge.serial_port ?? "COM9"} {serialUp ? `@ ${snapshot?.bridge.baud ?? 115200}` : "OFFLINE"}
        </Badge>
        <Badge tone={channel === DEFAULT_CHANNEL ? "cyan" : "emerald"}>
          CH {channel} | {freq} MHz
        </Badge>
        <Badge tone={auto ? "emerald" : "amber"} pulse={acsState !== "IDLE"}>
          ACS {auto ? "AUTO" : "MANUAL"}
          {acsState !== "IDLE" ? ` · ${acsState}` : ""}
        </Badge>
      </div>

      <div className="ml-auto flex items-center gap-4">
        {net && (
          <div className="hidden items-center gap-4 whitespace-nowrap text-[11px] 2xl:flex">
            <span className="text-slate-500">
              HEALTH <span className="tabular-nums text-slate-100">{net.health_score.toFixed(1)}%</span>
            </span>
            <span className="text-slate-500">
              PDR <span className="tabular-nums text-slate-100">{net.pdr_percent.toFixed(1)}%</span>
            </span>
            <span className="text-slate-500">
              DIAMETER <span className="tabular-nums text-slate-100">{net.graph_diameter} hops</span>
            </span>
          </div>
        )}
        <div className="whitespace-nowrap rounded border border-slate-700 bg-slate-900/70 px-3 py-1 text-[11px] font-semibold tracking-wider text-slate-200">
          <span className="text-amber-300">{hw} HW</span> + <span className="text-cyan-300">{virt} VIRTUAL</span> ={" "}
          <span className="text-white">{hw + virt} NODES</span>
        </div>
        <button
          type="button"
          onClick={onAnchorGps}
          className="whitespace-nowrap rounded border border-slate-700 px-2 py-1 text-[10px] tracking-wider text-slate-400 hover:border-cyan-500/60 hover:text-cyan-300"
          title="Re-anchor the gateway to this browser's GPS position"
        >
          ANCHOR GPS
        </button>
      </div>
    </header>
  );
}
