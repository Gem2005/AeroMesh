"use client";

import { memo, useMemo, useRef, useState } from "react";
import {
  STATUS_COLORS,
  fmtCoord,
  roleLabel,
  rssiBars,
  type BridgeCommand,
  type LatLng,
  type MeshNodeState,
  type Snapshot,
} from "@/lib/mesh";
import { Badge, HudButton, Panel, Stat } from "@/components/ui";

export type InteractionMode = "none" | "draw_polygon" | "place_zone";
type RosterFilter = "ALL" | "HW" | "VIRTUAL" | "ALERT";

const SLIDER_DEBOUNCE_MS = 250;

function RssiBars({ rssi, color }: { rssi: number | null; color: string }) {
  const bars = rssiBars(rssi);
  return (
    <span className="inline-flex h-3 items-end gap-[2px]" aria-label={`${bars} of 5 bars`}>
      {[1, 2, 3, 4, 5].map((b) => (
        <span
          key={b}
          className="w-[3px] rounded-sm"
          style={{ height: `${b * 20}%`, background: b <= bars ? color : "#1e293b" }}
        />
      ))}
    </span>
  );
}

const RosterRow = memo(function RosterRow({
  node,
  selected,
  onSelect,
}: {
  node: MeshNodeState;
  selected: boolean;
  onSelect: (id: string) => void;
}) {
  const isHw = node.type === "hardware";
  const color = STATUS_COLORS[node.status];
  return (
    <button
      type="button"
      onClick={() => onSelect(node.id)}
      className={`flex w-full items-center gap-1.5 border-l-2 px-2 py-1 text-left text-[10.5px] transition-colors ${
        selected ? "border-cyan-400 bg-cyan-500/10" : "border-transparent hover:bg-slate-800/60"
      } ${isHw ? "bg-amber-500/[0.04]" : ""}`}
    >
      <span className="h-1.5 w-1.5 shrink-0 rounded-full" style={{ background: color }} />
      <span className={`w-[72px] shrink-0 text-[9.5px] font-semibold ${isHw ? "text-amber-300" : "text-slate-500"}`}>
        [{roleLabel(node)}]
      </span>
      <span className={`min-w-0 flex-1 truncate ${isHw ? "text-amber-100" : "text-slate-200"}`} title={node.id}>
        {node.id}
      </span>
      <span className="w-6 shrink-0 rounded bg-slate-800 text-center text-[9px] text-slate-300">{node.channel}</span>
      <RssiBars rssi={node.role === "gateway" ? -40 : node.rssi} color={color} />
      <span className="w-[46px] shrink-0 text-right text-[8.5px] tracking-wide" style={{ color }}>
        {node.status}
      </span>
    </button>
  );
});

function MessageInspector({
  node,
  canSend,
  serialUp,
  send,
  onClose,
}: {
  node: MeshNodeState;
  canSend: boolean;
  serialUp: boolean;
  send: (cmd: BridgeCommand) => boolean;
  onClose: () => void;
}) {
  const [text, setText] = useState("");
  const [lastSent, setLastSent] = useState<string | null>(null);

  const submit = () => {
    const message = text.trim();
    if (!message) return;
    if (send({ type: "UPLINK_TX", target_id: node.id, message })) {
      setLastSent(message);
      setText("");
    }
  };

  return (
    <div className="border-t border-slate-800 bg-[#0d1424] p-3">
      <div className="mb-2 flex items-center justify-between">
        <div className="min-w-0">
          <div className="text-[9px] tracking-[0.2em] text-slate-500">NODE MESSAGE INSPECTOR</div>
          <div className={`truncate text-xs font-semibold ${node.type === "hardware" ? "text-amber-300" : "text-cyan-300"}`}>
            [{roleLabel(node)}] {node.id}
          </div>
        </div>
        <button type="button" onClick={onClose} className="text-[10px] text-slate-500 hover:text-white" aria-label="Close inspector">
          CLOSE
        </button>
      </div>
      <div className="mb-2 grid grid-cols-2 gap-x-3 gap-y-0.5">
        <Stat label="STATUS" value={<span style={{ color: STATUS_COLORS[node.status] }}>{node.status}</span>} />
        <Stat label="CHANNEL" value={`CH ${node.channel}`} />
        <Stat label="RSSI" value={node.rssi !== null ? `${node.rssi.toFixed(1)} dBm` : "--"} />
        <Stat label="SNR" value={node.snr !== null ? `${node.snr.toFixed(1)} dB` : "--"} />
        <Stat label="BATTERY" value={node.battery_mv !== null ? `${node.battery_mv} mV` : "USB/--"} />
        <Stat label="HOPS" value={node.reachable ? node.hop_count : "NO ROUTE"} />
      </div>
      <Stat label="POSITION" value={fmtCoord(node.coords)} />
      <div className="mt-1 truncate text-[10px] text-slate-500" title={node.hop_path.join(" → ")}>
        ROUTE: {node.hop_path.length > 1 ? node.hop_path.join(" → ") : node.role === "gateway" ? "ROOT" : "partitioned"}
      </div>
      <form
        className="mt-2 flex gap-1.5"
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
      >
        <input
          value={text}
          onChange={(e) => setText(e.target.value)}
          maxLength={120}
          placeholder="Message to node..."
          className="min-w-0 flex-1 rounded border border-slate-700 bg-slate-950 px-2 py-1.5 text-[11px] text-slate-100 outline-none placeholder:text-slate-600 focus:border-cyan-500"
        />
        <button
          type="submit"
          disabled={!canSend || !text.trim()}
          className="rounded border border-cyan-500/50 bg-cyan-500/15 px-3 text-[10px] font-semibold tracking-wider text-cyan-200 hover:bg-cyan-500/25 disabled:opacity-35"
        >
          SEND
        </button>
      </form>
      <div className="mt-1.5 text-[9.5px] leading-snug text-slate-500">
        {serialUp
          ? "UPLINK_TX traces the Dijkstra route and is mirrored to http://aeromesh.local phones via TARGET:ALL."
          : "Gateway serial offline: message animates on the digital twin only."}
        {lastSent && <div className="mt-0.5 truncate text-emerald-400">SENT: {lastSent}</div>}
      </div>
    </div>
  );
}

export default function NodeOrchestrator({
  snapshot,
  connected,
  send,
  mode,
  onModeChange,
  draftPolygon,
  onUndoVertex,
  onFinishPolygon,
  selectedId,
  onSelect,
}: {
  snapshot: Snapshot | null;
  connected: boolean;
  send: (cmd: BridgeCommand) => boolean;
  mode: InteractionMode;
  onModeChange: (m: InteractionMode) => void;
  draftPolygon: LatLng[];
  onUndoVertex: () => void;
  onFinishPolygon: () => void;
  selectedId: string | null;
  onSelect: (id: string | null) => void;
}) {
  const swarm = snapshot?.swarm;
  const [draftCount, setDraftCount] = useState<number | null>(null);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [filter, setFilter] = useState<RosterFilter>("ALL");
  const [query, setQuery] = useState("");

  const count = draftCount ?? swarm?.count ?? 40;

  const onSlider = (value: number) => {
    setDraftCount(value);
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => {
      send({ type: "SET_SWARM_SIZE", count: value });
      setDraftCount(null);
    }, SLIDER_DEBOUNCE_MS);
  };

  const nodes = useMemo(() => snapshot?.nodes ?? [], [snapshot]);
  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return nodes.filter((n) => {
      if (filter === "HW" && n.type !== "hardware") return false;
      if (filter === "VIRTUAL" && n.type !== "virtual") return false;
      if (filter === "ALERT" && n.status === "ONLINE") return false;
      return !q || n.id.toLowerCase().includes(q);
    });
  }, [nodes, filter, query]);

  const selected = selectedId ? nodes.find((n) => n.id === selectedId) ?? null : null;
  const alertCount = nodes.filter((n) => n.status !== "ONLINE").length;
  const drawing = mode === "draw_polygon";

  return (
    <aside className="flex min-h-0 flex-col border-r border-slate-800 bg-[#0b101c]">
      <Panel title="Node Orchestrator" right={<Badge tone="cyan">{swarm?.mode ?? "ORBIT_PATROL"}</Badge>} className="shrink-0">
        <div className="space-y-3 p-3">
          <div>
            <div className="mb-1 flex items-baseline justify-between text-[10px] tracking-wider text-slate-400">
              <span>VIRTUAL NODE COUNT</span>
              <span className="text-sm font-semibold tabular-nums text-cyan-300">{count}</span>
            </div>
            <input
              type="range"
              min={swarm?.min ?? 10}
              max={swarm?.max ?? 100}
              step={1}
              value={count}
              disabled={!connected}
              onChange={(e) => onSlider(Number(e.target.value))}
              className="hud-range w-full"
              aria-label="Virtual node count"
            />
            <div className="flex justify-between text-[9px] text-slate-600">
              <span>{swarm?.min ?? 10}</span>
              <span>{swarm?.max ?? 100}</span>
            </div>
          </div>

          <div>
            <div className="mb-1 text-[10px] tracking-wider text-slate-400">PLACEMENT MODE</div>
            <div className="grid grid-cols-2 gap-1.5">
              <HudButton
                active={swarm?.mode === "ORBIT_PATROL" && !drawing}
                disabled={!connected}
                onClick={() => {
                  onModeChange("none");
                  send({ type: "SET_PLACEMENT_MODE", mode: "ORBIT_PATROL" });
                }}
              >
                Orbit Patrol
              </HudButton>
              <HudButton
                active={drawing || swarm?.mode === "POLYGON_INFILL"}
                tone={drawing ? "amber" : "cyan"}
                disabled={!connected}
                onClick={() => onModeChange(drawing ? "none" : "draw_polygon")}
              >
                Draw Coverage Area
              </HudButton>
            </div>
            {drawing && (
              <div className="mt-2 rounded border border-amber-500/40 bg-amber-500/5 p-2 text-[10px] text-amber-200">
                Click the map to add vertices ({draftPolygon.length} placed, min 3).
                <div className="mt-1.5 flex gap-1.5">
                  <HudButton tone="emerald" active disabled={draftPolygon.length < 3} onClick={onFinishPolygon} className="flex-1">
                    Finish Area
                  </HudButton>
                  <HudButton disabled={draftPolygon.length === 0} onClick={onUndoVertex}>
                    Undo
                  </HudButton>
                  <HudButton onClick={() => onModeChange("none")}>Cancel</HudButton>
                </div>
              </div>
            )}
          </div>
        </div>
      </Panel>

      <Panel
        title="Node Roster"
        right={<span className="text-[10px] tabular-nums text-slate-500">{filtered.length}/{nodes.length}</span>}
        className="min-h-0 flex-1 border-t border-slate-800"
        bodyClassName="flex flex-col"
      >
        <div className="flex shrink-0 gap-1 px-2 pt-2">
          {(["ALL", "HW", "VIRTUAL", "ALERT"] as RosterFilter[]).map((f) => (
            <button
              key={f}
              type="button"
              onClick={() => setFilter(f)}
              className={`flex-1 rounded px-1 py-1 text-[9.5px] font-semibold tracking-wider ${
                filter === f ? "bg-slate-700 text-white" : "text-slate-500 hover:text-slate-300"
              }`}
            >
              {f}
              {f === "ALERT" && alertCount > 0 ? ` (${alertCount})` : ""}
            </button>
          ))}
        </div>
        <div className="shrink-0 px-2 py-1.5">
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Filter by node id..."
            className="w-full rounded border border-slate-800 bg-slate-950 px-2 py-1 text-[10.5px] text-slate-200 outline-none placeholder:text-slate-600 focus:border-slate-600"
          />
        </div>
        <div className="hud-scroll min-h-0 flex-1 overflow-y-auto pb-1">
          {filtered.map((n) => (
            <RosterRow key={n.id} node={n} selected={n.id === selectedId} onSelect={onSelect} />
          ))}
          {filtered.length === 0 && (
            <div className="px-3 py-6 text-center text-[10px] text-slate-600">
              {snapshot ? "No nodes match this filter" : "Waiting for bridge snapshot..."}
            </div>
          )}
        </div>
      </Panel>

      {selected && (
        <MessageInspector
          key={selected.id}
          node={selected}
          canSend={connected}
          serialUp={snapshot?.bridge.serial_connected ?? false}
          send={send}
          onClose={() => onSelect(null)}
        />
      )}
    </aside>
  );
}
