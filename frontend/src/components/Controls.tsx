import { useState } from "react";
import { api } from "../api";
import type { StateSnapshot } from "../types";

type CmdState = { kind: "ok" | "err" | "warn"; text: string } | null;

export default function Controls({ snap }: { snap: StateSnapshot | null }) {
  const [msg, setMsg] = useState<CmdState>(null);
  const [busy, setBusy] = useState(false);
  const [confirmAction, setConfirmAction] = useState<{
    path: string;
    title: string;
    body: string;
  } | null>(null);

  const actions = snap?.allowed_actions ?? { start_mowing: null, pause: null, dock: null };
  const taskActive = !!snap?.status?.task_active;
  const canResume =
    actions.start_mowing === 4 /* RESUME */ || actions.start_mowing === 22 /* RESUME_DOCK */;

  async function run(path: string, body: unknown = {}) {
    setBusy(true);
    setMsg(null);
    try {
      const res = await api.command(path, body);
      setMsg(
        res.confirmed
          ? { kind: "ok", text: `${res.action}: sent` }
          : {
              kind: "warn",
              text: `${res.action}: sent but NOT confirmed by the mower within 2.5s ` +
                `(firmware may have ignored it — not retrying)`,
            },
      );
    } catch (e) {
      setMsg({ kind: "err", text: (e as Error).message });
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <h2>Controls</h2>
      <div className="control-buttons">
        <button
          className="primary"
          disabled={busy || actions.start_mowing === null}
          onClick={() => run("start")}
          title={
            canResume
              ? "Resume the saved task"
              : "Start mowing all enabled zones"
          }
        >
          {canResume ? "Resume" : "Start mowing"}
        </button>
        <button
          disabled={busy || actions.pause === null}
          onClick={() => run("pause")}
          title="Pause (also clears an active error)"
        >
          {snap?.status?.error ? "Clear error / Pause" : "Pause"}
        </button>
        <button
          disabled={busy || actions.dock === null}
          onClick={() => run("dock")}
          title="Return to dock (keeps task progress where the mower supports it)"
        >
          Dock
        </button>
        <button
          className="danger"
          disabled={busy || !taskActive}
          onClick={() =>
            setConfirmAction({
              path: "dock-cancel",
              title: "Cancel task and dock?",
              body:
                "This abandons the current mowing task. Progress in the current " +
                "zone is lost and the task will not resume automatically.",
            })
          }
        >
          Cancel task + dock
        </button>
        <button
          className="danger"
          disabled={busy}
          onClick={() =>
            setConfirmAction({
              path: "force-reinit",
              title: "Force mower re-initialization?",
              body:
                "The mower abandons its task and re-initializes localization. " +
                "Use only if the mower is stuck in a bad state.",
            })
          }
        >
          Force re-init
        </button>
      </div>
      <div className={`cmd-msg ${msg?.kind ?? ""}`}>{msg?.text ?? ""}</div>

      {confirmAction && (
        <div className="modal-backdrop" onClick={() => setConfirmAction(null)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <h3>{confirmAction.title}</h3>
            <p>{confirmAction.body}</p>
            <div className="row">
              <button onClick={() => setConfirmAction(null)}>Keep running</button>
              <button
                className="danger"
                onClick={() => {
                  const a = confirmAction;
                  setConfirmAction(null);
                  run(a.path, { confirm: true });
                }}
              >
                Yes, do it
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
