import type { AttemptStatus, AttemptType } from "./types";

export const NOT_RECORDED = "not recorded";

export const ATTEMPT_TYPE_LABELS: Record<AttemptType, string> = {
  look: "Look",
  tryon: "Try-on",
  look_tryon: "Look try-on",
};

export const STATUS_LABELS: Record<AttemptStatus, string> = {
  started: "Running",
  running: "Running",
  succeeded: "Succeeded",
  failed: "Failed",
  timed_out: "Timed out",
};

/** failed_step in plain words. */
export const FAILED_STEP_LABELS: Record<string, string> = {
  validation: "The request was rejected (missing or invalid input)",
  auth: "Not logged in, or the login expired",
  credits: "Not enough credits",
  upload: "Could not read the input images",
  prompt_build: "Could not build the prompt",
  ai_call: "The AI call failed",
  ai_no_image: "The AI answered without an image",
  ai_blocked: "The AI refused the request (safety block)",
  storage_save: "Could not save the result image",
  db_save: "Could not save to the database",
  timeout: "No result within 5 minutes",
  client_network: "The app lost its connection (reported by the app)",
  unknown: "Unknown error",
};

const IST_DATE_TIME = new Intl.DateTimeFormat("en-IN", {
  timeZone: "Asia/Kolkata",
  day: "2-digit",
  month: "short",
  year: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: true,
});

const IST_DAY = new Intl.DateTimeFormat("en-CA", {
  timeZone: "Asia/Kolkata",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
});

export function formatIst(value: string | null | undefined): string {
  if (!value) return NOT_RECORDED;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return `${IST_DATE_TIME.format(date)} IST`;
}

/** Today's date in IST as YYYY-MM-DD, shifted by `offsetDays`. */
export function istDay(offsetDays = 0): string {
  return IST_DAY.format(new Date(Date.now() + offsetDays * 86_400_000));
}

/** Start of an IST day (YYYY-MM-DD) as an ISO timestamp. */
export function istDayStartIso(day: string): string {
  return `${day}T00:00:00+05:30`;
}

/** Start of the IST day after `day`: an exclusive upper bound. */
export function istNextDayStartIso(day: string): string {
  const start = new Date(istDayStartIso(day));
  return new Date(start.getTime() + 86_400_000).toISOString();
}

export function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "—";
  if (ms < 1000) return `${ms} ms`;
  const seconds = ms / 1000;
  if (seconds < 60) return `${seconds.toFixed(1)} s`;
  const minutes = Math.floor(seconds / 60);
  const rest = Math.round(seconds - minutes * 60);
  return `${minutes} m ${String(rest).padStart(2, "0")} s`;
}

export function formatCredits(charged: number | null, refunded: number | null): string {
  const refundedCount = refunded ?? 0;
  if (refundedCount > 0) {
    return `${Math.max(0, (charged ?? 0) - refundedCount)} (refunded)`;
  }
  return String(charged ?? 0);
}

export function formatInr(value: number | string | null | undefined): string {
  if (value === null || value === undefined || value === "") return NOT_RECORDED;
  const amount = Number(value);
  return Number.isFinite(amount) ? `₹${amount.toFixed(2)}` : NOT_RECORDED;
}

export function statusPillClass(status: AttemptStatus): string {
  if (status === "succeeded") return "pill pill-ok";
  if (status === "failed") return "pill pill-bad";
  if (status === "timed_out") return "pill pill-warn";
  return "pill pill-run";
}

export function garmentLabel(name: string | null, fabricMode: string | null): string {
  const garment = name || NOT_RECORDED;
  return fabricMode ? `${garment} · ${fabricMode}` : garment;
}
