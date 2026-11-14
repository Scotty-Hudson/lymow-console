import { FormEvent, useEffect, useState } from "react";
import { api } from "../api";

export default function Settings({ onProbe }: { onProbe: () => void }) {
  const [settings, setSettings] = useState<Record<string, unknown>>({});
  const [note, setNote] = useState<string | null>(null);

  // return-to-dock form
  const [enableRr, setEnableRr] = useState(true);
  const [rechargeBat, setRechargeBat] = useState(30);
  const [resumeBat, setResumeBat] = useState(80);

  // cloud credentials form (write-only: values never come back)
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [thingName, setThingName] = useState("");
  const [haveCloud, setHaveCloud] = useState(false);

  const load = () =>
    api
      .settings()
      .then((s) => {
        setSettings(s);
        setHaveCloud(Boolean(s.lymow_username));
        setThingName(String(s.device_thing_name ?? ""));
        setUsername(String(s.lymow_username ?? ""));
      })
      .catch(() => undefined);
  useEffect(() => {
    load();
  }, []);

  async function saveRr(ev: FormEvent) {
    ev.preventDefault();
    setNote(null);
    try {
      await api.setReturnToDock({ enableRr, rechargeBat, resumeBat });
      setNote("Return-to-dock thresholds sent to the mower.");
    } catch (e) {
      setNote((e as Error).message);
    }
  }

  async function saveCloud(ev: FormEvent) {
    ev.preventDefault();
    setNote(null);
    try {
      await api.putCloudSettings({
        lymow_username: username,
        lymow_password: password,
        device_thing_name: thingName || null,
      });
      setNote("Cloud credentials saved server-side (never sent to the browser again).");
      setPassword("");
      load();
    } catch (e) {
      setNote((e as Error).message);
    }
  }

  return (
    <>
      <fieldset className="group">
        <legend>Auto-recharge / resume thresholds</legend>
        <form className="settings-form" onSubmit={saveRr}>
          <div className="field">
            <label>
              <input
                type="checkbox"
                checked={enableRr}
                onChange={(e) => setEnableRr(e.target.checked)}
              />{" "}
              Return-to-dock during a task (recharge &amp; resume)
            </label>
          </div>
          <div className="field">
            <label>Recharge at battery ≤ (%)</label>
            <input
              type="number"
              min={1}
              max={100}
              value={rechargeBat}
              onChange={(e) => setRechargeBat(Number(e.target.value))}
            />
          </div>
          <div className="field">
            <label>Resume mowing at battery ≥ (%)</label>
            <input
              type="number"
              min={1}
              max={100}
              value={resumeBat}
              onChange={(e) => setResumeBat(Number(e.target.value))}
            />
          </div>
          <button className="primary" type="submit">
            Send to mower
          </button>
        </form>
      </fieldset>

      <fieldset className="group">
        <legend>Lymow cloud credentials (real provider only)</legend>
        <p className="help" style={{ marginTop: 0 }}>
          {haveCloud
            ? `Configured for: ${settings.lymow_username} · password ${settings.lymow_password}`
            : "Not configured. Only needed when the backend runs with LYMOW_PROVIDER=real."}
        </p>
        <form className="settings-form" onSubmit={saveCloud}>
          <div className="field">
            <label>Lymow account email</label>
            <input
              type="email"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              autoComplete="username"
            />
          </div>
          <div className="field">
            <label>Lymow account password</label>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder={haveCloud ? "(stored — type to replace)" : ""}
              autoComplete="new-password"
            />
            <span className="help">
              Stored hashed-free but server-side only, masked in every API view,
              never written to logs, never sent back to this page.
            </span>
          </div>
          <div className="field">
            <label>Device thing name (optional)</label>
            <input
              type="text"
              value={thingName}
              onChange={(e) => setThingName(e.target.value)}
              placeholder="e.g. LYMOW-ONE-PLUS-…"
            />
          </div>
          <div style={{ display: "flex", gap: 10 }}>
            <button className="primary" type="submit">
              Save credentials
            </button>
            <button
              className="danger"
              type="button"
              onClick={() =>
                api.deleteCloudSettings().then(() => {
                  setHaveCloud(false);
                  setUsername("");
                  load();
                })
              }
            >
              Remove
            </button>
          </div>
        </form>
      </fieldset>

      <fieldset className="group">
        <legend>Current backend mode</legend>
        <p className="help">
          Provider and bind address are set via environment (<code>LYMOW_PROVIDER</code>,{" "}
          <code>LYMOW_HOST</code>) and require a restart — by design the console
          binds to localhost and is only ever exposed to the LAN through an SSH
          tunnel or an authenticated reverse proxy.
        </p>
        <pre style={{ overflowX: "auto", fontSize: 12 }}>
          {JSON.stringify(
            { provider_mode: "see /healthz", stored_settings: settings },
            null,
            2,
          )}
        </pre>
      </fieldset>

      {note && (
        <div className="cmd-msg ok" onClick={onProbe}>
          {note}
        </div>
      )}
    </>
  );
}
