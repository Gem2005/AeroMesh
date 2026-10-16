"use client";

import {
  BLEED_RATIO,
  WIFI_CHANNELS,
  channelFreqMhz,
  fmtCoord,
  type BridgeCommand,
  type Snapshot,
} from "@/lib/mesh";
import { Badge, HudButton, Panel, Stat, type Tone } from "@/components/ui";
import type { InteractionMode } from "@/components/NodeOrchestrator";

export type ZoneChannelChoice = "active" | 1 | 6 | 11;

const NOISE_MIN = -95;
const NOISE_MAX = -30;

function SpectrumChart({
  noise,
  activeChannel,
  zoneChannel,
}: {
  noise: Record<string, number>;
  activeChannel: number;
  zoneChannel: number | null;
}) {
  const W = 220;
  const H = 110;
  const padL = 24;
  const padB = 16;
  const plotW = W - padL - 2;
  const plotH = H - padB - 4;
  const slot = plotW / WIFI_CHANNELS.length;
  const toY = (v: number) => 4 + plotH - ((Math.min(NOISE_MAX, Math.max(NOISE_MIN, v)) - NOISE_MIN) / (NOISE_MAX - NOISE_MIN)) * plotH;

  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full" role="img" aria-label="2.4 GHz channel noise floor">
      {[-40, -60, -80].map((g) => (
        <g key={g}>
          <line x1={padL} x2={W - 2} y1={toY(g)} y2={toY(g)} stroke="#1e293b" strokeDasharray="2 3" />
          <text x={padL - 3} y={toY(g) + 3} textAnchor="end" fontSize="7" fill="#64748b">
            {g}
          </text>
        </g>
      ))}
      {WIFI_CHANNELS.map((ch, i) => {
        const v = noise[String(ch)] ?? -92;
        const x = padL + i * slot + slot * 0.18;
        const bw = slot * 0.64;
        const y = toY(v);
        const isActive = ch === activeChannel;
        const color = v > -60 ? "#f43f5e" : v > -80 ? "#f59e0b" : isActive ? "#22d3ee" : "#334155";
        return (
          <g key={ch}>
            <title>{`CH ${ch} (${channelFreqMhz(ch)} MHz): ${v.toFixed(1)} dBm`}</title>
            {isActive && (
              <rect x={x - 2} y={2} width={bw + 4} height={plotH + 4} fill="#22d3ee" fillOpacity={0.04} stroke="#22d3ee" strokeOpacity={0.45} strokeWidth={0.6} strokeDasharray="2 2" rx={1.5} />
            )}
            <rect x={x} y={y} width={bw} height={Math.max(1, 4 + plotH - y)} fill={color} rx={1} className="spectrum-bar" />
            <text x={x + bw / 2} y={H - 5} textAnchor="middle" fontSize="7.5" fill={isActive ? "#67e8f9" : zoneChannel === ch ? "#fb7185" : "#94a3b8"} fontWeight={isActive ? 700 : 400}>
              {ch}
            </text>
          </g>
        );
      })}
    </svg>
  );
}

const MISSION_TONES: Record<string, Tone> = {
  LOITERING: "slate",
  DISPATCHED: "amber",
  BRIDGING_ACTIVE: "violet",
};

export default function RfControlPanel({
  snapshot,
  connected,
  send,
  mode,
  onModeChange,
  zoneRadius,
  onZoneRadius,
  zoneChannel,
  onZoneChannel,
}: {
  snapshot: Snapshot | null;
  connected: boolean;
  send: (cmd: BridgeCommand) => boolean;
  mode: InteractionMode;
  onModeChange: (m: InteractionMode) => void;
  zoneRadius: number;
  onZoneRadius: (r: number) => void;
  zoneChannel: ZoneChannelChoice;
  onZoneChannel: (c: ZoneChannelChoice) => void;
}) {
  const net = snapshot?.network;
  const threat = snapshot?.threat;
  const ap = snapshot?.autopilot;
  const activeChannel = net?.channel ?? 6;
  const placing = mode === "place_zone";
  const switching = threat?.acs_state === "SWITCHING" || threat?.acs_state === "SCANNING";
  const countdown = threat?.fhss_countdown_s ?? null;
  const countdownPct = countdown !== null && threat ? (countdown / threat.fhss_confirm_s) * 100 : 0;
  const auto = threat?.eccm_auto ?? true;

  const fhss = (ch: number) => send({ type: "EXECUTE_FHSS", target_channel: ch });

  return (
    <aside className="hud-scroll flex min-h-0 flex-col overflow-y-auto border-l border-slate-800 bg-[#0b101c]">
      <Panel
        title="RF Interference Simulator"
        right={threat?.active ? <Badge tone="red" pulse>ZONE ACTIVE</Badge> : <Badge>STANDBY</Badge>}
        className="shrink-0"
      >
        <div className="space-y-2.5 p-3">
          <div className="grid grid-cols-2 gap-1.5">
            <HudButton tone="red" active={placing} disabled={!connected} onClick={() => onModeChange(placing ? "none" : "place_zone")}>
              {placing ? "Click map..." : "Place Interference Zone"}
            </HudButton>
            <HudButton disabled={!connected || !threat?.active} onClick={() => send({ type: "CLEAR_JAMMER" })}>
              Clear Zone
            </HudButton>
          </div>
          <div>
            <div className="mb-1 flex justify-between text-[10px] tracking-wider text-slate-400">
              <span>ZONE RADIUS</span>
              <span className="tabular-nums">
                <span className="text-rose-300">{zoneRadius} m</span> / <span className="text-amber-300">{Math.round(zoneRadius * BLEED_RATIO)} m</span>
              </span>
            </div>
            <input
              type="range"
              min={100}
              max={500}
              step={10}
              value={zoneRadius}
              onChange={(e) => onZoneRadius(Number(e.target.value))}
              className="hud-range hud-range-red w-full"
              aria-label="Interference zone radius"
            />
            <div className="flex justify-between text-[9px] text-slate-600">
              <span>100 m</span>
              <span>PRIMARY / FRINGE</span>
              <span>500 m</span>
            </div>
          </div>
          <div>
            <div className="mb-1 text-[10px] tracking-wider text-slate-400">TARGET CHANNEL</div>
            <div className="grid grid-cols-4 gap-1">
              {(["active", 1, 6, 11] as ZoneChannelChoice[]).map((c) => (
                <HudButton key={String(c)} active={zoneChannel === c} tone="red" onClick={() => onZoneChannel(c)} className="px-1">
                  {c === "active" ? `MESH ${activeChannel}` : `CH ${c}`}
                </HudButton>
              ))}
            </div>
          </div>
          {threat?.active && (
            <div className="rounded border border-rose-500/30 bg-rose-500/5 p-2">
              <Stat label="EPICENTER" value={fmtCoord(threat.epicenter)} />
              <Stat label="EST. CENTER" value={fmtCoord(threat.estimated_epicenter)} tone="text-rose-300" />
              <Stat label="EST. ERROR" value={threat.estimation_error_m !== null ? `${threat.estimation_error_m.toFixed(0)} m` : "--"} />
              <Stat label="CHANNEL" value={`CH ${threat.channel} (${threat.freq_mhz} MHz)`} />
              <Stat
                label="AFFECTED"
                value={
                  <>
                    <span className="text-rose-300">{threat.jammed_count} dropped</span> ·{" "}
                    <span className="text-amber-300">{threat.degraded_count} degraded</span>
                  </>
                }
              />
              <Stat label="INTERFERENCE REPORTS" value={threat.report_count} />
            </div>
          )}
        </div>
      </Panel>

      <Panel title="2.4 GHz Channel Noise Floor" right={<span className="text-[9px] text-slate-500">dBm</span>} className="shrink-0 border-t border-slate-800">
        <div className="px-2 pb-1 pt-2">
          <SpectrumChart noise={net?.spectrum_noise ?? {}} activeChannel={activeChannel} zoneChannel={threat?.channel ?? null} />
        </div>
      </Panel>

      <Panel
        title="Adaptive Channel Switching"
        right={<Badge tone={switching ? "amber" : countdown !== null ? "red" : "emerald"}>{threat?.acs_state ?? "IDLE"}</Badge>}
        className="shrink-0 border-t border-slate-800"
      >
        <div className="space-y-2.5 p-3">
          <div className="flex items-center justify-between">
            <span className="text-[11px] text-slate-300">Auto Channel Switching</span>
            <button
              type="button"
              role="switch"
              aria-checked={auto}
              disabled={!connected}
              onClick={() => send({ type: "SET_ECCM_MODE", auto: !auto })}
              className={`relative h-5 w-10 rounded-full border transition-colors disabled:opacity-40 ${
                auto ? "border-emerald-400/60 bg-emerald-500/30" : "border-slate-600 bg-slate-800"
              }`}
            >
              <span className={`absolute top-0.5 h-3.5 w-3.5 rounded-full transition-all ${auto ? "left-5 bg-emerald-300" : "left-0.5 bg-slate-400"}`} />
            </button>
          </div>
          <div>
            <div className="mb-1 flex justify-between text-[10px] text-slate-500">
              <span>{countdown !== null ? `SWITCHING TO CH ${threat?.acs_target_channel} IN` : switching ? "RETUNING MESH..." : "CONFIRMATION TIMER"}</span>
              <span className="tabular-nums text-slate-300">{countdown !== null ? `${countdown.toFixed(1)} s` : `${threat?.fhss_confirm_s ?? 3}.0 s`}</span>
            </div>
            <div className="h-1.5 overflow-hidden rounded bg-slate-800">
              <div
                className={`h-full rounded ${switching ? "animate-pulse bg-amber-400" : "bg-rose-500"}`}
                style={{ width: `${switching ? 100 : countdownPct}%`, transition: "width 1s linear" }}
              />
            </div>
          </div>
          <div className="grid grid-cols-1 gap-1.5">
            <HudButton tone="emerald" active={activeChannel === 11} disabled={!connected || switching || activeChannel === 11} onClick={() => fhss(11)}>
              Switch → CH 11 (2462 MHz)
            </HudButton>
            <HudButton tone="emerald" active={activeChannel === 1} disabled={!connected || switching || activeChannel === 1} onClick={() => fhss(1)}>
              Switch → CH 1 (2412 MHz)
            </HudButton>
            <HudButton tone="cyan" active={activeChannel === 6} disabled={!connected || switching || activeChannel === 6} onClick={() => fhss(6)}>
              Reset → CH 6 (2437 MHz)
            </HudButton>
          </div>
          <Stat
            label="LAST CONVERGENCE"
            value={net?.self_heal_convergence_ms != null ? `${(net.self_heal_convergence_ms / 1000).toFixed(2)} s` : "--"}
            tone="text-emerald-300"
          />
        </div>
      </Panel>

      <Panel
        title="MAVLink Relay Monitor"
        right={<Badge tone={MISSION_TONES[ap?.mission_state ?? "LOITERING"]} pulse={ap?.mission_state === "DISPATCHED"}>{ap?.mission_state ?? "--"}</Badge>}
        className="shrink-0 border-t border-slate-800"
      >
        <div className="space-y-1 p-3">
          <div className="mb-1.5 flex items-center justify-between rounded border border-violet-500/30 bg-violet-500/5 px-2 py-1 text-[10px]">
            <span className="text-violet-300">
              UDP {ap?.mavlink_host ?? "127.0.0.1"}:{ap?.mavlink_port ?? 14550}
            </span>
            <span className="text-slate-500">{ap?.mavlink_backend ?? "--"} · v2</span>
          </div>
          <Stat label="ASSET" value={ap?.id ?? "UAV-RELAY-01"} tone="text-violet-200" />
          <Stat label="COMMAND" value={ap?.last_command ?? "HEARTBEAT"} />
          <Stat label="RELAY POS" value={fmtCoord(ap?.coords)} />
          <Stat label="WP-ALPHA" value={fmtCoord(ap?.target_waypoint)} tone="text-sky-300" />
          <Stat label="ALTITUDE" value={`${(ap?.alt_m ?? 60).toFixed(1)} m AGL`} />
          <Stat label="GROUND SPEED" value={`${(ap?.speed_mps ?? 0).toFixed(1)} / ${(ap?.cruise_speed_mps ?? 18).toFixed(0)} m/s`} />
          <Stat label="BEARING" value={`${(ap?.bearing_deg ?? 0).toFixed(0)}°`} />
          <Stat label="ETA" value={ap?.eta_s != null && ap.mission_state === "DISPATCHED" ? `${ap.eta_s.toFixed(0)} s` : "--"} />
          <Stat label="ISOLATED / BRIDGED" value={`${ap?.isolated_count ?? 0} / ${ap?.bridged_count ?? 0}`} />
          <Stat label="WAYPOINTS / PACKETS" value={`${ap?.waypoint_count ?? 0} / ${ap?.packet_count ?? 0}`} />
        </div>
      </Panel>
    </aside>
  );
}
