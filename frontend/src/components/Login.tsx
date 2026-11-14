import { FormEvent, useState } from "react";
import { api } from "../api";

export default function Login({
  needsSetup,
  onLoggedIn,
}: {
  needsSetup: boolean;
  onLoggedIn: () => void;
}) {
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = (ev: FormEvent) => {
    ev.preventDefault();
    setError(null);
    if (needsSetup) {
      if (password.length < 8) {
        setError("Password must be at least 8 characters.");
        return;
      }
      if (password !== confirm) {
        setError("Passwords do not match.");
        return;
      }
    }
    setBusy(true);
    api
      .login(password)
      .then(onLoggedIn)
      .catch((e) => setError(e.message === "unauthorized" ? "Invalid password." : e.message))
      .finally(() => setBusy(false));
  };

  return (
    <div className="login">
      <h1>Lymow Console</h1>
      <p className="hint">
        {needsSetup
          ? "First run: choose the console password. The server binds to localhost only."
          : "Enter the console password."}
      </p>
      <form onSubmit={submit}>
        <input
          type="password"
          autoFocus
          placeholder="Console password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />
        {needsSetup && (
          <input
            type="password"
            placeholder="Repeat password"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
          />
        )}
        {error && <div className="error">{error}</div>}
        <button className="primary" type="submit" disabled={busy}>
          {busy ? "…" : needsSetup ? "Create password" : "Log in"}
        </button>
      </form>
    </div>
  );
}
