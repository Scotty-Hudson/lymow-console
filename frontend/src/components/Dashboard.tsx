import type { StateSnapshot } from "../types";

const RTK_LABELS: Record<number, string> = {
  0: "Not Ready",
  1: "Float Fix",
  2: "Fixed",
};

const NET_LABELS: Record<number, string> = { 0: "none", 1: "WiFi", 2: "LTE" };

function fmtDuration(s?: number | null): string {
  if (s === undefined || s === null) return "—";
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return h > 0 ? `${h}h ${m}m` : `${m}m`;
}

export default function Dashboard({ snap }: { snap: StateSnapshot | null }) {
  if (!snap) return <div className="center-note">Waiting for telemetry…</div>;
  const t = snap.telemetry;
  const err = snap.status?.error;
  const battery = Math.round(t.battery ?? 0);
  const charging = t.is_charging || t.is_recharging;

  return (
    <>
      <h2>Status</h2>
      <div className="dash-grid">
        <div className="stat">
          <div className="k">Activity</div>
          <div className="v">{snap.activity ?? "unknown"}</div>
          {snap.status?.phase && snap.status.phase !== "idle" && (
            <div className="k">{snap.status.phase}</div>
          )}
        </div>
        <div className={`stat${charging ? " charging" : ""}`}>
          <div className="k">Battery {charging ? "⚡" : ""}</div>
          <div className="v">{t.battery != null ? `${battery}%` : "—"}</div>
          <div className="battery-bar">
            <div style={{ width: `${battery}%` }} />
          </div>
        </div>
        <div className="stat">
          <div className="k">Firmware state</div>
          <div className="v">{snap.status?.work_status_label ?? "—"}</div>
        </div>
        <div className={`stat${err ? " error" : ""}`}>
          <div className="k">Errors</div>
          <div className="v">
            {err ? err.label : t.error_codes?.length ? `${t.error_codes.length}` : "none"}
          </div>
          {err?.all_codes?.length ? (
            <div className="k">codes: {err.all_codes.join(", ")}</div>
          ) : null}
        </div>
        <div className="stat">
          <div className="k">RTK</div>
          <div className="v">
            {t.rtk_status != null ? RTK_LABELS[t.rtk_status] ?? `code ${t.rtk_status}` : "—"}
          </div>
          {t.rtk_precision_m != null && (
            <div className="k">±{t.rtk_precision_m.toFixed(2)} m · {t.rtk_satellite_count ?? "?"} sats</div>
          )}
        </div>
        <div className="stat">
          <div className="k">Network</div>
          <div className="v">{t.current_net != null ? NET_LABELS[t.current_net] ?? "?" : "—"}</div>
          {t.wifi_name && <div className="k">{t.wifi_name} {t.wifi_signal_dbm != null ? `${t.wifi_signal_dbm} dBm` : ""}</div>}
          {t.current_net === 2 && t.sim_signal_dbm != null && (
            <div className="k">LTE {t.sim_signal_dbm} dBm</div>
          )}
        </div>
        <div className="stat">
          <div className="k">Current task</div>
          <div className="v">
            {t.clean_percent != null && snap.status?.task_active
              ? `${Math.round(t.clean_percent * 100)}%`
              : "—"}
          </div>
          {t.clean_area_m2 != null && <div className="k">{Math.round(t.clean_area_m2)} m² mown</div>}
        </div>
        <div className="stat">
          <div className="k">Elapsed / remaining</div>
          <div className="v">{fmtDuration(t.clean_time_s)}</div>
          {/* remainCleanTime is only shown when the firmware actually
              populated it — we never extrapolate (requirement: remaining
              time only if reliable). */}
          <div className="k">remaining {t.remain_clean_time_s ? fmtDuration(t.remain_clean_time_s) : "n/a"}</div>
        </div>
        <div className="stat">
          <div className="k">Device</div>
          <div className="v">{t.ip_address ?? "—"}</div>
          <div className="k">fw {t.fw_version ?? "?"} · {snap.device_id}</div>
        </div>
      </div>
      {snap.task_zones.length > 0 && (
        <div className="chip-row">
          {snap.task_zones.map((z) => (
            <span key={z.hash_id} className="chip">
              #{z.mow_order + 1} {z.name}
            </span>
          ))}
        </div>
      )}
      {snap.stale && (
        <div className="cmd-msg warn">
          Telemetry is stale — last update{" "}
          {snap.last_update_epoch
            ? new Date(snap.last_update_epoch * 1000).toLocaleTimeString()
            : "never"}
          .
        </div>
      )}
    </>
  );
}
