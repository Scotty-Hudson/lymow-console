import { useEffect, useState } from "react";
import { api } from "../api";
import type { HistoryTask } from "../types";

interface AuditRow {
  id: number;
  ts: number;
  action: string;
  userctrl: number;
  detail: Record<string, unknown> | null;
  outcome: string;
  simulator: boolean;
}

const END_TYPE_LABELS: Record<number, string> = {
  1: "completed",
  2: "cancelled",
};

function ts(v: number | null): string {
  return v ? new Date(v * 1000).toLocaleString() : "—";
}

export default function History() {
  const [tasks, setTasks] = useState<HistoryTask[]>([]);
  const [audit, setAudit] = useState<AuditRow[]>([]);
  const [tab, setTab] = useState<"tasks" | "audit">("tasks");

  const load = () => {
    api.history().then((d) => setTasks(d as HistoryTask[])).catch(() => undefined);
    api.audit().then((d) => setAudit(d as AuditRow[])).catch(() => undefined);
  };
  useEffect(load, []);

  return (
    <>
      <div style={{ display: "flex", gap: 10, alignItems: "center", marginBottom: 10 }}>
        <button className={tab === "tasks" ? "primary" : "ghost"} onClick={() => setTab("tasks")}>
          Mow tasks
        </button>
        <button className={tab === "audit" ? "primary" : "ghost"} onClick={() => setTab("audit")}>
          Command audit
        </button>
        <button className="ghost" onClick={load} style={{ marginLeft: "auto" }}>
          Refresh
        </button>
      </div>

      {tab === "tasks" &&
        (tasks.length === 0 ? (
          <div className="map-note">No completed tasks recorded yet.</div>
        ) : (
          <table className="data">
            <thead>
              <tr>
                <th>Started</th>
                <th>End</th>
                <th>Duration</th>
                <th>Area</th>
                <th>Battery used</th>
                <th>Zones</th>
                <th>Source</th>
              </tr>
            </thead>
            <tbody>
              {tasks.map((t) => (
                <tr key={t.id}>
                  <td>{ts(t.started_at)}</td>
                  <td>{t.end_type != null ? END_TYPE_LABELS[t.end_type] ?? `type ${t.end_type}` : "in progress"}</td>
                  <td>{t.duration_s != null ? `${Math.round(t.duration_s / 60)} min` : "—"}</td>
                  <td>{t.area_m2 != null ? `${Math.round(t.area_m2)} m²` : "—"}</td>
                  <td>{t.used_battery != null ? `${t.used_battery}%` : "—"}</td>
                  <td className="wrap">{t.zones?.join(", ") ?? "—"}</td>
                  <td>{t.simulator ? "sim" : "mower"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ))}

      {tab === "audit" &&
        (audit.length === 0 ? (
          <div className="map-note">No commands issued yet.</div>
        ) : (
          <table className="data">
            <thead>
              <tr>
                <th>Time</th>
                <th>Action</th>
                <th>userCtrl</th>
                <th>Detail</th>
                <th>Outcome</th>
              </tr>
            </thead>
            <tbody>
              {audit.map((a) => (
                <tr key={a.id}>
                  <td>{ts(a.ts)}</td>
                  <td>{a.action}</td>
                  <td>{a.userctrl || "—"}</td>
                  <td className="wrap">{a.detail ? JSON.stringify(a.detail) : "—"}</td>
                  <td>{a.outcome}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ))}
    </>
  );
}
