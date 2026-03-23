import type { AdminSession } from "./types";

const SESSION_KEY = "aivastra_admin_session";

export function normalizeApiBaseUrl(value: string) {
  return value.trim().replace(/\/+$/, "");
}

export function readSessionFromStorage(): AdminSession | null {
  const raw = window.localStorage.getItem(SESSION_KEY);
  if (!raw) return null;

  try {
    const parsed = JSON.parse(raw) as AdminSession;
    if (!parsed?.apiBaseUrl || !parsed?.adminSecret) return null;
    return {
      apiBaseUrl: normalizeApiBaseUrl(parsed.apiBaseUrl),
      adminSecret: parsed.adminSecret,
    };
  } catch {
    return null;
  }
}

export function writeSessionToStorage(session: AdminSession | null) {
  if (!session) {
    window.localStorage.removeItem(SESSION_KEY);
    return;
  }

  window.localStorage.setItem(
    SESSION_KEY,
    JSON.stringify({
      apiBaseUrl: normalizeApiBaseUrl(session.apiBaseUrl),
      adminSecret: session.adminSecret,
    })
  );
}
