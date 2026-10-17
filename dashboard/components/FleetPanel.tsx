"use client";

import { memo, useState } from "react";
import {
  FLIGHT_MODE_COLORS,
  FLIGHT_STATE_LABEL,
  RSSI_BUCKETS,
  STATUS_COLORS,
  batteryColor,
  fmtCoord,
  fmtDuration,
  uavTypeColor,
  type BridgeCommand,
  type FleetAction,
  type FleetState,
  type NetworkState,
  type Snapshot,
  type UavAction,
  type UavState,
} from "@/lib/mesh";
import { Badge, HudButton, Panel, Stat } from "@/components/ui";
import type { InteractionMode } from "@/components/NodeOrchestrator";

const MAX_ADD_BATCH = 10;

function BatteryBar({ pct, cfg }: { pct: number; cfg?: FleetState["config"] }) {
  return (
    <span className="inline-flex h-1.5 w-10 overflow-hidden rounded-sm bg-slate-800" aria-label={`battery ${pct.toFixed(0)}%`}>
      <span className="h-full" style={{ width: `${Math.max(2, Math.min(100, pct))}%`, background: batteryColor(pct, cfg) }} />
    </span>
  );
}

function ModeBadge({ uav }: { uav: UavState }) {
  const color = FLIGHT_MODE_COLORS[uav.mode];
  return (
    <span
      className="w-[46px] shrink-0 rounded border px-1 text-center text-[8.5px] font-semibold tracking-wider"
      style={{ color, borderColor: `${color}66`, background: `${color}14` }}
    >
      {uav.mode}
    </span>
  );
}

function TypeBadge({ type, className = "" }: { type: string; className?: string }) {
  const color = uavTypeColor(type);
  return (
    <span
      className={`shrink-0 rounded px-1 text-center text-[8px] font-semibold tracking-wide ${className}`}
      style={{ color, background: `${color}1a`, border: `1px solid ${color}55` }}
    >
      {type}
    </span>
  );
}

/** Stacked bar of ground nodes by next-hop RSSI band. */
function SignalBandsBar({ bands }: { bands: NetworkState["signal_bands"] }) {
  const total = RSSI_BUCKETS.reduce((s, b) => s + (bands[b.key] ?? 0), 0);
  return (
    <div>
      <div className="flex h-2 overflow-hidden rounded-sm bg-slate-800">
        {total > 0 &&
          RSSI_BUCKETS.map((b) =>
            bands[b.key] ? (
              <span key={b.key} style={{ width: `${(100 * bands[b.key]) / total}%`, background: b.color }} title={`${b.label}: ${bands[b.key]}`} />
            ) : null,
          )}
      </div>
      <div className="mt-0.5 flex justify-between text-[8.5px] tabular-nums">
        {RSSI_BUCKETS.map((b) => (
          <span key={b.key} style={{ color: b.color }}>
            {b.label.slice(0, 4).toUpperCase()} {bands[b.key] ?? 0}
          </span>
        ))}
      </div>
    </div>
  );
}

const UavRow = memo(function UavRow({
  uav,
  cfg,
  selected,
  onSelect,
}: {
  uav: UavState;
  cfg: FleetState["config"];
  selected: boolean;
  onSelect: (id: string) => void;
}) {
  const alert = uav.failsafe !== null || uav.battery_pct <= cfg.relief_battery_pct;
  return (
    <button
      type="button"
      onClick={() => onSelect(uav.id)}
      className={`flex w-full items-center gap-1.5 border-l-2 px-2 py-1 text-left text-[10px] transition-colors ${
        selected ? "border-violet-400 bg-violet-500/10" : "border-transparent hover:bg-slate-800/60"
      }`}
    >
      <span className="w-[44px] shrink-0 font-semibold text-violet-200">{uav.id}</span>
      <TypeBadge type={uav.type} className="w-[46px]" />
      <ModeBadge uav={uav} />
      <span className={`min-w-0 flex-1 truncate text-[9px] ${alert ? "text-amber-300" : "text-slate-400"}`}>
        {uav.failsafe ?? (uav.slot_label ? `${FLIGHT_STATE_LABEL[uav.flight_state]} ${uav.slot_label}` : FLIGHT_STATE_LABEL[uav.flight_state])}
      </span>
      <BatteryBar pct={uav.battery_pct} cfg={cfg} />
      <span className="w-[30px] shrink-0 text-right tabular-nums text-slate-300">{uav.alt_m.toFixed(0)}m</span>
      <span className="w-[16px] shrink-0 text-right tabular-nums text-emerald-300" title="ground nodes routed through this UAV">
        {uav.bridged_count || ""}
      </span>
    </button>
  );
});

function UavControls({
  uav,
  fleet,
  connected,
  send,
  mode,
  onModeChange,
}: {
  uav: UavState;
  fleet: FleetState;
  connected: boolean;
  send: (cmd: BridgeCommand) => boolean;
  mode: InteractionMode;
  onModeChange: (m: InteractionMode) => void;
}) {
  const cfg = fleet.config;
  const [alt, setAlt] = useState(uav.cruise_alt_m);
  const [speed, setSpeed] = useState(uav.cruise_speed_mps);
  const airborne = uav.flight_state !== "LANDED";
  const going = mode === "uav_goto";
  const cmd = (action: UavAction, extra: { alt_m?: number; speed_mps?: number } = {}) =>
    send({ type: "UAV_COMMAND", uav_id: uav.id, action, ...extra });
  const dirty = alt !== uav.cruise_alt_m || speed !== uav.cruise_speed_mps;

  const apply = () => {
    if (alt !== uav.cruise_alt_m) cmd("SET_ALT", { alt_m: alt });
    if (speed !== uav.cruise_speed_mps) cmd("SET_SPEED", { speed_mps: speed });
  };

  return (
    <div className="space-y-2.5 p-3">
      <div className="flex items-center justify-between">
        <div>
          <div className="flex items-center gap-1.5 text-xs font-semibold text-violet-200">
            {uav.id} <TypeBadge type={uav.type} />
            <span className="text-[10px] font-normal text-slate-500">sysid {uav.sysid ?? "n/a"}</span>
          </div>
          <div className="text-[9.5px] text-slate-500">
            {uav.armed ? "ARMED" : "DISARMED"} · {FLIGHT_STATE_LABEL[uav.flight_state]}
            {uav.task ? ` · ${uav.task}${uav.slot_label ? ` ${uav.slot_label}` : ""}` : ""}
          </div>
        </div>
        <ModeBadge uav={uav} />
      </div>
      <div className="rounded border border-slate-800 bg-slate-900/40 px-2 py-1 text-[9.5px] leading-snug text-slate-400">
        {uav.type_name}: {uav.endurance_min} min rated, {uav.speed_min_mps}-{uav.speed_max_mps} m/s,{" "}
        {uav.loiter_radius_m > 0 ? `loiters r=${uav.loiter_radius_m} m on station` : "hovers on station"}
        {uav.cover_radius_m !== null && ` · good-link footprint ${uav.cover_radius_m.toFixed(0)} m`}
      </div>

      {uav.failsafe && (
        <div className="rounded border border-rose-500/40 bg-rose-500/10 px-2 py-1 text-[10px] text-rose-200">
          FAILSAFE {uav.failsafe.replace("_", " ")}
          {uav.failsafe === "CRITICAL_BATTERY" ? ": landing cannot be overridden" : ": operator command clears it"}
        </div>
      )}
      {uav.remove_on_land && (
        <div className="rounded border border-amber-500/40 bg-amber-500/5 px-2 py-1 text-[10px] text-amber-200">
          Decommissioning: returns to pad, then leaves the fleet
        </div>
      )}

      <div className="grid grid-cols-2 gap-x-3 gap-y-0.5">
        <Stat label="BATTERY" value={<span style={{ color: batteryColor(uav.battery_pct, cfg) }}>{uav.battery_pct.toFixed(0)}%</span>} />
        <Stat label="USABLE" value={fmtDuration(uav.usable_endurance_s)} />
        <Stat label="ALT" value={`${uav.alt_m.toFixed(0)}/${uav.cruise_alt_m.toFixed(0)} m`} />
        <Stat label="CLIMB" value={`${uav.climb_mps.toFixed(1)} m/s`} />
        <Stat label="SPEED" value={`${uav.speed_mps.toFixed(1)}/${uav.cruise_speed_mps.toFixed(0)}`} />
        <Stat label="HDG" value={`${uav.bearing_deg.toFixed(0)}°`} />
        <Stat label="ETA" value={uav.eta_s !== null ? fmtDuration(uav.eta_s) : "--"} />
        <Stat label="HOME" value={`${uav.dist_home_m.toFixed(0)} m`} />
        <Stat
          label="LINK"
          value={
            uav.in_mesh ? (
              <span style={{ color: uav.status === "STANDBY" ? undefined : STATUS_COLORS[uav.status] }}>
                {uav.reachable ? `${uav.hop_count} hop${uav.hop_count === 1 ? "" : "s"}` : "NO C2"}
              </span>
            ) : (
              "STANDBY"
            )
          }
        />
        <Stat label="NEXT HOP" value={uav.link_rssi !== null ? `${uav.link_rssi.toFixed(0)} dBm` : "--"} />
        <Stat label="SERVING" value={<span className="text-emerald-300">{uav.bridged_count}</span>} />
        <Stat label="FLIGHT" value={fmtDuration(uav.mission_time_s)} />
      </div>
      {uav.c2_lost_s !== null && (
        <div className="text-[10px] text-amber-300">
          C2 lost {uav.c2_lost_s.toFixed(0)} s (RTL at {cfg.lost_link_s} s)
        </div>
      )}
      {(uav.relieving || uav.relief_by) && (
        <div className="text-[10px] text-sky-300">
          {uav.relieving ? `Relieving ${uav.relieving}` : `Relief inbound: ${uav.relief_by}`}
        </div>
      )}
      <Stat label="TARGET" value={fmtCoord(uav.target)} tone="text-sky-300" />
      <Stat label="LAST CMD" value={<span className="block max-w-[170px] truncate">{uav.last_command ?? "--"}</span>} />

      <div className="grid grid-cols-3 gap-1">
        <HudButton tone="emerald" disabled={!connected || airborne} onClick={() => cmd("TAKEOFF")}>
          Takeoff
        </HudButton>
        <HudButton disabled={!connected || !airborne} onClick={() => cmd("HOLD")}>
          Hold
        </HudButton>
        <HudButton tone="violet" active={uav.mode === "AUTO"} disabled={!connected} onClick={() => cmd("AUTO")}>
          Auto
        </HudButton>
        <HudButton tone="amber" disabled={!connected || !airborne} onClick={() => cmd("RTL")}>
          RTL
        </HudButton>
        <HudButton tone="amber" disabled={!connected || !airborne} onClick={() => cmd("LAND")}>
          Land
        </HudButton>
        <HudButton tone="cyan" active={going} disabled={!connected} onClick={() => onModeChange(going ? "none" : "uav_goto")}>
          {going ? "Click map" : "Go To"}
        </HudButton>
      </div>
      {going && (
        <div className="rounded border border-sky-500/40 bg-sky-500/5 px-2 py-1 text-[10px] text-sky-200">
          Click inside the {fleet.geofence.radius_m} m geofence to fly {uav.id} there in GUIDED mode.
        </div>
      )}

      <div>
        <div className="mb-0.5 flex justify-between text-[10px] text-slate-400">
          <span>CRUISE ALT</span>
          <span className="tabular-nums text-slate-200">{alt.toFixed(0)} m</span>
        </div>
        <input
          type="range"
          min={cfg.alt_min_m}
          max={cfg.alt_max_m}
          step={5}
          value={alt}
          onChange={(e) => setAlt(Number(e.target.value))}
          className="hud-range w-full"
          aria-label="Cruise altitude"
        />
        <div className="mb-0.5 mt-1 flex justify-between text-[10px] text-slate-400">
          <span>CRUISE SPEED</span>
          <span className="tabular-nums text-slate-200">{speed.toFixed(0)} m/s</span>
        </div>
        <input
          type="range"
          min={uav.speed_min_mps}
          max={uav.speed_max_mps}
          step={1}
          value={speed}
          onChange={(e) => setSpeed(Number(e.target.value))}
          className="hud-range w-full"
          aria-label="Cruise speed"
        />
        <div className="mt-1.5 grid grid-cols-3 gap-1">
          <HudButton tone="emerald" active={dirty} disabled={!connected || !dirty} onClick={apply}>
            Apply
          </HudButton>
          <HudButton disabled={!connected || airborne} onClick={() => cmd(uav.armed ? "DISARM" : "ARM")}>
            {uav.armed ? "Disarm" : "Arm"}
          </HudButton>
          <HudButton
            tone="red"
            active
            disabled={!connected || uav.remove_on_land}
            onClick={() => send({ type: "FLEET_REMOVE", uav_id: uav.id })}
            title={airborne ? "Returns to pad, then leaves the fleet" : "Remove from fleet"}
          >
            Remove
          </HudButton>
        </div>
      </div>
    </div>
  );
}

function GcsCard({ fleet }: { fleet: FleetState }) {
  const g = fleet.gcs;
  const firstSysid = fleet.uavs.find((u) => u.sysid !== null)?.sysid;
  return (
    <div className="space-y-1 p-3">
      <div className="mb-1.5 flex items-center justify-between rounded border border-violet-500/30 bg-violet-500/5 px-2 py-1 text-[10px]">
        <span className="text-violet-300">
          UDP {g.host}:{g.port}
        </span>
        <span className="text-slate-500">MAVLink v2 · rx :{g.local_port}</span>
      </div>
      <Stat label="GROUND STATION" value={g.connected ? `${g.gcs_addr} (sysid ${g.gcs_sysid})` : "not connected"} tone={g.connected ? "text-emerald-300" : "text-slate-500"} />
      <Stat label="HEARTBEAT AGE" value={g.heartbeat_age_s !== null ? `${g.heartbeat_age_s.toFixed(1)} s` : "--"} />
      <Stat label="PACKETS TX / RX" value={`${g.packet_count} / ${g.rx_count}`} />
      <Stat label="COMMANDS / ERRORS" value={`${g.cmd_count} / ${g.rx_errors}`} />
      <Stat
        label="LAST GCS CMD"
        value={<span className="block max-w-[160px] truncate">{g.last_command ? `${g.last_command} (${g.last_command_age_s?.toFixed(0)} s)` : "--"}</span>}
      />
      {!g.connected && (
        <div className="pt-1 text-[9.5px] leading-snug text-slate-500">
          Open QGroundControl or Mission Planner on UDP {g.port}: every relay UAV appears as its own vehicle
          {firstSysid ? ` (sysid ${firstSysid}+)` : ""} and accepts arm, takeoff, mode, fly-to, RTL and land.
        </div>
      )}
    </div>
  );
}

export default function FleetPanel({
  snapshot,
  connected,
  send,
  mode,
  onModeChange,
  selectedUavId,
  onSelectUav,
}: {
  snapshot: Snapshot | null;
  connected: boolean;
  send: (cmd: BridgeCommand) => boolean;
  mode: InteractionMode;
  onModeChange: (m: InteractionMode) => void;
  selectedUavId: string | null;
  onSelectUav: (id: string | null) => void;
}) {
  const fleet = snapshot?.fleet;
  const [addCount, setAddCount] = useState(1);
  const [addType, setAddType] = useState<string | null>(null);
  if (!fleet || !snapshot) {
    return <div className="p-6 text-center text-[10px] text-slate-600">Waiting for bridge snapshot...</div>;
  }
  const { counts, allocator, config } = fleet;
  const coverage = allocator.coverage;
  const coverBacklog = coverage.areas.length - coverage.assigned;
  const selected = selectedUavId ? fleet.uavs.find((u) => u.id === selectedUavId) ?? null : null;
  const fleetCmd = (action: FleetAction) => send({ type: "FLEET_COMMAND", action });
  const typeKey = addType ?? config.default_type;
  const typeInfo = config.types.find((t) => t.key === typeKey) ?? config.types[0];

  return (
    <div className="hud-scroll flex min-h-0 flex-1 flex-col overflow-y-auto">
      <Panel
        title="Relay Fleet"
        right={
          allocator.shortfall > 0 ? (
            <Badge tone="red" pulse>
              SHORT {allocator.shortfall}
            </Badge>
          ) : (
            <Badge tone={counts.bridging > 0 ? "violet" : "slate"}>
              {counts.airborne}/{counts.total} AIRBORNE
            </Badge>
          )
        }
        className="shrink-0"
      >
        <div className="space-y-2.5 p-3">
          <div className="grid grid-cols-3 gap-1 text-center">
            {(
              [
                ["TOTAL", counts.total, "text-slate-100"],
                ["AIRBORNE", counts.airborne, "text-sky-300"],
                ["BRIDGING", counts.bridging, "text-violet-300"],
                ["COVERING", counts.covering, "text-cyan-300"],
                ["LOW BATT", counts.low_battery, counts.low_battery ? "text-amber-300" : "text-slate-500"],
                ["FAILSAFE", counts.failsafe, counts.failsafe ? "text-rose-300" : "text-slate-500"],
              ] as const
            ).map(([label, value, tone]) => (
              <div key={label} className="rounded border border-slate-800 bg-slate-900/50 py-1">
                <div className={`text-sm font-semibold tabular-nums ${tone}`}>{value}</div>
                <div className="text-[8.5px] tracking-wider text-slate-500">{label}</div>
              </div>
            ))}
          </div>

          <div
            className={`rounded border p-2 text-[10px] ${
              allocator.suspended
                ? "border-rose-500/40 bg-rose-500/5"
                : allocator.shortfall > 0
                  ? "border-amber-500/40 bg-amber-500/5"
                  : "border-slate-800 bg-slate-900/40"
            }`}
          >
            <div className="mb-1 text-[9px] tracking-[0.2em] text-slate-500">RELAY ALLOCATOR</div>
            <Stat label="ISOLATED NODES" value={allocator.isolated} tone={allocator.isolated ? "text-rose-300" : undefined} />
            <Stat label="CLUSTERS" value={allocator.clusters.length} />
            <Stat label="SLOTS ASSIGNED" value={`${allocator.assigned} / ${allocator.needed}`} />
            {allocator.suspended && <div className="mt-1 text-rose-200">Gateway jammed: relays cannot restore C2. Switch channel.</div>}
            {allocator.shortfall > 0 && (
              <div className="mt-1.5 flex items-center justify-between gap-2 text-amber-200">
                <span>
                  Need {allocator.shortfall} more relay UAV{allocator.shortfall === 1 ? "" : "s"}
                </span>
                <HudButton
                  tone="amber"
                  active
                  disabled={!connected}
                  onClick={() => send({ type: "FLEET_ADD", count: Math.min(MAX_ADD_BATCH, allocator.shortfall) })}
                >
                  Add {Math.min(MAX_ADD_BATCH, allocator.shortfall)}
                </HudButton>
              </div>
            )}
          </div>

          <div className="rounded border border-slate-800 bg-slate-900/40 p-2 text-[10px]">
            <div className="mb-1 flex items-center justify-between">
              <span className="text-[9px] tracking-[0.2em] text-slate-500">COVERAGE · NEXT HOP &lt; {coverage.threshold_dbm} dBm</span>
              <HudButton
                tone="cyan"
                active={coverage.enabled}
                disabled={!connected}
                onClick={() => send({ type: "SET_COVERAGE", enabled: !coverage.enabled })}
                className="px-1.5 py-0"
                title="Fly idle UAVs over areas whose ground next hop is degraded"
              >
                {coverage.enabled ? "Auto on" : "Auto off"}
              </HudButton>
            </div>
            <SignalBandsBar bands={snapshot.network.signal_bands} />
            <div className="mt-1.5">
              <Stat label="WEAK NEXT HOP (NO UAV)" value={coverage.weak_nodes} tone={coverage.weak_nodes ? "text-amber-300" : undefined} />
              <Stat label="AREAS COVERED" value={`${coverage.assigned} / ${coverage.areas.length}`} />
              <Stat label="NODES VIA UAV" value={snapshot.network.uav_served_count} tone="text-cyan-300" />
            </div>
            {coverage.enabled && coverBacklog > 0 && (
              <div className="mt-1 text-[9.5px] text-slate-400">
                {coverBacklog} weak area{coverBacklog === 1 ? "" : "s"} waiting: add UAVs or let relays recharge.
              </div>
            )}
          </div>

          <div className="flex flex-wrap gap-1" role="radiogroup" aria-label="UAV type to add">
            {config.types.map((t) => {
              const on = t.key === typeKey;
              const color = uavTypeColor(t.key);
              return (
                <button
                  key={t.key}
                  type="button"
                  role="radio"
                  aria-checked={on}
                  onClick={() => setAddType(t.key)}
                  className="flex-1 rounded border px-1 py-0.5 text-[9px] font-semibold tracking-wide transition-colors"
                  style={on ? { color, borderColor: color, background: `${color}1f` } : { color: "#64748b", borderColor: "#334155" }}
                  title={t.name}
                >
                  {t.key} <span className="font-normal">{counts.by_type[t.key] ?? 0}</span>
                </button>
              );
            })}
          </div>
          {typeInfo && (
            <div className="-mt-1 text-[9px] text-slate-500">
              {typeInfo.name}: {typeInfo.endurance_min} min, cruise {typeInfo.cruise_speed_mps} m/s
              {typeInfo.loiter_radius_m > 0 ? `, loiters r=${typeInfo.loiter_radius_m} m` : ", hovers"} · same relay radio
            </div>
          )}

          <div className="flex items-center gap-1.5">
            <div className="flex items-center rounded border border-slate-700">
              <button
                type="button"
                className="px-2 py-1 text-[11px] text-slate-400 hover:text-white disabled:opacity-30"
                disabled={addCount <= 1}
                onClick={() => setAddCount((c) => Math.max(1, c - 1))}
                aria-label="Fewer"
              >
                −
              </button>
              <span className="w-6 text-center text-[11px] tabular-nums text-slate-100">{addCount}</span>
              <button
                type="button"
                className="px-2 py-1 text-[11px] text-slate-400 hover:text-white disabled:opacity-30"
                disabled={addCount >= MAX_ADD_BATCH}
                onClick={() => setAddCount((c) => Math.min(MAX_ADD_BATCH, c + 1))}
                aria-label="More"
              >
                +
              </button>
            </div>
            <HudButton
              tone="violet"
              active
              disabled={!connected}
              onClick={() => send({ type: "FLEET_ADD", count: addCount, uav_type: typeKey })}
              className="flex-1"
            >
              Add {addCount} {typeKey}
            </HudButton>
          </div>
          {counts.total >= config.soft_limit && (
            <div className="text-[9.5px] text-amber-300">
              {counts.total} UAVs: above {config.soft_limit} the map and GCS get crowded; no hard cap.
            </div>
          )}

          <div className="grid grid-cols-4 gap-1">
            <HudButton tone="emerald" disabled={!connected || counts.landed === 0} onClick={() => fleetCmd("LAUNCH_ALL")} className="px-1">
              Launch
            </HudButton>
            <HudButton tone="violet" disabled={!connected || counts.total === 0} onClick={() => fleetCmd("AUTO_ALL")} className="px-1">
              Auto
            </HudButton>
            <HudButton tone="amber" disabled={!connected || counts.airborne === 0} onClick={() => fleetCmd("RTL_ALL")} className="px-1">
              RTL
            </HudButton>
            <HudButton tone="amber" disabled={!connected || counts.airborne === 0} onClick={() => fleetCmd("LAND_ALL")} className="px-1">
              Land
            </HudButton>
          </div>
          <div className="text-[9px] text-slate-600">
            ALL-FLEET · relief / handoff at {config.relief_battery_pct}% · RTL at {config.low_battery_pct}%
          </div>
        </div>
      </Panel>

      <Panel
        title="Vehicles"
        right={<span className="text-[10px] tabular-nums text-slate-500">{fleet.uavs.length}</span>}
        className="shrink-0 border-t border-slate-800"
      >
        <div className="hud-scroll max-h-[230px] overflow-y-auto py-1">
          {fleet.uavs.map((u) => (
            <UavRow key={u.id} uav={u} cfg={config} selected={u.id === selectedUavId} onSelect={onSelectUav} />
          ))}
          {fleet.uavs.length === 0 && <div className="px-3 py-4 text-center text-[10px] text-slate-600">No UAVs: add some above</div>}
        </div>
      </Panel>

      {selected && (
        <Panel
          title="Vehicle Control"
          right={
            <button type="button" onClick={() => onSelectUav(null)} className="text-[10px] text-slate-500 hover:text-white">
              CLOSE
            </button>
          }
          className="shrink-0 border-t border-slate-800"
        >
          <UavControls
            key={`${selected.id}:${selected.cruise_alt_m}:${selected.cruise_speed_mps}`}
            uav={selected}
            fleet={fleet}
            connected={connected}
            send={send}
            mode={mode}
            onModeChange={onModeChange}
          />
        </Panel>
      )}

      <Panel
        title="GCS Link"
        right={<Badge tone={fleet.gcs.connected ? "emerald" : "slate"}>{fleet.gcs.connected ? "QGC LINKED" : "NO GCS"}</Badge>}
        className="shrink-0 border-t border-slate-800"
      >
        <GcsCard fleet={fleet} />
      </Panel>
    </div>
  );
}
