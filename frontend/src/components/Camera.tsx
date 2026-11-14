import { useEffect, useState } from "react";
import { api } from "../api";

type CamStatus = {
  mode: string;
  available: boolean;
  live: boolean;
  reason: string | null;
};

export default function Camera() {
  const [status, setStatus] = useState<CamStatus | null>(null);
  const [enabled, setEnabled] = useState(false);
  const [imgError, setImgError] = useState(false);

  useEffect(() => {
    api.cameraStatus().then(setStatus).catch(() => setStatus(null));
  }, []);

  if (!status) return <div className="camera-off">Camera status unknown.</div>;
  if (!status.available)
    return (
      <>
        <h2>Camera</h2>
        <div className="camera-off">
          Camera relay disabled: {status.reason ?? "unavailable"}.
        </div>
      </>
    );

  return (
    <>
      <h2>Camera</h2>
      {enabled ? (
        imgError ? (
          <div className="camera-off">
            Stream closed (mower camera stopped?).{" "}
            <button className="ghost" onClick={() => setImgError(false)}>
              retry
            </button>
          </div>
        ) : (
          <img
            className="camera-frame"
            src="/api/camera/stream"
            alt="mower camera"
            onError={() => setImgError(true)}
          />
        )
      ) : (
        <div className="camera-off">
          {status.mode === "simulated"
            ? "Simulated camera feed (rendered from the map state)."
            : "Live camera relay via the backend — the browser never connects to the mower."}
          <div style={{ marginTop: 10 }}>
            <button onClick={() => setEnabled(true)}>Start feed</button>
          </div>
        </div>
      )}
    </>
  );
}
