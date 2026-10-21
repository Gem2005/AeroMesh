"use client";

import {
  BLEED_RATIO,
  MAST_ALT_M,
  SHEPHERD_SLANT_M,
  WIFI_CHANNELS,
  ZONE_MODES,
  ZONE_MODE_META,
  channelFreqMhz,
  fmtCoord,
  type BridgeCommand,
  type Snapshot,
  type ZoneMode,
} from "@/lib/mesh";
import { Badge, HudButton, Panel, Stat, type Tone } from "@/components/ui";
import type { InteractionMode } from "@/components/NodeOrchestrator";

export type ZoneChannelChoice = "active" | 1 | 6 | 11;

const NOISE_MIN = -95;
const NOISE_MAX = -30;

const MODE_TONE: Record<ZoneMode, Tone> = { SPOT: "red", BARRAGE: "violet", SWEEPER: "amber" };

function SpectrumChart({
  noise,
  activeChannel,
  zoneChannel,
  barrage,
}: {
  noise: Record<string, number>;
  activeChannel: number;
  zoneChannel: number | null;
  barrage: boolean;
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
        const color = v > -60 ? (barrage ? "#a855f7" : "#f43f5e") : v > -80 ? "#f59e0b" : isActive ? "#22d3ee" : "#334155";
        return (
          <g key={ch}>
            <title>{`CH ${ch} (${channelFreqMhz(ch)} MHz): ${v.toFixed(1)} dBm`}</title>
            {isActive && (
              <rect x={x - 2} y={2} width={bw + 4} height={plotH + 4} fill="#22d3ee" fillOpacity={0.04} stroke="#22d3ee" strokeOpacity={0.45} strokeWidth={0.6} strokeDasharray="2 2" rx={1.5} />
            )}
            <rect x={x} y={y} width={bw} height={Math.max(1, 4 + plotH - y)} fill={color} rx={1} className="spectrum-bar" />
            <text x={x + bw / 2} y={H - 5} textAnchor="middle" fontSize="7.5" fill={isActive ? "#67e8f9" : barrage || zoneChannel === ch ? "#fb7185" : "#94a3b8"} fontWeight={isActive ? 700 : 400}>
              {ch}
            </text>
          </g>
        );
      })}
      {barrage && (
        <text x={padL + plotW / 2} y={12} textAnchor="middle" fontSize="7.5" fill="#d8b4fe" fontWeight={700} letterSpacing="1.5">
          WIDEBAND BARRAGE CH 1-11
        </text>
      )}
    </svg>
  );
}

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
  zoneMode,
  onZoneMode,
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
  zoneMode: ZoneMode;
  onZoneMode: (m: ZoneMode) => void;
}) {
  const net = snapshot?.network;
  const threat = snapshot?.threat;
  const fleetCfg = snapshot?.fleet.config;
  const activeChannel = net?.channel ?? 6;
  const placing = mode === "place_zone";
  const switching = threat?.acs_state === "SWITCHING" || threat?.acs_state === "SCANNING";
  const countdown = threat?.fhss_countdown_s ?? null;
  const countdownPct = countdown !== null && threat ? (countdown / threat.fhss_confirm_s) * 100 : 0;
  const auto = threat?.eccm_auto ?? true;
  /** the live zone's mode wins over the operator's pending selection */
  const liveMode: ZoneMode = threat?.active ? threat.mode : zoneMode;
  const barrage = !!threat?.active && threat.barrage;
  const stranded = threat?.stranded_count ?? 0;
  const mastActive = !!threat?.mast_active;
  const gatewayInZone = !!threat?.gateway_in_zone;
  const shepherdSlant = fleetCfg?.shepherd_slant_m ?? SHEPHERD_SLANT_M;
  const mastAlt = fleetCfg?.mast_alt_m ?? MAST_ALT_M;

  const fhss = (ch: number) => send({ type: "EXECUTE_FHSS", target_channel: ch });

  return (
    <div className="hud-scroll flex min-h-0 flex-1 flex-col overflow-y-auto">
      <Panel
        title="RF Interference Simulator"
        right={
          threat?.active ? (
            <Badge tone={MODE_TONE[liveMode]} pulse>
              {ZONE_MODE_META[liveMode].short} ACTIVE
            </Badge>
          ) : (
            <Badge>STANDBY</Badge>
          )
        }
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
              <span>INTERFERENCE MODE</span>
              {threat?.active && <span className="text-slate-500">live: switches the active zone</span>}
            </div>
            <div className="grid grid-cols-3 gap-1" role="radiogroup" aria-label="Interference mode">
              {ZONE_MODES.map((m) => (
                <HudButton
                  key={m}
                  tone={MODE_TONE[m]}
                  active={liveMode === m}
                  disabled={!connected}
                  onClick={() => onZoneMode(m)}
                  className="px-1"
                  title={ZONE_MODE_META[m].description}
                >
                  {ZONE_MODE_META[m].short}
                </HudButton>
              ))}
            </div>
            <p className="mt-1 text-[9.5px] leading-snug text-slate-500">
              <span className="font-semibold" style={{ color: ZONE_MODE_META[liveMode].color }}>
                {ZONE_MODE_META[liveMode].label}
              </span>{" "}
              — {ZONE_MODE_META[liveMode].description}
            </p>
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
          <div className={liveMode === "BARRAGE" ? "opacity-40" : ""}>
            <div className="mb-1 flex justify-between text-[10px] tracking-wider text-slate-400">
              <span>TARGET CHANNEL</span>
              {liveMode === "BARRAGE" && <span className="text-slate-500">all channels</span>}
              {liveMode === "SWEEPER" && <span className="text-slate-500">start of sweep</span>}
            </div>
            <div className="grid grid-cols-4 gap-1">
              {(["active", 1, 6, 11] as ZoneChannelChoice[]).map((c) => (
                <HudButton
                  key={String(c)}
                  active={zoneChannel === c}
                  tone="red"
                  disabled={liveMode === "BARRAGE"}
                  onClick={() => onZoneChannel(c)}
                  className="px-1"
                >
                  {c === "active" ? `MESH ${activeChannel}` : `CH ${c}`}
                </HudButton>
              ))}
            </div>
          </div>
          {threat?.active && (
            <div className={`rounded border p-2 ${barrage ? "border-violet-500/30 bg-violet-500/5" : "border-rose-500/30 bg-rose-500/5"}`}>
              <Stat label="EPICENTER" value={fmtCoord(threat.epicenter)} />
              <Stat label="EST. CENTER (LKGP)" value={fmtCoord(threat.estimated_epicenter)} tone="text-rose-300" />
              <Stat
                label="LKGP TRIANGULATION ERROR"
                value={threat.estimation_error_m !== null ? `${threat.estimation_error_m.toFixed(0)} m` : "--"}
                tone={threat.estimation_error_m !== null && threat.estimation_error_m > 150 ? "text-amber-300" : "text-slate-200"}
              />
              <Stat
                label="CHANNEL"
                value={
                  barrage ? (
                    <span className="text-violet-300">CH 1-11 wideband</span>
                  ) : (
                    <>
                      CH {threat.channel} ({threat.freq_mhz} MHz)
                      {threat.sweeper_next_hop_s !== null && (
                        <span className="text-amber-300"> · hop in {threat.sweeper_next_hop_s.toFixed(0)} s</span>
                      )}
                    </>
                  )
                }
              />
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

          <div className="rounded border border-slate-800 bg-slate-900/40 p-2">
            <div className="mb-1 text-[9px] tracking-[0.2em] text-slate-500">CAUSAL RECOVERY</div>
            <Stat
              label="STRANDED ON OLD CH"
              value={
                stranded ? (
                  <span className="text-amber-300">
                    {stranded} · awaiting {threat?.shepherd_assigned ? `${threat.shepherd_assigned} shepherd` : "shepherd"} / hunt
                  </span>
                ) : (
                  <span className="text-emerald-300">0</span>
                )
              }
            />
            <Stat
              label="GATEWAY PROTECTION"
              value={
                mastActive ? (
                  <span className="text-sky-300">HIGH-MAST ACTIVE ({threat?.mast_uav_id} @ {mastAlt.toFixed(0)} m)</span>
                ) : gatewayInZone ? (
                  <span className="text-rose-300">GATEWAY {threat?.gateway_rf_state} · MAST INBOUND</span>
                ) : (
                  <span className="text-emerald-300">NOMINAL</span>
                )
              }
            />
            <p className="mt-1 text-[9px] leading-snug text-slate-600">
              Silent nodes never hear CH_SWITCH: a Shepherd UAV within {shepherdSlant.toFixed(0)} m slant retunes them, or their own
              watchdog sweeps 1/6/11 after prolonged silence.
            </p>
          </div>
        </div>
      </Panel>

      <Panel title="2.4 GHz Channel Noise Floor" right={<span className="text-[9px] text-slate-500">dBm</span>} className="shrink-0 border-t border-slate-800">
        <div className="px-2 pb-1 pt-2">
          <SpectrumChart noise={net?.spectrum_noise ?? {}} activeChannel={activeChannel} zoneChannel={threat?.channel ?? null} barrage={barrage} />
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
          {barrage && (
            <div className="rounded border border-violet-500/30 bg-violet-500/5 px-2 py-1 text-[9.5px] leading-snug text-violet-200">
              Wideband barrage: no clean channel exists, ACS holds. Recovery via HIGH-MAST proxy and flanking relays.
            </div>
          )}
          <div>
            <div className="mb-1 flex justify-between text-[10px] text-slate-500">
              <span>
                {countdown !== null
                  ? `${gatewayInZone ? "EMERGENCY EVACUATION" : "SWITCHING"} TO CH ${threat?.acs_target_channel} IN`
                  : switching
                    ? "RETUNING MESH..."
                    : "CONFIRMATION TIMER"}
              </span>
              <span className="tabular-nums text-slate-300">{countdown !== null ? `${countdown.toFixed(1)} s` : `${(threat?.fhss_confirm_s ?? 3).toFixed(1)} s`}</span>
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
            value={
              net?.self_heal_convergence_ms != null
                ? `${(net.self_heal_convergence_ms / 1000).toFixed(2)} s`
                : stranded
                  ? "recovering..."
                  : "--"
            }
            tone={stranded ? "text-amber-300" : "text-emerald-300"}
          />
        </div>
      </Panel>
    </div>
  );
}
