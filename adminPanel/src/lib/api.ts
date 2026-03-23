import type { AdminSession } from "./types";

function getErrorMessage(status: number, payload: unknown): string {
  if (typeof payload === "string" && payload.trim()) return payload;

  if (payload && typeof payload === "object" && "detail" in payload) {
    const detail = (payload as { detail?: unknown }).detail;
    if (typeof detail === "string" && detail.trim()) return detail;
  }

  return `HTTP ${status}`;
}

export async function adminFetch<T>(
  session: AdminSession,
  path: string,
  init: RequestInit = {}
): Promise<T> {
  const headers = new Headers(init.headers ?? {});
  headers.set("x-admin-secret", session.adminSecret);

  if (init.body && !(init.body instanceof FormData) && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }

  const response = await fetch(`${session.apiBaseUrl}${path}`, {
    ...init,
    headers,
  });

  const text = await response.text();
  let payload: unknown = null;

  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = text;
    }
  }

  if (!response.ok) {
    throw new Error(getErrorMessage(response.status, payload));
  }

  return payload as T;
}

export async function verifyAdminSession(session: AdminSession): Promise<void> {
  await adminFetch<{ ok: boolean }>(session, "/admin/session", { method: "GET" });
}
