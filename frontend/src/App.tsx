import { useCallback, useEffect, useState } from "react";
import { api, useTelemetry } from "./api";
import type { StateSnapshot } from "./types";
import Login from "./components/Login";
import Dashboard from "./components/Dashboard";
import MapPanel from "./components/MapPanel";
import Controls from "./components/Controls";
import Camera from "./components/Camera";
import History from "./components/History";
import Settings from "./components/Settings";

type Tab = "map" | "history" | "settings";

export default function App() {
  const [authed, setAuthed] = useState<boolean | null>(null); // null = probing
  const [needsSetup, setNeedsSetup] = useState(false);
  const [theme, setTheme] = useState<"dark" | "light">(
    localStorage.getItem("lymow-theme") === "light" ? "light" : "dark",
  );
  const [tab, setTab] = useState<Tab>("map");
  const { snapshot, wsUp } = useTelemetry(authed === true);
  const [poll, setPoll] = useState<StateSnapshot | null>(null);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem("lymow-theme", theme);
  }, [theme]);

  const probe = useCallback(() => {
    api
      .authStatus()
      .then((s) => {
        setNeedsSetup(s.needs_setup);
        setAuthed(s.authenticated);
      })
      .catch(() => setAuthed(false));
  }, []);

  useEffect(() => {
    probe();
    const onUnauthorized = () => setAuthed(false);
    window.addEventListener("lymow:unauthorized", onUnauthorized);
    return () => window.removeEventListener("lymow:unauthorized", onUnauthorized);
  }, [probe]);

  // Fallback poll while WS is down (e.g. proxy hiccups) so UI still moves.
  useEffect(() => {
    if (authed !== true || wsUp) return;
    const t = setInterval(() => {
      api.state().then(setPoll).catch(() => undefined);
    }, 5000);
    return () => clearInterval(t);
  }, [authed, wsUp]);

  const view = snapshot ?? poll;

  if (authed === null) return <div className="center-note">Connecting…</div>;
  if (!authed)
    return (
      <Login
        needsSetup={needsSetup}
        onLoggedIn={() => {
          setNeedsSetup(false);
          setAuthed(true);
        }}
      />
    );

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark">Lymow</span>
          <span className="brand-sub">console</span>
        </div>
        <div className="conn-badges">
          {view?.simulator && <span className="badge sim">SIMULATOR</span>}
          <span className={`badge ${view?.online ? "ok" : "warn"}`}>
            {view?.online ? "mower online" : "mower offline"}
          </span>
          <span className={`badge ${wsUp ? "ok" : "warn"}`}>
            {wsUp ? "live" : "reconnecting"}
          </span>
          {view?.stale && <span className="badge warn">telemetry stale</span>}
        </div>
        <div className="topbar-actions">
          <button
            className="ghost"
            onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
            title="Toggle theme"
          >
            {theme === "dark" ? "☀" : "☾"}
          </button>
          <button
            className="ghost"
            onClick={() =>
              api.logout().then(() => setAuthed(false)).catch(() => undefined)
            }
          >
            Log out
          </button>
        </div>
      </header>

      <main className="grid">
        <section className="panel">
          <Dashboard snap={view} />
        </section>
        <section className="panel controls-panel">
          <Controls snap={view} />
        </section>
        <section className="panel map-panel wide">
          <nav className="tabs">
            <button className={tab === "map" ? "tab active" : "tab"} onClick={() => setTab("map")}>
              Map
            </button>
            <button
              className={tab === "history" ? "tab active" : "tab"}
              onClick={() => setTab("history")}
            >
              History
            </button>
            <button
              className={tab === "settings" ? "tab active" : "tab"}
              onClick={() => setTab("settings")}
            >
              Settings
            </button>
          </nav>
          {tab === "map" && <MapPanel snap={view} />}
          {tab === "history" && <History />}
          {tab === "settings" && <Settings onProbe={probe} />}
        </section>
        <section className="panel camera-panel">
          <Camera />
        </section>
      </main>

      <footer className="footnote">
        Read-only map editor — zone geometry stays in the Lymow app. Commands are
        state-gated and audited.
      </footer>
    </div>
  );
}
