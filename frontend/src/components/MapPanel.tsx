import { useEffect, useMemo, useState } from "react";
import { CRS } from "leaflet";
import {
  Circle,
  CircleMarker,
  MapContainer,
  Polygon,
  Polyline,
  Tooltip,
  useMap,
} from "react-leaflet";
import { api } from "../api";
import type { MapDocument, StateSnapshot } from "../types";

function lat(p: [number, number]): [number, number] {
  // ENU meters (x, y) -> Leaflet CRS.Simple [lat=y, lng=x]
  return [p[1], p[0]];
}

// Concrete colors: Leaflet sets pathOptions as SVG presentation attributes,
// which do not resolve CSS var() — theme-independent values it is, then.
const C = {
  zone: "#3fb68b",
  zoneDisabled: "#7a8792",
  activeTask: "#2f8fd9",
  mowerHere: "#f0503c",
  dock: "#d9a13f",
  channel: "#6f7fb5",
};

function FitOnce({ doc }: { doc: MapDocument }) {
  const map = useMap();
  useEffect(() => {
    const pts = doc.zones
      .flatMap((z) => z.polygon.map(lat))
      .concat(doc.channels.flatMap((c) => c.polygon.map(lat)));
    if (doc.dock) pts.push([doc.dock.y, doc.dock.x]);
    if (pts.length) {
      map.fitBounds(pts, { padding: [18, 18] });
    }
    // Only fit when the geometry itself changes, not on every pose update.
  }, [doc.available, doc.zones.length, doc.channels.length, doc.dock?.x]);
  return null;
}

export default function MapPanel({ snap }: { snap: StateSnapshot | null }) {
  const [doc, setDoc] = useState<MapDocument | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [note, setNote] = useState<string | null>(null);

  const load = () => api.map().then(setDoc).catch((e) => setNote(e.message));
  useEffect(() => {
    if (snap?.map_ready) load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [snap?.map_ready]);

  const toggle = (hashId: string) =>
    setSelected((cur) =>
      cur.includes(hashId) ? cur.filter((h) => h !== hashId) : [...cur, hashId],
    );

  async function startSelected() {
    setNote(null);
    try {
      const res = await api.command("start", { zones: selected });
      setNote(
        res.confirmed
          ? `Started ${selected.length} zone(s).`
          : "Command sent but not confirmed by the mower (not retrying).",
      );
      setTimeout(load, 1200);
    } catch (e) {
      setNote((e as Error).message);
    }
  }

  async function refresh() {
    setNote("Querying map from mower…");
    try {
      await api.command("query-map");
    } catch {
      /* catalog may already be fresh */
    }
    setTimeout(() => {
      load();
      setNote(null);
    }, 1000);
  }

  const zoneColor = useMemo(() => {
    const m = new Map<string, { fill: string; stroke: string }>();
    for (const z of doc?.zones ?? []) {
      m.set(z.hash_id, {
        fill: z.in_current_task ? C.activeTask : z.is_enabled ? C.zone : C.zoneDisabled,
        stroke: z.hash_id === doc?.current_zone ? C.mowerHere : C.zone,
      });
    }
    return m;
  }, [doc]);

  if (!snap?.map_ready)
    return <div className="map-note">Map catalog not loaded yet.</div>;
  if (!doc || !doc.available)
    return <div className="map-note">Map unavailable — press Refresh to query the mower.</div>;

  const pose = snap.pose ?? doc.pose;

  return (
    <>
      <div className="map-frame">
        <MapContainer
          crs={CRS.Simple}
          attributionControl={false}
          zoomControl
          preferCanvas
          style={{ height: "100%", width: "100%" }}
        >
          <FitOnce doc={doc} />
          {doc.zones.map((z) => (
            <Polygon
              key={z.hash_id}
              positions={z.polygon.map(lat)}
              pathOptions={{
                color: zoneColor.get(z.hash_id)?.stroke,
                weight: z.hash_id === doc.current_zone ? 3 : 1.5,
                fillColor: zoneColor.get(z.hash_id)?.fill,
                fillOpacity: z.in_current_task ? 0.35 : z.is_enabled ? 0.15 : 0.05,
                dashArray: z.is_enabled ? undefined : "4 4",
              }}              eventHandlers={{ click: () => toggle(z.hash_id) }}
            >
              <Tooltip sticky>
                {z.name}
                {z.in_current_task ? " · in current task" : ""}
                {z.hash_id === doc.current_zone ? " · mower here" : ""}
              </Tooltip>
            </Polygon>
          ))}
          {doc.channels.map((c) => (
            <Polyline
              key={c.hash_id}
              positions={c.polygon.map(lat)}
              pathOptions={{ color: C.channel, weight: 1, dashArray: "2 4" }}
            >
              <Tooltip>{c.is_docking_channel ? "Docking channel" : "Channel"}</Tooltip>
            </Polyline>
          ))}
          {doc.dock && (
            <CircleMarker
              center={[doc.dock.y, doc.dock.x]}
              radius={7}
              pathOptions={{ color: "#d9a13f", fillColor: "#d9a13f", fillOpacity: 1 }}
            >
              <Tooltip>Dock</Tooltip>
            </CircleMarker>
          )}
          {pose && (
            <>
              {snap.telemetry.horizontal_accuracy_m != null && (
                <Circle
                  center={[pose.y, pose.x]}
                  radius={Math.max(0.5, snap.telemetry.horizontal_accuracy_m * 2)}
                  pathOptions={{
                    color: "#f0503c",
                    weight: 1,
                    fillOpacity: 0.06,
                    dashArray: "2 3",
                  }}
                />
              )}
              <CircleMarker
                center={[pose.y, pose.x]}
                radius={5}
                pathOptions={{ color: "#f0503c", fillColor: "#f0503c", fillOpacity: 1 }}
              >
                <Tooltip>Mower</Tooltip>
              </CircleMarker>
            </>
          )}
        </MapContainer>
      </div>

      <div className="map-legend">
        <span><span className="swatch" style={{ background: "var(--zone-stroke)" }} />zones (click to select)</span>
        <span><span className="swatch" style={{ background: "#d9a13f" }} />dock</span>
        <span><span className="swatch" style={{ background: "#f0503c" }} />mower position</span>
        <span><span className="swatch" style={{ background: "var(--channel-stroke)" }} />channels</span>
      </div>

      <div className="zone-pick">
        {doc.zones.map((z) => (
          <label key={z.hash_id} className={selected.includes(z.hash_id) ? "sel" : ""}>
            <input
              type="checkbox"
              style={{ width: 14 }}
              checked={selected.includes(z.hash_id)}
              onChange={() => toggle(z.hash_id)}
            />
            {z.name}
          </label>
        ))}
      </div>
      <div style={{ display: "flex", gap: 10, marginTop: 12, flexWrap: "wrap" }}>
        <button
          className="primary"
          disabled={selected.length === 0}
          onClick={startSelected}
        >
          Start {selected.length ? `${selected.length} selected zone(s)` : "zones"}
        </button>
        <button onClick={refresh}>Refresh map</button>
      </div>
      {note && <div className="cmd-msg">{note}</div>}
      {doc.unsupported.length > 0 && (
        <div className="unsupported-note">
          Not shown (protocol gap):{" "}
          {doc.unsupported.map((u) => u.feature.replace(/_/g, " ")).join(", ")}.{" "}
          {doc.unsupported[0]?.reason}
        </div>
      )}
    </>
  );
}
