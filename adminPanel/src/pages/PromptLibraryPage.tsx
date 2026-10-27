import { useCallback, useEffect, useMemo, useState } from "react";

import { adminFetch } from "../lib/api";
import { useAdminAuth } from "../lib/auth";
import { BLANK_HINTS, SET_LABELS, errorMessage, unknownTokens } from "../lib/promptLibrary";
import type { MoveAllResponse, PromptLibraryResponse, PromptSet, PromptSetKey, PromptSetVersion } from "../lib/types";

type Tab = "look_prompt" | "tryon_prompt" | "look_tryon_prompt";
type Texts = Record<Tab, string>;

const TABS: { key: Tab; label: string }[] = [
  { key: "look_prompt", label: "Look" },
  { key: "tryon_prompt", label: "Try-on" },
  { key: "look_tryon_prompt", label: "Look try-on" },
];

const EMPTY_TEXTS: Texts = { look_prompt: "", tryon_prompt: "", look_tryon_prompt: "" };

function textsOf(version: PromptSetVersion | null | undefined): Texts {
  if (!version) return { ...EMPTY_TEXTS };
  return {
    look_prompt: version.look_prompt ?? "",
    tryon_prompt: version.tryon_prompt ?? "",
    look_tryon_prompt: version.look_tryon_prompt ?? "",
  };
}

function formatDate(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? value
    : date.toLocaleString("en-IN", { day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" });
}

export function PromptLibraryPage() {
  const { session } = useAdminAuth();

  const [data, setData] = useState<PromptLibraryResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [statusText, setStatusText] = useState("");

  const [selectedKey, setSelectedKey] = useState<PromptSetKey>("single");
  const [tab, setTab] = useState<Tab>("look_prompt");
  const [draft, setDraft] = useState<Texts>({ ...EMPTY_TEXTS });
  const [draftBase, setDraftBase] = useState<number | null>(null);
  const [note, setNote] = useState("");
  const [viewing, setViewing] = useState<PromptSetVersion | null>(null);
  const [busy, setBusy] = useState("");

  const selected: PromptSet | null = data?.sets.find((item) => item.key === selectedKey) ?? null;
  const latest = selected?.versions[0] ?? null;

  const load = useCallback(async () => {
    if (!session) return null;
    setLoading(true);
    setError("");
    try {
      const result = await adminFetch<PromptLibraryResponse>(session, "/admin/prompt-library");
      setData(result);
      return result;
    } catch (err) {
      setError(errorMessage(err));
      return null;
    } finally {
      setLoading(false);
    }
  }, [session]);

  useEffect(() => {
    void load();
  }, [load]);

  // Editor starts from the set's latest version (or empty when it has none).
  useEffect(() => {
    if (!data) return;
    const set = data.sets.find((item) => item.key === selectedKey);
    const newest = set?.versions[0] ?? null;
    if (draftBase === (newest?.version ?? 0)) return;
    setDraft(textsOf(newest));
    setDraftBase(newest?.version ?? 0);
    setViewing(null);
  }, [data, selectedKey]);

  const dirty = useMemo(() => {
    const base = textsOf(latest);
    return TABS.some(({ key }) => draft[key] !== base[key]);
  }, [draft, latest]);

  function selectSet(key: PromptSetKey) {
    if (key === selectedKey) return;
    if (dirty && !window.confirm("Discard your unsaved changes to this set?")) return;
    setSelectedKey(key);
    setDraftBase(null);
    setNote("");
    setStatusText("");
  }

  async function handleSave() {
    if (!session || !selected) return;
    setBusy("save");
    setStatusText("");
    try {
      const saved = await adminFetch<PromptSetVersion>(session, `/admin/prompt-library/${selected.key}/versions`, {
        method: "POST",
        body: JSON.stringify({ ...draft, note: note.trim() || null }),
      });
      setNote("");
      setDraftBase(null);
      await load();
      setStatusText(`Saved ${selected.label} · Version ${saved.version}.`);
    } catch (err) {
      setStatusText(`Save failed: ${errorMessage(err)}`);
    } finally {
      setBusy("");
    }
  }

  async function handleRestore(version: PromptSetVersion) {
    if (!session || !selected) return;
    if (dirty && !window.confirm("Discard your unsaved changes and restore this version?")) return;
    if (!window.confirm(`Save Version ${version.version}'s text as a new latest version? No garment changes until you move them.`)) return;
    setBusy(`restore-${version.version}`);
    try {
      const saved = await adminFetch<PromptSetVersion>(
        session,
        `/admin/prompt-library/${selected.key}/versions/${version.version}/restore`,
        { method: "POST", body: JSON.stringify({}) }
      );
      setDraftBase(null);
      await load();
      setStatusText(`Restored Version ${version.version} as Version ${saved.version}.`);
    } catch (err) {
      setStatusText(`Restore failed: ${errorMessage(err)}`);
    } finally {
      setBusy("");
    }
  }

  const moveTarget = viewing ?? latest;
  const willChange = selected && moveTarget ? selected.garments_count - moveTarget.garments_count : 0;

  async function handleMoveAll() {
    if (!session || !selected || !moveTarget) return;
    const confirmed = window.confirm(
      `Move ${willChange} garment${willChange === 1 ? "" : "s"} (all shops) to ${selected.label} · Version ${moveTarget.version}?\n\n` +
        "Their prompts are filled again from this version. Each change is saved in the garment's prompt history."
    );
    if (!confirmed) return;
    setBusy("move");
    try {
      const result = await adminFetch<MoveAllResponse>(session, `/admin/prompt-library/${selected.key}/move-all`, {
        method: "POST",
        body: JSON.stringify({ version: moveTarget.version }),
      });
      await load();
      const failed = result.failed.length
        ? ` ${result.failed.length} could not move: ${result.failed.map((item) => `${item.name || item.garment_id} (${item.error})`).join("; ")}`
        : "";
      setStatusText(`Moved ${result.moved} garment${result.moved === 1 ? "" : "s"} to Version ${result.version}.${failed}`);
    } catch (err) {
      setStatusText(`Move failed: ${errorMessage(err)}`);
    } finally {
      setBusy("");
    }
  }

  const shownTexts = viewing ? textsOf(viewing) : draft;
  const unknown = selected ? unknownTokens(selected.key, shownTexts) : [];
  const canSave = !!draft.look_prompt.trim() && !!draft.tryon_prompt.trim() && (dirty || !latest);

  return (
    <div className="page">
      <header className="page-head">
        <div>
          <h1>Prompt library</h1>
          <p className="muted">Shared prompt sets for all shops. Saving never overwrites: each save is a new version.</p>
        </div>
        <button className="btn" type="button" onClick={() => void load()} disabled={loading}>
          Refresh
        </button>
      </header>

      {error ? <p className="error-text">Could not load the prompt library: {error}</p> : null}
      {statusText ? <p className="notice">{statusText}</p> : null}

      <div className="library-layout">
        <nav className="set-list" aria-label="Prompt sets">
          {(data?.sets ?? (["single", "two"] as PromptSetKey[]).map((key) => ({ key, label: SET_LABELS[key], garments_count: 0, latest_version: null }))).map(
            (set) => (
              <button
                key={set.key}
                type="button"
                className={set.key === selectedKey ? "set-item is-active" : "set-item"}
                onClick={() => selectSet(set.key)}
              >
                <span className="cell-title">{set.label}</span>
                <span className="tiny muted">
                  {set.garments_count} garment{set.garments_count === 1 ? "" : "s"} ·{" "}
                  {set.latest_version ? `Version ${set.latest_version}` : "no version yet"}
                </span>
              </button>
            )
          )}
        </nav>

        <section className="section library-editor">
          {!selected ? (
            <p className="muted">{loading ? "Loading…" : "Prompt library not loaded."}</p>
          ) : (
            <>
              <div className="row spread">
                <div>
                  <h2>{selected.label}</h2>
                  <p className="tiny muted">
                    {viewing
                      ? `Viewing Version ${viewing.version} (read-only)`
                      : latest
                        ? `Editing from Version ${latest.version}${dirty ? " · unsaved changes" : ""}`
                        : "No version yet: paste Version 1 below."}
                  </p>
                </div>
                {moveTarget ? (
                  <button className="btn" type="button" onClick={() => void handleMoveAll()} disabled={!!busy || willChange <= 0}>
                    {busy === "move"
                      ? "Moving…"
                      : willChange > 0
                        ? `Move all garments to Version ${moveTarget.version}`
                        : `All garments are on Version ${moveTarget.version}`}
                  </button>
                ) : null}
              </div>

              {viewing ? (
                <div className="notice row">
                  <span>Viewing Version {viewing.version} · {formatDate(viewing.created_at)}{viewing.note ? ` · ${viewing.note}` : ""}</span>
                  <button className="btn btn-small" type="button" onClick={() => void handleRestore(viewing)} disabled={!!busy || viewing.version === latest?.version}>
                    Restore
                  </button>
                  <button className="btn btn-small" type="button" onClick={() => setViewing(null)}>
                    Back to editing
                  </button>
                </div>
              ) : null}

              <div className="tabs">
                {TABS.map((item) => (
                  <button key={item.key} type="button" className={tab === item.key ? "tab is-active" : "tab"} onClick={() => setTab(item.key)}>
                    {item.label}
                    {!shownTexts[item.key].trim() ? <span className="tab-dot" title="Empty" /> : null}
                  </button>
                ))}
              </div>

              <textarea
                className="input mono editor"
                value={shownTexts[tab]}
                readOnly={!!viewing}
                onChange={(event) => setDraft((prev) => ({ ...prev, [tab]: event.target.value }))}
                placeholder={tab === "look_tryon_prompt" ? "Look try-on prompt (optional)" : `${TABS.find((t) => t.key === tab)?.label} prompt`}
                spellCheck={false}
              />

              <div className="hint-box tiny">
                <strong>Blanks</strong> (filled for each garment when it is saved):
                <ul className="blank-list">
                  {selected.blanks.map((blank) => (
                    <li key={blank}>
                      <code>[{blank}]</code> = {BLANK_HINTS[blank] ?? blank}
                    </li>
                  ))}
                </ul>
                <span className="muted">
                  {"{garment_name}, {fabric_mapping} and {image_count} are still filled at generation time, as today."}
                </span>
              </div>
              {unknown.length ? (
                <p className="tiny warn-text">Not a blank of this set, sent to the AI as typed: {unknown.join(", ")}</p>
              ) : null}

              {!viewing ? (
                <div className="row">
                  <input className="input note-input" value={note} onChange={(event) => setNote(event.target.value)} placeholder="Note, e.g. Fixed collar wording" />
                  <button className="btn btn-primary" type="button" onClick={() => void handleSave()} disabled={!!busy || !canSave}>
                    {busy === "save" ? "Saving…" : latest ? "Save as new version" : "Save as Version 1"}
                  </button>
                  {!draft.look_prompt.trim() || !draft.tryon_prompt.trim() ? (
                    <span className="tiny muted">Look and Try-on are required.</span>
                  ) : null}
                </div>
              ) : null}

              <div className="stack">
                <h3>History</h3>
                {selected.versions.length === 0 ? (
                  <p className="tiny muted">No versions yet.</p>
                ) : (
                  <ul className="history-list">
                    {selected.versions.map((version) => (
                      <li key={version.id} className={viewing?.version === version.version ? "is-active" : undefined}>
                        <span>
                          <strong>Version {version.version}</strong>
                          <span className="muted">
                            {" "}
                            · {formatDate(version.created_at)} · {version.note || "no note"} · {version.garments_count} garment
                            {version.garments_count === 1 ? "" : "s"}
                          </span>
                        </span>
                        <span className="row tight">
                          <button className="btn btn-small" type="button" onClick={() => setViewing(version)}>
                            View
                          </button>
                          <button
                            className="btn btn-small"
                            type="button"
                            onClick={() => void handleRestore(version)}
                            disabled={!!busy || version.version === latest?.version}
                          >
                            {busy === `restore-${version.version}` ? "Restoring…" : "Restore"}
                          </button>
                        </span>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            </>
          )}
        </section>
      </div>
    </div>
  );
}
