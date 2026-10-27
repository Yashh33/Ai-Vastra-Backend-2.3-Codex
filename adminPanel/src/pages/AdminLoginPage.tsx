import { useState, type FormEvent } from "react";

import { ADMIN_ENV } from "../lib/env";
import { useAdminAuth } from "../lib/auth";

export function AdminLoginPage() {
  const { signIn } = useAdminAuth();

  const [apiBaseUrl, setApiBaseUrl] = useState(ADMIN_ENV.apiBaseUrl);
  const [adminSecret, setAdminSecret] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");
    setBusy(true);

    try {
      await signIn({ apiBaseUrl, adminSecret });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to login");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="login-screen">
      <section className="login-card">
        <h1>MyTryonAi admin</h1>
        <p className="muted">Private control panel</p>

        <form className="stack" onSubmit={handleSubmit}>
          <label className="field">
            <span>Backend URL</span>
            <input
              className="input"
              type="text"
              value={apiBaseUrl}
              onChange={(event) => setApiBaseUrl(event.target.value)}
              placeholder="http://localhost:8000"
              disabled={busy}
            />
          </label>

          <label className="field">
            <span>Admin secret</span>
            <input
              className="input"
              type="password"
              value={adminSecret}
              onChange={(event) => setAdminSecret(event.target.value)}
              placeholder="ADMIN_PANEL_SECRET"
              disabled={busy}
            />
          </label>

          {error ? <p className="error-text">{error}</p> : null}

          <button className="btn btn-primary btn-block" type="submit" disabled={busy}>
            {busy ? "Checking…" : "Log in"}
          </button>
        </form>
      </section>
    </main>
  );
}
