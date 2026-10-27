/**
 * Live preview of a library garment's prompts. Mirrors prompt_library.py in the backend, which
 * does the real filling on save: plain one-pass replacement of the set's blanks, nothing else.
 */
import type { AdminFabricSlotRow, GarmentOverviewRow, PromptSetKey } from "./types";

export const SET_LABELS: Record<PromptSetKey, string> = {
  single: "Single fabric",
  two: "Two fabrics",
};

export const SET_BLANKS: Record<PromptSetKey, string[]> = {
  single: ["GARMENT"],
  two: ["DESIGN", "KEEP", "PART1_LABEL", "PART1", "PART2_LABEL", "PART2"],
};

export const BLANK_HINTS: Record<string, string> = {
  GARMENT: "garment name, exactly as typed",
  DESIGN: "Design text",
  KEEP: "Keep text",
  PART1_LABEL: "Part 1 name in UPPERCASE",
  PART1: "Part 1 name in lowercase",
  PART2_LABEL: "Part 2 name in UPPERCASE",
  PART2: "Part 2 name in lowercase",
};

const BLANK_SOURCES: Record<string, string> = {
  GARMENT: "Name",
  DESIGN: "Design",
  KEEP: "Keep",
  PART1_LABEL: "Part 1 name",
  PART1: "Part 1 name",
  PART2_LABEL: "Part 2 name",
  PART2: "Part 2 name",
};

export type PromptTexts = {
  look_prompt: string | null | undefined;
  tryon_prompt: string | null | undefined;
  look_tryon_prompt: string | null | undefined;
};

export type BlankSource = {
  name?: string | null;
  design_text?: string | null;
  keep_text?: string | null;
  part1_name?: string | null;
  part2_name?: string | null;
};

export function blankValues(setKey: PromptSetKey, garment: BlankSource): Record<string, string> {
  const text = (value: string | null | undefined) => (value ?? "").trim();
  if (setKey === "single") return { GARMENT: text(garment.name) };
  const part1 = text(garment.part1_name);
  const part2 = text(garment.part2_name);
  return {
    DESIGN: text(garment.design_text),
    KEEP: text(garment.keep_text),
    PART1_LABEL: part1.toUpperCase(),
    PART1: part1.toLowerCase(),
    PART2_LABEL: part2.toUpperCase(),
    PART2: part2.toLowerCase(),
  };
}

function blankPattern(setKey: PromptSetKey): RegExp {
  const names = [...SET_BLANKS[setKey]].sort((a, b) => b.length - a.length);
  return new RegExp(`\\[(${names.join("|")})\\]`, "g");
}

export function fillBlanks(template: string | null | undefined, setKey: PromptSetKey, values: Record<string, string>): string {
  if (!template) return "";
  return template.replace(blankPattern(setKey), (_match, name: string) => values[name] ?? "");
}

export function missingBlanks(setKey: PromptSetKey, texts: PromptTexts, values: Record<string, string>): string[] {
  const used = new Set<string>();
  for (const template of [texts.look_prompt, texts.tryon_prompt, texts.look_tryon_prompt]) {
    for (const match of (template ?? "").matchAll(blankPattern(setKey))) used.add(match[1]);
  }
  const missing: string[] = [];
  for (const name of SET_BLANKS[setKey]) {
    const source = BLANK_SOURCES[name];
    if (used.has(name) && !values[name] && !missing.includes(source)) missing.push(source);
  }
  return missing;
}

/** Square-bracket tokens in a template that are not blanks of this set (sent to the AI as typed). */
export function unknownTokens(setKey: PromptSetKey, texts: PromptTexts): string[] {
  const known = new Set(SET_BLANKS[setKey]);
  const found = new Set<string>();
  for (const template of [texts.look_prompt, texts.tryon_prompt, texts.look_tryon_prompt]) {
    for (const match of (template ?? "").matchAll(/\[([A-Z0-9_]+)\]/g)) {
      if (!known.has(match[1])) found.add(`[${match[1]}]`);
    }
  }
  return [...found];
}

/** Same rule as the backend: lowercase, every run of spaces / symbols -> "_". */
export function makeSlug(name: string): string {
  return name.trim().toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "");
}

export function slotPartName(slot: AdminFabricSlotRow): string {
  return (slot.label || slot.apply_to || "").replace(/\s+fabric$/i, "").trim();
}

/** Part name to prefill from an existing slot: one whose slug is that slot's apply_to, so saving
 * the garment on a library set keeps the slot (and the looks made with it) unchanged. */
export function partNameForSlot(slot: AdminFabricSlotRow): string {
  const fromLabel = slotPartName(slot);
  if (makeSlug(fromLabel) === slot.apply_to) return fromLabel;
  const words = (slot.apply_to || "").replace(/_+/g, " ").trim();
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : fromLabel;
}

export function fabricsSummary(garment: GarmentOverviewRow): string {
  const slots = garment.fabric_slots ?? [];
  if (slots.length === 0) return "1 · whole outfit";
  return `${slots.length} · ${slots.map(slotPartName).join(" / ")}`;
}

export function promptsSummary(garment: GarmentOverviewRow): string {
  if (garment.prompt_set && garment.prompt_set in SET_LABELS) {
    return `${SET_LABELS[garment.prompt_set]} · Version ${garment.prompt_set_version ?? "?"}`;
  }
  return "Own prompts";
}

/** "Ready", or what is missing. */
export function garmentStatus(garment: GarmentOverviewRow): { ready: boolean; text: string } {
  if (garment.is_active === false) return { ready: false, text: "Archived" };
  const missing: string[] = [];
  if (!garment.default_hero_image_id) missing.push("No hero");
  if (!(garment.look_prompt ?? "").trim() || !(garment.tryon_prompt ?? "").trim()) missing.push("No prompts");
  else if (!(garment.look_tryon_prompt ?? "").trim()) missing.push("No look try-on prompt");
  return missing.length ? { ready: false, text: missing.join(", ") } : { ready: true, text: "Ready" };
}

export const CREDITS_PER_LOOK = 50;

export function looksFor(credits: number): number {
  return Math.trunc((Number(credits) || 0) / CREDITS_PER_LOOK);
}

export function errorMessage(err: unknown): string {
  return err instanceof Error ? err.message : "Unknown error";
}
