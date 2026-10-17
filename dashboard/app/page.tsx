"use client";

/**
 * AeroMesh -- single-pane HIL mesh telemetry & simulation dashboard.
 *
 * Layout: status header / node orchestrator / geospatial map /
 * RF + fleet controls (tabbed) / observability dock. Every value is rendered
 * from the bridge's 1 Hz SNAPSHOT; user actions are sent back as commands.
 */

import dynamic from "next/dynamic";
import { useCallback, useState } from "react";
import { BLEED_RATIO, channelFreqMhz, isUavId, type LatLng } from "@/lib/mesh";
import { useBridge } from "@/lib/useBridge";
import HeaderBar from "@/components/HeaderBar";
import NodeOrchestrator, { type InteractionMode } from "@/components/NodeOrchestrator";
import RfControlPanel, { type ZoneChannelChoice } from "@/components/RfControlPanel";
import FleetPanel from "@/components/FleetPanel";
import ObservabilityDock from "@/components/ObservabilityDock";

const MeshMap = dynamic(() => import("@/components/MeshMap"), {
  ssr: false,
  loading: () => (
    <div className="flex h-full w-full items-center justify-center bg-[#0a0e17] text-[11px] tracking-[0.3em] text-slate-600">
      INITIALIZING GEOSPATIAL ENGINE
    </div>
  ),
});

type RightTab = "rf" | "fleet";

export default function Home() {
  const { wsStatus, snapshot, history, logs, animations, send, clearLogs, pushLog, getRecord } = useBridge();

  const [mode, setMode] = useState<InteractionMode>("none");
  const [draftPolygon, setDraftPolygon] = useState<LatLng[]>([]);
  const [zoneRadius, setZoneRadius] = useState(200);
  const [zoneChannel, setZoneChannel] = useState<ZoneChannelChoice>("active");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [selectedUavId, setSelectedUavId] = useState<string | null>(null);
  const [tab, setTab] = useState<RightTab>("fleet");

  const connected = wsStatus === "connected";
  const activeChannel = snapshot?.network.channel ?? 6;
  const fleet = snapshot?.fleet;
  const selectedUavExists = !!selectedUavId && !!fleet?.uavs.some((u) => u.id === selectedUavId);
  const effectiveMode: InteractionMode = mode === "uav_goto" && !selectedUavExists ? "none" : mode;

  const changeMode = useCallback((m: InteractionMode) => {
    setMode(m);
    setDraftPolygon([]);
  }, []);

  const selectUav = useCallback((id: string | null) => {
    setSelectedUavId(id);
    setMode((m) => (m === "uav_goto" ? "none" : m));
  }, []);

  const onMapClick = useCallback(
    (p: LatLng) => {
      if (effectiveMode === "draw_polygon") {
        setDraftPolygon((prev) => [...prev, p]);
      } else if (effectiveMode === "place_zone") {
        const channel = zoneChannel === "active" ? activeChannel : zoneChannel;
        send({
          type: "DEPLOY_JAMMER",
          lat: p[0],
          lng: p[1],
          kill_radius_m: zoneRadius,
          bleed_radius_m: Math.round(zoneRadius * BLEED_RATIO),
          freq_mhz: channelFreqMhz(channel),
          channel,
        });
        setMode("none");
      } else if (effectiveMode === "uav_goto" && selectedUavId) {
        send({ type: "UAV_COMMAND", uav_id: selectedUavId, action: "GOTO", lat: p[0], lng: p[1] });
        setMode("none");
      }
    },
    [effectiveMode, zoneChannel, activeChannel, zoneRadius, selectedUavId, send],
  );

  const finishPolygon = useCallback(() => {
    if (draftPolygon.length < 3) return;
    send({ type: "SET_DEPLOYMENT_POLYGON", vertices: draftPolygon });
    setDraftPolygon([]);
    setMode("none");
  }, [draftPolygon, send]);

  const undoVertex = useCallback(() => setDraftPolygon((prev) => prev.slice(0, -1)), []);

  const onNodeClick = useCallback(
    (id: string) => {
      if (effectiveMode !== "none") return;
      if (isUavId(id)) {
        selectUav(id);
        setTab("fleet");
      } else {
        setSelectedId(id);
      }
    },
    [effectiveMode, selectUav],
  );

  const onRosterSelect = useCallback(
    (id: string | null) => {
      if (id && isUavId(id)) {
        selectUav(id);
        setTab("fleet");
        return;
      }
      setSelectedId(id);
    },
    [selectUav],
  );

  const anchorGps = useCallback(() => {
    if (!navigator.geolocation) {
      pushLog("SYSTEM", "warn", "Geolocation API not available in this browser");
      return;
    }
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        if (send({ type: "OPERATOR_GPS", lat: pos.coords.latitude, lon: pos.coords.longitude })) {
          pushLog("SYSTEM", "ok", `Gateway anchor request sent: ${pos.coords.latitude.toFixed(5)}, ${pos.coords.longitude.toFixed(5)}`);
        }
      },
      (err) => pushLog("SYSTEM", "warn", `GPS unavailable: ${err.message}`),
      { enableHighAccuracy: true, timeout: 10000 },
    );
  }, [send, pushLog]);

  const shortfall = fleet?.allocator.shortfall ?? 0;

  return (
    <main className="grid h-screen grid-cols-[300px_minmax(0,1fr)_340px] grid-rows-[auto_minmax(0,1fr)_212px] overflow-hidden bg-[#0a0e17] font-mono text-slate-100">
      <HeaderBar wsStatus={wsStatus} snapshot={snapshot} onAnchorGps={anchorGps} />

      <NodeOrchestrator
        snapshot={snapshot}
        connected={connected}
        send={send}
        mode={effectiveMode}
        onModeChange={changeMode}
        draftPolygon={draftPolygon}
        onUndoVertex={undoVertex}
        onFinishPolygon={finishPolygon}
        selectedId={selectedId}
        onSelect={onRosterSelect}
      />

      <div className="relative min-h-0 min-w-0">
        <MeshMap
          snapshot={snapshot}
          animations={animations}
          mode={effectiveMode}
          draftPolygon={draftPolygon}
          zonePreviewRadius={zoneRadius}
          selectedId={selectedId}
          selectedUavId={selectedUavId}
          onMapClick={onMapClick}
          onNodeClick={onNodeClick}
        />
        {!connected && (
          <div className="pointer-events-none absolute inset-x-0 top-12 z-[1000] mx-auto w-fit rounded border border-rose-500/50 bg-[#0a0e17]/90 px-4 py-2 text-[11px] tracking-wider text-rose-200">
            {wsStatus === "connecting" ? "CONNECTING TO BRIDGE" : "BRIDGE OFFLINE"} · run{" "}
            <span className="text-white">python bridge.py</span>
          </div>
        )}
      </div>

      <aside className="flex min-h-0 flex-col border-l border-slate-800 bg-[#0b101c]">
        <div className="flex shrink-0 border-b border-slate-800" role="tablist">
          {(
            [
              ["fleet", `UAV FLEET${fleet ? ` (${fleet.counts.total})` : ""}`],
              ["rf", "RF / CHANNEL"],
            ] as const
          ).map(([key, label]) => (
            <button
              key={key}
              type="button"
              role="tab"
              aria-selected={tab === key}
              onClick={() => setTab(key)}
              className={`flex flex-1 items-center justify-center gap-1.5 border-b-2 px-2 py-2 text-[10px] font-semibold tracking-[0.18em] ${
                tab === key ? "border-cyan-400 text-cyan-200" : "border-transparent text-slate-500 hover:text-slate-300"
              }`}
            >
              {label}
              {key === "fleet" && shortfall > 0 && <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-rose-400" />}
              {key === "rf" && snapshot?.threat.active && <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-rose-400" />}
            </button>
          ))}
        </div>
        {tab === "fleet" ? (
          <FleetPanel
            snapshot={snapshot}
            connected={connected}
            send={send}
            mode={effectiveMode}
            onModeChange={changeMode}
            selectedUavId={selectedUavExists ? selectedUavId : null}
            onSelectUav={selectUav}
          />
        ) : (
          <RfControlPanel
            snapshot={snapshot}
            connected={connected}
            send={send}
            mode={effectiveMode}
            onModeChange={changeMode}
            zoneRadius={zoneRadius}
            onZoneRadius={setZoneRadius}
            zoneChannel={zoneChannel}
            onZoneChannel={setZoneChannel}
          />
        )}
      </aside>

      <ObservabilityDock snapshot={snapshot} history={history} logs={logs} onClearLogs={clearLogs} getRecord={getRecord} />
    </main>
  );
}
