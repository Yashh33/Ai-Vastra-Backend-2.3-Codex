import { createClient } from "@supabase/supabase-js";
import type { AdminSession } from "./types";

const SESSION_KEY = "aivastra_admin_session";
const SUPABASE_URL = import.meta.env.VITE_SUPABASE_URL as string;
const SUPABASE_ANON_KEY = import.meta.env.VITE_SUPABASE_ANON_KEY as string;

const supabase = createClient(SUPABASE_URL, SUPABASE_ANON_KEY);

export async function createSignedUrl(
  bucket: "hero-images" | "fabric-images" | "generated-outputs" | "shop-logos",
  path: string,
  expiresInSeconds = 3600
): Promise<string> {
  const { data, error } = await supabase.storage
    .from(bucket)
    .createSignedUrl(path, expiresInSeconds);

  if (error) throw new Error(error.message);

  const signed =
    (data as { signedUrl?: string; signedURL?: string } | null)?.signedUrl ??
    (data as { signedUrl?: string; signedURL?: string } | null)?.signedURL;

  if (!signed) throw new Error("Signed URL response was empty");

  return signed;
}

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
