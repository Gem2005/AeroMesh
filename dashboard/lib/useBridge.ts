"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  WS_URL,
  type BridgeCommand,
  type BridgeFrame,
  type LogEventFrame,
  type LogTag,
  type Severity,
  type Snapshot,
} from "@/lib/mesh";

export type WsStatus = "connecting" | "connected" | "disconnected";

export interface MetricSample {
  tick: number;
  pdr: number;
  latency: number;
}

export interface LogEntry extends LogEventFrame {
  id: number;
}

export interface ActiveAnimation {
  id: number;
  direction: "outbound" | "inbound";
  path: string[];
  message: string;
}

const RETRY_MS = 2000;
const HISTORY_LEN = 60;
const MAX_LOGS = 250;
export const ANIMATION_MS = 2600;

/**
 * Owns the bridge WebSocket: reconnects automatically, keeps the latest
 * snapshot, a 60-sample rolling metric history, the event log and the
 * short-lived packet animations.
 */
export function useBridge() {
  const [wsStatus, setWsStatus] = useState<WsStatus>("connecting");
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [history, setHistory] = useState<MetricSample[]>([]);
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [animations, setAnimations] = useState<ActiveAnimation[]>([]);

  const wsRef = useRef<WebSocket | null>(null);
  const idRef = useRef(0);

  const pushLog = useCallback((tag: LogTag, severity: Severity, text: string) => {
    const entry: LogEntry = {
      type: "LOG_EVENT",
      tag,
      severity,
      text,
      ts: new Date().toISOString(),
      id: ++idRef.current,
    };
    setLogs((prev) => [...prev.slice(-(MAX_LOGS - 1)), entry]);
  }, []);

  useEffect(() => {
    let disposed = false;
    let wasConnected = false;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;
    const animTimers = new Set<ReturnType<typeof setTimeout>>();

    const handleFrame = (frame: BridgeFrame) => {
      switch (frame.type) {
        case "SNAPSHOT":
          setSnapshot(frame);
          setHistory((prev) => {
            const last = prev[prev.length - 1];
            if (last && last.tick === frame.tick) return prev;
            const sample = {
              tick: frame.tick,
              pdr: frame.network.pdr_percent,
              latency: frame.network.avg_latency_ms,
            };
            return [...prev.slice(-(HISTORY_LEN - 1)), sample];
          });
          break;
        case "LOG_EVENT":
          setLogs((prev) => [...prev.slice(-(MAX_LOGS - 1)), { ...frame, id: ++idRef.current }]);
          break;
        case "LOG_HISTORY":
          setLogs(frame.events.slice(-MAX_LOGS).map((e) => ({ ...e, id: ++idRef.current })));
          break;
        case "PACKET_ANIMATION": {
          const anim: ActiveAnimation = {
            id: ++idRef.current,
            direction: frame.direction,
            path: frame.path,
            message: frame.message,
          };
          setAnimations((prev) => [...prev, anim]);
          const timer = setTimeout(() => {
            animTimers.delete(timer);
            setAnimations((prev) => prev.filter((a) => a.id !== anim.id));
          }, ANIMATION_MS);
          animTimers.add(timer);
          break;
        }
        case "NODE_REPLY":
          // Replies are also streamed as LOG_EVENT [MSG]; the frame only drives the inbound animation.
          break;
      }
    };

    const connect = () => {
      if (disposed) return;
      const ws = new WebSocket(WS_URL);
      wsRef.current = ws;

      ws.onopen = () => {
        wasConnected = true;
        setWsStatus("connected");
        pushLog("SYSTEM", "ok", `Bridge link established (${WS_URL})`);
      };
      ws.onmessage = (ev) => {
        try {
          handleFrame(JSON.parse(ev.data) as BridgeFrame);
        } catch {
          /* ignore malformed frames */
        }
      };
      ws.onerror = () => ws.close();
      ws.onclose = () => {
        if (wsRef.current === ws) wsRef.current = null;
        if (disposed) return;
        if (wasConnected) pushLog("SYSTEM", "warn", "Bridge link lost; retrying");
        wasConnected = false;
        setWsStatus("disconnected");
        retryTimer = setTimeout(() => {
          setWsStatus("connecting");
          connect();
        }, RETRY_MS);
      };
    };

    connect();

    return () => {
      disposed = true;
      if (retryTimer) clearTimeout(retryTimer);
      animTimers.forEach(clearTimeout);
      wsRef.current?.close();
      wsRef.current = null;
    };
  }, [pushLog]);

  const send = useCallback((cmd: BridgeCommand): boolean => {
    const ws = wsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) return false;
    ws.send(JSON.stringify(cmd));
    return true;
  }, []);

  const clearLogs = useCallback(() => setLogs([]), []);

  return { wsStatus, snapshot, history, logs, animations, send, clearLogs, pushLog };
}
