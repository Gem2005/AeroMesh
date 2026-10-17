"use client";

import { useEffect, useMemo, useRef } from "react";
import { RSSI_BUCKETS, rssiBucket, type LogTag, type Severity, type Snapshot } from "@/lib/mesh";
import type { LogEntry, MetricSample, TelemetryRecord } from "@/lib/useBridge";
import { LineChart } from "@/components/charts";
import { Panel } from "@/components/ui";

const TAG_COLORS: Record<LogTag, string> = {
  SERIAL: "text-sky-300",
  "RF-ALERT": "text-rose-400",
  "CH-SWITCH": "text-emerald-300",
  FLEET: "text-violet-300",
  MAVLINK: "text-violet-300",
  MSG: "text-amber-300",
  SYSTEM: "text-slate-400",
};

const SEVERITY_TEXT: Record<Severity, string> = {
  info: "text-slate-300",
  ok: "text-emerald-200/90",
  warn: "text-amber-200/90",
  critical: "text-rose-200",
};

const RECORD_COLUMNS: (keyof TelemetryRecord)[] = [
  "ts",
  "tick",
  "channel",
  "pdr_percent",
  "avg_latency_ms",
  "health_score",
  "node_count",
  "reachable_count",
  "holding_count",
  "isolated_count",
  "uav_total",
  "uav_airborne",
  "uav_bridging",
  "relay_shortfall",
  "zone_active",
];

function csvCell(v: unknown): string {
  const s = String(v ?? "");
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

/** Single file, two sections: per-tick metrics, then the event log. */
function buildCsv(record: TelemetryRecord[], logs: LogEntry[]): string {
  const lines = ["# AeroMesh run record: metrics (1 Hz)", RECORD_COLUMNS.join(",")];
  for (const r of record) lines.push(RECORD_COLUMNS.map((k) => csvCell(r[k])).join(","));
  lines.push("", "# event log", "ts,tag,severity,text");
  for (const l of logs) lines.push([l.ts, l.tag, l.severity, l.text].map(csvCell).join(","));
  return lines.join("\n");
}

function downloadCsv(record: TelemetryRecord[], logs: LogEntry[]) {
  const blob = new Blob([buildCsv(record, logs)], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `aeromesh-run-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-")}.csv`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function PdrPanel({ history, current }: { history: MetricSample[]; current: number | null }) {
  const values = history.map((h) => h.pdr);
  const color = current === null ? "#64748b" : current >= 95 ? "#34d399" : current >= 70 ? "#fbbf24" : "#f43f5e";
  return (
    <Panel
      title="Packet Delivery Ratio"
      right={<span className="text-sm font-semibold tabular-nums" style={{ color }}>{current !== null ? `${current.toFixed(1)}%` : "--"}</span>}
      bodyClassName="flex flex-col px-2 pb-1"
    >
      <div className="relative min-h-0 flex-1 pt-1">
        <div className="absolute left-0 top-1 text-[8px] text-slate-600">100%</div>
        <div className="absolute bottom-0 left-0 text-[8px] text-slate-600">0%</div>
        <LineChart series={[{ values, color, fill: true }]} min={0} max={100} />
      </div>
      <div className="flex justify-between text-[8.5px] text-slate-600">
        <span>-60 s</span>
        <span>now</span>
      </div>
    </Panel>
  );
}

function LatencyPanel({ history, current, convergenceMs }: { history: MetricSample[]; current: number | null; convergenceMs: number | null }) {
  const values = history.map((h) => h.latency);
  const max = Math.max(40, ...values.map((v) => v * 1.2));
  return (
    <Panel
      title="Latency & Self-Healing"
      right={<span className="text-sm font-semibold tabular-nums text-cyan-300">{current !== null ? `${current.toFixed(1)} ms` : "--"}</span>}
      bodyClassName="flex flex-col px-2 pb-1"
    >
      <div className="relative min-h-0 flex-1 pt-1">
        <div className="absolute left-0 top-1 text-[8px] text-slate-600">{max.toFixed(0)} ms</div>
        <LineChart series={[{ values, color: "#22d3ee", fill: true }]} min={0} max={max} />
      </div>
      <div className="mt-1 rounded border border-emerald-500/30 bg-emerald-500/5 px-2 py-1 text-center text-[10px] text-emerald-200">
        Self-Healing Convergence Time:{" "}
        <span className="font-semibold tabular-nums">{convergenceMs !== null ? `${(convergenceMs / 1000).toFixed(2)} s` : "--"}</span>
      </div>
    </Panel>
  );
}

function RssiHistogram({ snapshot }: { snapshot: Snapshot | null }) {
  const counts = useMemo(() => {
    const c = { excellent: 0, good: 0, degraded: 0, disconnected: 0 };
    for (const n of snapshot?.nodes ?? []) {
      if (n.role === "gateway" || n.role === "uav") continue;
      c[rssiBucket(n.rssi, n.status)] += 1;
    }
    return c;
  }, [snapshot]);
  const total = Math.max(1, Object.values(counts).reduce((a, b) => a + b, 0));

  return (
    <Panel
      title="Next-Hop RSSI"
      right={<span className="text-[10px] tabular-nums text-slate-500">{total} nodes</span>}
      bodyClassName="flex flex-col justify-center gap-2 px-3 py-2"
    >
      {RSSI_BUCKETS.map((b) => {
        const v = counts[b.key];
        return (
          <div key={b.key}>
            <div className="mb-0.5 flex justify-between text-[9.5px]">
              <span className="text-slate-300">
                {b.label} <span className="text-slate-600">({b.range})</span>
              </span>
              <span className="tabular-nums text-slate-200">{v}</span>
            </div>
            <div className="h-2 overflow-hidden rounded-sm bg-slate-800/80">
              <div className="h-full rounded-sm" style={{ width: `${(v / total) * 100}%`, background: b.color, transition: "width 0.6s ease" }} />
            </div>
          </div>
        );
      })}
    </Panel>
  );
}

function EventLog({ logs, onClear, onExport }: { logs: LogEntry[]; onClear: () => void; onExport: () => void }) {
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const stickRef = useRef(true);

  useEffect(() => {
    const el = scrollRef.current;
    if (el && stickRef.current) el.scrollTop = el.scrollHeight;
  }, [logs]);

  return (
    <Panel
      title="Live System Event Log"
      right={
        <span className="flex gap-3">
          <button
            type="button"
            onClick={onExport}
            className="text-[9.5px] tracking-wider text-cyan-400 hover:text-cyan-200"
            title="Download metrics history (up to 1 h) and the event log as CSV"
          >
            EXPORT CSV
          </button>
          <button type="button" onClick={onClear} className="text-[9.5px] tracking-wider text-slate-500 hover:text-white">
            CLEAR
          </button>
        </span>
      }
      bodyClassName="flex flex-col"
    >
      <div
        ref={scrollRef}
        onScroll={(e) => {
          const el = e.currentTarget;
          stickRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
        }}
        className="hud-scroll min-h-0 flex-1 overflow-y-auto bg-[#060910] px-2 py-1 text-[10px] leading-[1.45]"
      >
        {logs.length === 0 && <div className="py-4 text-center text-slate-600">No events yet</div>}
        {logs.map((l) => (
          <div key={l.id} className="flex gap-1.5">
            <span className="shrink-0 tabular-nums text-slate-600">{l.ts.slice(11, 19)}</span>
            <span className={`w-[76px] shrink-0 font-semibold ${TAG_COLORS[l.tag] ?? "text-slate-400"}`}>[{l.tag}]</span>
            <span className={`min-w-0 break-words ${SEVERITY_TEXT[l.severity] ?? "text-slate-300"}`}>{l.text}</span>
          </div>
        ))}
      </div>
    </Panel>
  );
}

export default function ObservabilityDock({
  snapshot,
  history,
  logs,
  onClearLogs,
  getRecord,
}: {
  snapshot: Snapshot | null;
  history: MetricSample[];
  logs: LogEntry[];
  onClearLogs: () => void;
  getRecord: () => TelemetryRecord[];
}) {
  const net = snapshot?.network;
  return (
    <footer className="col-span-3 grid min-h-0 grid-cols-[1fr_1fr_0.9fr_1.6fr] gap-px border-t border-slate-800 bg-slate-800">
      <PdrPanel history={history} current={net?.pdr_percent ?? null} />
      <LatencyPanel history={history} current={net?.avg_latency_ms ?? null} convergenceMs={net?.self_heal_convergence_ms ?? null} />
      <RssiHistogram snapshot={snapshot} />
      <EventLog logs={logs} onClear={onClearLogs} onExport={() => downloadCsv(getRecord(), logs)} />
    </footer>
  );
}
