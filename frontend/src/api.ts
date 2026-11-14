import { useEffect, useRef, useState } from "react";
import type { CommandResult, MapDocument, StateSnapshot } from "./types";

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (res.status === 401) {
    window.dispatchEvent(new Event("lymow:unauthorized"));
    throw new Error("unauthorized");
  }
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail ?? `${res.status} ${res.statusText}`);
  }
  return res.json() as Promise<T>;
}

export const api = {
  authStatus: () =>
    request<{ needs_setup: boolean; authenticated: boolean }>("/api/auth/status"),
  login: (password: string) =>
    request("/api/auth/login", { method: "POST", body: JSON.stringify({ password }) }),
  logout: () => request("/api/auth/logout", { method: "POST" }),
  state: () => request<StateSnapshot>("/api/state"),
  map: () => request<MapDocument>("/api/map"),
  history: () => request("/api/history"),
  audit: () => request("/api/audit"),
  cameraStatus: () =>
    request<{ mode: string; available: boolean; live: boolean; reason: string | null }>(
      "/api/camera/status",
    ),
  command: (path: string, body: unknown = {}): Promise<CommandResult> =>
    request(`/api/commands/${path}`, { method: "POST", body: JSON.stringify(body) }),
  setReturnToDock: (body: unknown) =>
    request("/api/config/return-to-dock", { method: "PUT", body: JSON.stringify(body) }),
  settings: () => request<Record<string, unknown>>("/api/settings"),
  putCloudSettings: (body: unknown) =>
    request("/api/settings/cloud", { method: "PUT", body: JSON.stringify(body) }),
  deleteCloudSettings: () => request("/api/settings/cloud", { method: "DELETE" }),
};

/** Live telemetry via WebSocket; reconnects with backoff. */
export function useTelemetry(authed: boolean) {
  const [snapshot, setSnapshot] = useState<StateSnapshot | null>(null);
  const [wsUp, setWsUp] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    if (!authed) return;
    let closed = false;
    let retryMs = 500;

    const connect = () => {
      if (closed) return;
      const proto = location.protocol === "https:" ? "wss" : "ws";
      const ws = new WebSocket(`${proto}://${location.host}/ws/telemetry`);
      wsRef.current = ws;
      ws.onopen = () => {
        retryMs = 500;
        setWsUp(true);
      };
      ws.onmessage = (ev) => {
        const msg = JSON.parse(ev.data);
        if (msg.type === "state") setSnapshot(msg.payload);
      };
      ws.onclose = () => {
        setWsUp(false);
        if (!closed) {
          setTimeout(connect, retryMs);
          retryMs = Math.min(retryMs * 2, 10_000);
        }
      };
    };
    connect();
    return () => {
      closed = true;
      wsRef.current?.close();
    };
  }, [authed]);

  return { snapshot, wsUp };
}
