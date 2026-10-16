"use client";

/**
 * AeroMesh -- single-pane HIL mesh telemetry & simulation dashboard.
 *
 * Layout: status header / node orchestrator / geospatial map /
 * RF + relay controls / observability dock. Every value is rendered from
 * the bridge's 1 Hz SNAPSHOT; user actions are sent back as commands.
 */

import dynamic from "next/dynamic";
import { useCallback, useState } from "react";
import { BLEED_RATIO, channelFreqMhz, type LatLng } from "@/lib/mesh";
import { useBridge } from "@/lib/useBridge";
import HeaderBar from "@/components/HeaderBar";
import NodeOrchestrator, { type InteractionMode } from "@/components/NodeOrchestrator";
import RfControlPanel, { type ZoneChannelChoice } from "@/components/RfControlPanel";
import ObservabilityDock from "@/components/ObservabilityDock";

const MeshMap = dynamic(() => import("@/components/MeshMap"), {
  ssr: false,
  loading: () => (
    <div className="flex h-full w-full items-center justify-center bg-[#0a0e17] text-[11px] tracking-[0.3em] text-slate-600">
      INITIALIZING GEOSPATIAL ENGINE
    </div>
  ),
});

export default function Home() {
  const { wsStatus, snapshot, history, logs, animations, send, clearLogs, pushLog } = useBridge();

  const [mode, setMode] = useState<InteractionMode>("none");
  const [draftPolygon, setDraftPolygon] = useState<LatLng[]>([]);
  const [zoneRadius, setZoneRadius] = useState(200);
  const [zoneChannel, setZoneChannel] = useState<ZoneChannelChoice>("active");
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const connected = wsStatus === "connected";
  const activeChannel = snapshot?.network.channel ?? 6;

  const changeMode = useCallback((m: InteractionMode) => {
    setMode(m);
    setDraftPolygon([]);
  }, []);

  const onMapClick = useCallback(
    (p: LatLng) => {
      if (mode === "draw_polygon") {
        setDraftPolygon((prev) => [...prev, p]);
      } else if (mode === "place_zone") {
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
      }
    },
    [mode, zoneChannel, activeChannel, zoneRadius, send],
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
      if (mode === "none") setSelectedId(id);
    },
    [mode],
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

  return (
    <main className="grid h-screen grid-cols-[300px_minmax(0,1fr)_320px] grid-rows-[auto_minmax(0,1fr)_212px] overflow-hidden bg-[#0a0e17] font-mono text-slate-100">
      <HeaderBar wsStatus={wsStatus} snapshot={snapshot} onAnchorGps={anchorGps} />

      <NodeOrchestrator
        snapshot={snapshot}
        connected={connected}
        send={send}
        mode={mode}
        onModeChange={changeMode}
        draftPolygon={draftPolygon}
        onUndoVertex={undoVertex}
        onFinishPolygon={finishPolygon}
        selectedId={selectedId}
        onSelect={setSelectedId}
      />

      <div className="relative min-h-0 min-w-0">
        <MeshMap
          snapshot={snapshot}
          animations={animations}
          mode={mode}
          draftPolygon={draftPolygon}
          zonePreviewRadius={zoneRadius}
          selectedId={selectedId}
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

      <RfControlPanel
        snapshot={snapshot}
        connected={connected}
        send={send}
        mode={mode}
        onModeChange={changeMode}
        zoneRadius={zoneRadius}
        onZoneRadius={setZoneRadius}
        zoneChannel={zoneChannel}
        onZoneChannel={setZoneChannel}
      />

      <ObservabilityDock snapshot={snapshot} history={history} logs={logs} onClearLogs={clearLogs} />
    </main>
  );
}
