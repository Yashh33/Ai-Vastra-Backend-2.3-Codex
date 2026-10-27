import { useEffect, useMemo, useState } from "react";

import { adminFetch } from "../../lib/api";
import {
  SET_LABELS,
  blankValues,
  errorMessage,
  fillBlanks,
  makeSlug,
  missingBlanks,
  partNameForSlot,
} from "../../lib/promptLibrary";
import type {
  AdminFabricSlotRow,
  AdminFolderRow,
  AdminHeroImageRow,
  AdminSession,
  GarmentOverviewRow,
  PromptLibraryResponse,
  PromptSetKey,
  PromptVersion,
  SkillParseResponse,
} from "../../lib/types";

type FabricMode = "one" | "two";
type PromptTab = "look_prompt" | "tryon_prompt" | "look_tryon_prompt";

const PROMPT_TABS: { key: PromptTab; label: string }[] = [
  { key: "look_prompt", label: "Look" },
  { key: "tryon_prompt", label: "Try-on" },
  { key: "look_tryon_prompt", label: "Look try-on" },
];

const HERO_TIP = "Head to above knee, plain white/grey garment, portrait, plain background.";

type Props = {
  session: AdminSession;
  shopId: string;
  /** null = new garment */
  garment: GarmentOverviewRow | null;
  library: PromptLibraryResponse | null;
  libraryError: string;
  onClose: (message?: string) => void;
};

function sortSlots(slots: AdminFabricSlotRow[]): AdminFabricSlotRow[] {
  return [...slots].sort((a, b) => a.sort_order - b.sort_order || String(a.created_at).localeCompare(String(b.created_at)));
}

function Seg<T extends string>({
  value,
  options,
  onChange,
  disabled,
}: {
  value: T;
  options: { value: T; label: string }[];
  onChange: (value: T) => void;
  disabled?: boolean;
}) {
  return (
    <div className="seg" role="radiogroup">
      {options.map((option) => (
        <button
          key={option.value}
          type="button"
          role="radio"
          aria-checked={value === option.value}
          className={value === option.value ? "seg-btn is-on" : "seg-btn"}
          onClick={() => onChange(option.value)}
          disabled={disabled}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}

export function GarmentPanel({ session, shopId, garment, library, libraryError, onClose }: Props) {
  const isNew = !garment;
  const base = `/admin/shops/${encodeURIComponent(shopId)}`;
  const initialSlots = sortSlots(garment?.fabric_slots ?? []);
  const initialFabric: FabricMode = garment?.prompt_set
    ? garment.prompt_set === "single"
      ? "one"
      : "two"
    : initialSlots.length === 0
      ? "one"
      : "two";
  const twoSlots = initialSlots.length === 2;

  // --- form
  const [name, setName] = useState(garment?.name ?? "");
  const [category, setCategory] = useState(garment?.category || "unisex");
  const [fabricMode, setFabricMode] = useState<FabricMode>(initialFabric);
  const [part1Name, setPart1Name] = useState(garment?.part1_name ?? (twoSlots ? partNameForSlot(initialSlots[0]) : ""));
  const [part1Sam, setPart1Sam] = useState(garment?.part1_sam_word ?? "");
  const [part2Name, setPart2Name] = useState(garment?.part2_name ?? (twoSlots ? partNameForSlot(initialSlots[1]) : ""));
  const [part2Sam, setPart2Sam] = useState(garment?.part2_sam_word ?? "");
  const [part2Default, setPart2Default] = useState<"white" | "black">(garment?.part2_default === "black" ? "black" : "white");
  const [designText, setDesignText] = useState(garment?.design_text ?? "");
  const [keepText, setKeepText] = useState(garment?.keep_text ?? "");
  const [fabricsTouched, setFabricsTouched] = useState(false);

  const startsOwn = !isNew && !garment?.prompt_set;
  const [ownPrompts, setOwnPrompts] = useState(startsOwn);
  const [advancedOpen, setAdvancedOpen] = useState(startsOwn);
  const [lookPrompt, setLookPrompt] = useState(garment?.look_prompt ?? "");
  const [tryonPrompt, setTryonPrompt] = useState(garment?.tryon_prompt ?? "");
  const [lookTryonPrompt, setLookTryonPrompt] = useState(garment?.look_tryon_prompt ?? "");
  const [note, setNote] = useState("");
  const [previewTab, setPreviewTab] = useState<PromptTab>("look_prompt");

  const [pasteText, setPasteText] = useState("");
  const [pasteMessage, setPasteMessage] = useState("");
  const [parsing, setParsing] = useState(false);

  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState("");
  const [busyAction, setBusyAction] = useState("");

  // --- existing garment data
  const [generations, setGenerations] = useState<number | null>(isNew ? 0 : null);
  const [heroImages, setHeroImages] = useState<AdminHeroImageRow[]>([]);
  const [defaultHeroId, setDefaultHeroId] = useState(garment?.default_hero_image_id ?? null);
  const [heroFile, setHeroFile] = useState<File | null>(null);
  const [heroInputKey, setHeroInputKey] = useState(0);
  const [heroStatus, setHeroStatus] = useState("");
  const [uploadingHero, setUploadingHero] = useState(false);

  const [slots, setSlots] = useState<AdminFabricSlotRow[]>(initialSlots);
  const [newSlotLabel, setNewSlotLabel] = useState("");
  const [newSlotApplyTo, setNewSlotApplyTo] = useState("");
  const [newSlotSortOrder, setNewSlotSortOrder] = useState(0);
  const [slotStatus, setSlotStatus] = useState("");

  const [history, setHistory] = useState<PromptVersion[] | null>(null);
  const [historyOpen, setHistoryOpen] = useState(false);

  const locked = !isNew && (generations ?? 1) > 0;
  const customSlots = slots.length > 0 && slots.length !== 2;

  useEffect(() => {
    if (!garment) return;
    adminFetch<{ generations: number }>(session, `${base}/garments/${encodeURIComponent(garment.id)}/usage`)
      .then((result) => setGenerations(result.generations))
      .catch(() => setGenerations(null));
    void loadHeroImages();
  }, [garment?.id]);

  async function loadHeroImages() {
    if (!garment) return;
    try {
      setHeroImages(
        await adminFetch<AdminHeroImageRow[]>(
          session,
          `${base}/hero-images?folder_id=${encodeURIComponent(garment.id)}&limit=30`
        )
      );
    } catch {
      setHeroImages([]);
    }
  }

  async function reloadSlots() {
    if (!garment) return;
    try {
      setSlots(sortSlots(await adminFetch<AdminFabricSlotRow[]>(session, `${base}/folders/${encodeURIComponent(garment.id)}/fabric-slots`)));
    } catch {
      // keep what is shown
    }
  }

  // --- library set + version for this form
  const setKey: PromptSetKey = fabricMode === "one" ? "single" : "two";
  const promptSet = library?.sets.find((item) => item.key === setKey) ?? null;
  const onThisSet = !!garment && garment.prompt_set === setKey && !!garment.prompt_set_version;
  const versionNumber = onThisSet ? garment!.prompt_set_version! : promptSet?.latest_version ?? null;
  const versionRow = promptSet?.versions.find((item) => item.version === versionNumber) ?? null;
  const newerVersion =
    onThisSet && promptSet?.latest_version && promptSet.latest_version > versionNumber! ? promptSet.latest_version : null;

  const values = useMemo(
    () =>
      blankValues(setKey, {
        name,
        design_text: designText,
        keep_text: keepText,
        part1_name: part1Name,
        part2_name: part2Name,
      }),
    [setKey, name, designText, keepText, part1Name, part2Name]
  );
  const preview = useMemo(
    () =>
      versionRow
        ? {
            look_prompt: fillBlanks(versionRow.look_prompt, setKey, values),
            tryon_prompt: fillBlanks(versionRow.tryon_prompt, setKey, values),
            look_tryon_prompt: fillBlanks(versionRow.look_tryon_prompt, setKey, values),
          }
        : null,
    [versionRow, setKey, values]
  );
  const missing = versionRow ? missingBlanks(setKey, versionRow, values) : [];

  const blockers: string[] = [];
  if (!name.trim()) blockers.push("Name is required");
  const fabricsApply = !ownPrompts || isNew || fabricsTouched;
  if (fabricMode === "two" && fabricsApply) {
    if (!part1Name.trim()) blockers.push("Part 1 name is required");
    if (!part2Name.trim()) blockers.push("Part 2 name is required");
    if (part1Name.trim() && part2Name.trim() && makeSlug(part1Name) === makeSlug(part2Name)) {
      blockers.push("Part 1 and Part 2 need different names");
    }
  }
  if (ownPrompts) {
    if (!lookPrompt.trim() || !tryonPrompt.trim()) blockers.push("Look and Try-on prompts are required");
  } else if (libraryError && !library) {
    blockers.push(`Prompt library not available: ${libraryError}`);
  } else if (library && !versionRow) {
    blockers.push(`${SET_LABELS[setKey]} has no version yet. Save Version 1 in Prompt library, or use your own prompts (Advanced).`);
  } else if (missing.length) {
    blockers.push(`Fill in ${missing.join(", ")} before saving`);
  }

  function touchFabric<T>(setter: (value: T) => void) {
    return (value: T) => {
      setter(value);
      setFabricsTouched(true);
    };
  }

  function changeFabricMode(next: FabricMode) {
    if (locked || next === fabricMode) return;
    setFabricMode(next);
    setFabricsTouched(true);
  }

  function setOwnPromptsMode(next: boolean) {
    if (next && preview && !lookPrompt.trim() && !tryonPrompt.trim()) {
      setLookPrompt(preview.look_prompt);
      setTryonPrompt(preview.tryon_prompt);
      setLookTryonPrompt(preview.look_tryon_prompt);
    }
    setOwnPrompts(next);
  }

  async function handleFillForm() {
    if (!pasteText.trim()) return;
    setParsing(true);
    setPasteMessage("");
    try {
      const { fields, filled } = await adminFetch<SkillParseResponse>(session, "/admin/prompt-library/parse-skill", {
        method: "POST",
        body: JSON.stringify({ text: pasteText }),
      });
      const notes: string[] = [];
      const applied = [...filled];
      if (fields.name !== undefined) setName(fields.name);
      if (fields.category !== undefined) setCategory(fields.category);
      if (fields.prompt_set) {
        const wanted: FabricMode = fields.prompt_set === "single" ? "one" : "two";
        if (wanted !== fabricMode && locked) {
          notes.push("Fabrics not changed: this garment already has looks");
          if (applied.includes("Fabrics")) applied.splice(applied.indexOf("Fabrics"), 1);
        } else if (wanted !== fabricMode) {
          setFabricMode(wanted);
          setFabricsTouched(true);
        }
      }
      if (fields.part1_name !== undefined) touchFabric(setPart1Name)(fields.part1_name);
      if (fields.part1_sam_word !== undefined) setPart1Sam(fields.part1_sam_word);
      if (fields.part2_name !== undefined) touchFabric(setPart2Name)(fields.part2_name);
      if (fields.part2_sam_word !== undefined) setPart2Sam(fields.part2_sam_word);
      if (fields.part2_default) setPart2Default(fields.part2_default);
      if (fields.design_text !== undefined) setDesignText(fields.design_text);
      if (fields.keep_text !== undefined) setKeepText(fields.keep_text);
      if (applied.length) setOwnPrompts(false);
      setPasteMessage(
        applied.length
          ? `Filled: ${applied.join(", ")}.${notes.length ? ` ${notes.join(". ")}.` : ""} Check the form, then save.`
          : "Nothing recognised. Lines look like “Name: Shirt + Pant”."
      );
    } catch (err) {
      setPasteMessage(`Could not read it: ${errorMessage(err)}`);
    } finally {
      setParsing(false);
    }
  }

  async function uploadHero(folderId: string, file: File, makeDefault: boolean) {
    const formData = new FormData();
    formData.append("folder_id", folderId);
    formData.append("file", file);
    const created = await adminFetch<AdminHeroImageRow>(session, `${base}/hero-images/upload`, { method: "POST", body: formData });
    if (makeDefault) {
      await adminFetch(session, `${base}/folders/${encodeURIComponent(folderId)}/default-hero`, {
        method: "PATCH",
        body: JSON.stringify({ default_hero_image_id: created.id }),
      });
    }
    return created;
  }

  async function handleHeroUploadNow() {
    if (!garment || !heroFile) return;
    setUploadingHero(true);
    setHeroStatus("");
    try {
      const makeDefault = !defaultHeroId;
      const created = await uploadHero(garment.id, heroFile, makeDefault);
      if (makeDefault) setDefaultHeroId(created.id);
      setHeroFile(null);
      setHeroInputKey((key) => key + 1);
      setHeroStatus(makeDefault ? "Hero uploaded and set as default." : "Hero uploaded.");
      await loadHeroImages();
    } catch (err) {
      setHeroStatus(`Hero upload failed: ${errorMessage(err)}`);
    } finally {
      setUploadingHero(false);
    }
  }

  async function handleSetDefaultHero(heroId: string) {
    if (!garment) return;
    try {
      await adminFetch(session, `${base}/folders/${encodeURIComponent(garment.id)}/default-hero`, {
        method: "PATCH",
        body: JSON.stringify({ default_hero_image_id: heroId }),
      });
      setDefaultHeroId(heroId);
      setHeroStatus("Default hero updated.");
    } catch (err) {
      setHeroStatus(`Failed: ${errorMessage(err)}`);
    }
  }

  async function handleSave() {
    if (blockers.length || saving) return;
    setSaving(true);
    setSaveError("");
    try {
      const body = {
        name: name.trim(),
        category,
        mode: ownPrompts ? "own" : "library",
        // Own prompts on an existing garment: leave its fabric slots alone unless step 2 was changed.
        fabric_mode: fabricsApply ? fabricMode : null,
        prompt_set_version: ownPrompts ? null : versionNumber,
        design_text: designText,
        keep_text: keepText,
        part1_name: part1Name,
        part1_sam_word: part1Sam,
        part2_name: part2Name,
        part2_sam_word: part2Sam,
        part2_default: part2Default,
        look_prompt: lookPrompt,
        tryon_prompt: tryonPrompt,
        look_tryon_prompt: lookTryonPrompt,
        note: note.trim() || null,
      };
      const saved = await adminFetch<AdminFolderRow>(
        session,
        isNew ? `${base}/garments` : `${base}/garments/${encodeURIComponent(garment!.id)}`,
        { method: isNew ? "POST" : "PUT", body: JSON.stringify(body) }
      );

      let message = `Saved ${saved.name}.`;
      if (isNew && heroFile) {
        try {
          await uploadHero(saved.id, heroFile, true);
          message = `Saved ${saved.name} with its hero image.`;
        } catch (err) {
          message = `Saved ${saved.name}, but the hero upload failed (${errorMessage(err)}). Open it to add the hero.`;
        }
      }
      onClose(message);
    } catch (err) {
      setSaveError(errorMessage(err));
      setSaving(false);
    }
  }

  async function handleMoveToLatest() {
    if (!garment || !newerVersion) return;
    if (!window.confirm(`Move ${garment.name} to Version ${newerVersion}? Unsaved changes in this panel are discarded.`)) return;
    setBusyAction("move");
    try {
      await adminFetch(session, `${base}/garments/${encodeURIComponent(garment.id)}/move-to-latest`, { method: "POST" });
      onClose(`${garment.name} moved to ${SET_LABELS[setKey]} · Version ${newerVersion}.`);
    } catch (err) {
      setSaveError(errorMessage(err));
      setBusyAction("");
    }
  }

  async function handleArchive() {
    if (!garment) return;
    const confirmed = window.confirm(
      `Archive garment type "${garment.name}"? Types with no generation history are deleted outright; ` +
        `types with history are archived and can be restored later.`
    );
    if (!confirmed) return;
    setBusyAction("archive");
    try {
      const result = await adminFetch<{ deleted: string | false; archived: boolean; generations?: number }>(
        session,
        `${base}/folders/${encodeURIComponent(garment.id)}`,
        { method: "DELETE" }
      );
      onClose(
        result.archived
          ? `Archived "${garment.name}" (has ${result.generations ?? 0} generations in history).`
          : `Deleted "${garment.name}".`
      );
    } catch (err) {
      setSaveError(`Failed to archive: ${errorMessage(err)}`);
      setBusyAction("");
    }
  }

  async function handleRestore() {
    if (!garment) return;
    setBusyAction("restore");
    try {
      await adminFetch(session, `${base}/folders/${encodeURIComponent(garment.id)}/restore`, { method: "PATCH" });
      onClose(`Restored "${garment.name}".`);
    } catch (err) {
      setSaveError(`Failed to restore: ${errorMessage(err)}`);
      setBusyAction("");
    }
  }

  async function toggleHistory() {
    const next = !historyOpen;
    setHistoryOpen(next);
    if (next && garment) {
      try {
        setHistory(await adminFetch<PromptVersion[]>(session, `${base}/folders/${encodeURIComponent(garment.id)}/prompt-versions`));
      } catch {
        setHistory([]);
      }
    }
  }

  async function handleRevert(versionId: string) {
    if (!garment) return;
    setBusyAction(`revert-${versionId}`);
    try {
      const updated = await adminFetch<AdminFolderRow>(
        session,
        `${base}/folders/${encodeURIComponent(garment.id)}/prompt-versions/${encodeURIComponent(versionId)}/revert`,
        { method: "POST" }
      );
      setLookPrompt(updated.look_prompt ?? "");
      setTryonPrompt(updated.tryon_prompt ?? "");
      setLookTryonPrompt(updated.look_tryon_prompt ?? "");
      setHistory(await adminFetch<PromptVersion[]>(session, `${base}/folders/${encodeURIComponent(garment.id)}/prompt-versions`));
      setSaveError("");
    } catch (err) {
      setSaveError(`Failed to revert: ${errorMessage(err)}`);
    } finally {
      setBusyAction("");
    }
  }

  async function handleAddSlot() {
    if (!garment || !newSlotLabel.trim() || !newSlotApplyTo.trim()) return;
    try {
      await adminFetch(session, `${base}/folders/${encodeURIComponent(garment.id)}/fabric-slots`, {
        method: "POST",
        body: JSON.stringify({ label: newSlotLabel.trim(), apply_to: newSlotApplyTo, sort_order: newSlotSortOrder }),
      });
      setNewSlotLabel("");
      setNewSlotApplyTo("");
      setNewSlotSortOrder(0);
      setSlotStatus("Fabric slot added.");
      await reloadSlots();
    } catch (err) {
      setSlotStatus(`Failed to add fabric slot: ${errorMessage(err)}`);
    }
  }

  async function handleDeleteSlot(slot: AdminFabricSlotRow) {
    if (!garment || !window.confirm(`Delete fabric slot "${slot.label}"? This cannot be undone.`)) return;
    try {
      await adminFetch(session, `${base}/folders/${encodeURIComponent(garment.id)}/fabric-slots/${encodeURIComponent(slot.id)}`, {
        method: "DELETE",
      });
      setSlotStatus(`Deleted fabric slot "${slot.label}".`);
      await reloadSlots();
    } catch (err) {
      setSlotStatus(`Failed to delete fabric slot: ${errorMessage(err)}`);
    }
  }

  const imageOrderLines = [
    "Image 1 → Hero (garment reference)",
    ...slots.map((slot, index) => `Image ${index + 2} → ${slot.label} (slot)`),
    `Image ${slots.length + 2} → Person (only on TRY-ON)`,
  ];
  const archived = garment?.is_active === false;

  return (
    <div className="drawer-overlay">
      <aside className="drawer drawer-wide" aria-label={isNew ? "Add garment" : `Edit ${garment?.name}`}>
        <header className="drawer-head">
          <div>
            <h2>{isNew ? "Add garment" : garment?.name}</h2>
            {!isNew ? (
              <p className="tiny muted">
                {generations === null ? "Checking looks…" : `${generations} look${generations === 1 ? "" : "s"} / try-ons made`}
                {archived ? " · Archived" : ""}
              </p>
            ) : null}
          </div>
          <button type="button" className="btn btn-ghost" onClick={() => onClose()} disabled={saving}>
            Close
          </button>
        </header>

        <form className="drawer-body" onSubmit={(event) => event.preventDefault()}>
          <section className="paste-box">
            <label className="field">
              <span>Paste from skill (optional)</span>
              <textarea
                className="input mono"
                rows={4}
                value={pasteText}
                onChange={(event) => setPasteText(event.target.value)}
                placeholder={"Template: v2 TWO-FABRIC\nName: Shirt + Pant\nPart 1: Shirt (SAM word: shirt)\n…"}
              />
            </label>
            <div className="row">
              <button className="btn" type="button" onClick={() => void handleFillForm()} disabled={parsing || !pasteText.trim()}>
                {parsing ? "Reading…" : "Fill form"}
              </button>
              {pasteMessage ? <span className="tiny muted">{pasteMessage}</span> : null}
            </div>
          </section>

          <div className="panel-grid">
            <div className="stack-lg">
              <section className="stack">
                <h3>1. Basics</h3>
                <div className="grid-2">
                  <label className="field">
                    <span>Name</span>
                    <input className="input" value={name} onChange={(event) => setName(event.target.value)} placeholder="e.g. Shirt + Pant" />
                  </label>
                  <label className="field">
                    <span>Category</span>
                    <select className="input" value={category} onChange={(event) => setCategory(event.target.value)}>
                      <option value="men">Men</option>
                      <option value="women">Women</option>
                      <option value="unisex">Unisex</option>
                    </select>
                  </label>
                </div>
              </section>

              <section className="stack">
                <h3>2. Fabrics</h3>
                <Seg<FabricMode>
                  value={fabricMode}
                  onChange={changeFabricMode}
                  disabled={locked}
                  options={[
                    { value: "one", label: "One fabric" },
                    { value: "two", label: "Two fabrics" },
                  ]}
                />
                {locked ? <p className="tiny muted">This garment already has looks. Create a new garment to change this.</p> : null}
                {customSlots && !fabricsTouched ? (
                  <p className="tiny muted">
                    This garment has {slots.length} custom fabric slot{slots.length === 1 ? "" : "s"}; they stay as they are
                    unless you change this step. Edit them under Advanced.
                  </p>
                ) : null}

                {fabricMode === "two" ? (
                  <div className="stack">
                    <div className="grid-2">
                      <label className="field">
                        <span>Part 1 name (required)</span>
                        <input className="input" value={part1Name} onChange={(event) => touchFabric(setPart1Name)(event.target.value)} placeholder="e.g. Shirt" />
                      </label>
                      <label className="field">
                        <span>Part 1 SAM word</span>
                        <input className="input" value={part1Sam} onChange={(event) => setPart1Sam(event.target.value)} placeholder="e.g. shirt" />
                      </label>
                      <label className="field">
                        <span>Part 2 name (the shop may leave this fabric empty)</span>
                        <input className="input" value={part2Name} onChange={(event) => touchFabric(setPart2Name)(event.target.value)} placeholder="e.g. Pant" />
                      </label>
                      <label className="field">
                        <span>Part 2 SAM word</span>
                        <input className="input" value={part2Sam} onChange={(event) => setPart2Sam(event.target.value)} placeholder="e.g. pants" />
                      </label>
                    </div>
                    <div className="field">
                      <span>Part 2 empty → default</span>
                      <Seg<"white" | "black">
                        value={part2Default}
                        onChange={setPart2Default}
                        options={[
                          { value: "white", label: "White" },
                          { value: "black", label: "Black" },
                        ]}
                      />
                    </div>
                    {part1Name.trim() && part2Name.trim() ? (
                      <p className="tiny muted">
                        Fabric slots: 1 = “{part1Name.trim()} fabric” ({makeSlug(part1Name) || "?"}), 2 = “{part2Name.trim()} fabric” (
                        {makeSlug(part2Name) || "?"})
                      </p>
                    ) : null}
                    {locked ? <p className="tiny muted">With looks, part names may only change in ways that keep the same slot word.</p> : null}
                  </div>
                ) : (
                  <p className="tiny muted">The shop uploads one fabric for the whole outfit.</p>
                )}
              </section>

              <section className="stack">
                <h3>3. Hero image</h3>
                <p className="tiny muted">Tip: {HERO_TIP}</p>
                {isNew ? (
                  <label className="field">
                    <span>Hero image (uploaded when you save, and set as default)</span>
                    <input key={heroInputKey} type="file" accept="image/*" onChange={(event) => setHeroFile(event.target.files?.[0] || null)} />
                  </label>
                ) : (
                  <>
                    <div className="row">
                      <input key={heroInputKey} type="file" accept="image/*" onChange={(event) => setHeroFile(event.target.files?.[0] || null)} disabled={uploadingHero} />
                      <button className="btn" type="button" onClick={() => void handleHeroUploadNow()} disabled={!heroFile || uploadingHero}>
                        {uploadingHero ? "Uploading…" : "Upload hero"}
                      </button>
                    </div>
                    {heroStatus ? <p className="tiny muted">{heroStatus}</p> : null}
                    {heroImages.length === 0 ? (
                      <p className="tiny muted">No hero images yet.</p>
                    ) : (
                      <div className="hero-grid">
                        {heroImages.map((hero) => {
                          const url = hero.thumb_signed_url || hero.signed_url;
                          const isDefault = hero.id === defaultHeroId;
                          return (
                            <figure key={hero.id} className={isDefault ? "hero-tile is-default" : "hero-tile"}>
                              {url ? <img src={url} alt={hero.original_filename || "Hero"} loading="lazy" /> : <div className="thumb-empty">No preview</div>}
                              <figcaption>
                                {isDefault ? (
                                  <span className="pill pill-ok">Default</span>
                                ) : (
                                  <button className="btn btn-small" type="button" onClick={() => void handleSetDefaultHero(hero.id)}>
                                    Set as default
                                  </button>
                                )}
                              </figcaption>
                            </figure>
                          );
                        })}
                      </div>
                    )}
                  </>
                )}
              </section>
            </div>

            <div className="stack-lg">
              <section className="stack">
                <h3>4. Prompts</h3>
                {ownPrompts ? (
                  <p className="muted">
                    This garment uses <strong>its own prompts</strong> (Advanced). Untick “Use my own prompts” to use the{" "}
                    {SET_LABELS[setKey]} set.
                  </p>
                ) : (
                  <>
                    <p>
                      <strong>{SET_LABELS[setKey]}</strong>{" "}
                      <span className="muted">
                        · {versionRow ? `Version ${versionRow.version}` : library ? "no version yet" : "loading…"}
                      </span>
                    </p>
                    {newerVersion ? (
                      <div className="notice row">
                        <span>Version {newerVersion} available</span>
                        <button className="btn btn-small" type="button" onClick={() => void handleMoveToLatest()} disabled={!!busyAction}>
                          {busyAction === "move" ? "Moving…" : "Move to latest"}
                        </button>
                      </div>
                    ) : null}
                    {fabricMode === "two" ? (
                      <div className="stack">
                        <label className="field">
                          <span>Design</span>
                          <textarea className="input" rows={3} value={designText} onChange={(event) => setDesignText(event.target.value)} placeholder="a half-sleeve shirt with a spread collar, …" />
                        </label>
                        <label className="field">
                          <span>Keep</span>
                          <textarea className="input" rows={2} value={keepText} onChange={(event) => setKeepText(event.target.value)} placeholder="collar, half sleeves, buttons, …" />
                        </label>
                      </div>
                    ) : null}
                    {missing.length ? <p className="error-text">Still empty: {missing.join(", ")}</p> : null}
                    <div className="stack">
                      <Seg<PromptTab> value={previewTab} onChange={setPreviewTab} options={PROMPT_TABS.map((tab) => ({ value: tab.key, label: tab.label }))} />
                      <pre className="prompt-preview">
                        {preview ? preview[previewTab] || "(empty in this version)" : "No version of this set yet."}
                      </pre>
                      <p className="tiny muted">Read-only preview, filled as you type. Saving writes exactly this.</p>
                    </div>
                  </>
                )}
              </section>

              <section className="advanced">
                <button type="button" className="advanced-toggle" onClick={() => setAdvancedOpen((open) => !open)} aria-expanded={advancedOpen}>
                  Advanced {advancedOpen ? "▾" : "▸"}
                </button>
                {advancedOpen ? (
                  <div className="stack">
                    <label className="check">
                      <input type="checkbox" checked={ownPrompts} onChange={(event) => setOwnPromptsMode(event.target.checked)} />
                      Use my own prompts for this garment
                    </label>

                    {ownPrompts ? (
                      <>
                        <label className="field">
                          <span>Look prompt</span>
                          <textarea className="input mono" rows={7} value={lookPrompt} onChange={(event) => setLookPrompt(event.target.value)} />
                        </label>
                        <label className="field">
                          <span>Try-on prompt</span>
                          <textarea className="input mono" rows={7} value={tryonPrompt} onChange={(event) => setTryonPrompt(event.target.value)} />
                        </label>
                        <div className="hint-box tiny">
                          <strong>Image order Gemini receives:</strong>
                          <ul>
                            {imageOrderLines.map((line) => (
                              <li key={line}>{line}</li>
                            ))}
                          </ul>
                        </div>
                        <label className="field">
                          <span>Look try-on prompt (Browse → Try on customer)</span>
                          <textarea className="input mono" rows={5} value={lookTryonPrompt} onChange={(event) => setLookTryonPrompt(event.target.value)} />
                        </label>
                        {!lookTryonPrompt.trim() ? (
                          <p className="tiny warn-text">Missing: “Try on customer” from Browse won't work for this garment.</p>
                        ) : null}
                        <div className="hint-box tiny">
                          <strong>Image order for the Look try-on prompt:</strong>
                          <ul>
                            <li>Image 1 → Finished look (garment reference)</li>
                            <li>Image 2 → Person</li>
                          </ul>
                        </div>
                      </>
                    ) : null}

                    {!isNew ? (
                      <div className="stack">
                        <button className="btn btn-small" type="button" onClick={() => void toggleHistory()}>
                          {historyOpen ? "Hide prompt history" : "Show prompt history"}
                        </button>
                        {historyOpen ? (
                          history === null ? (
                            <p className="tiny muted">Loading…</p>
                          ) : history.length === 0 ? (
                            <p className="tiny muted">No saved versions yet.</p>
                          ) : (
                            <ul className="history-list">
                              {history.map((version) => (
                                <li key={version.id}>
                                  <div>
                                    <div className="tiny">{new Date(version.created_at).toLocaleString()}</div>
                                    <div className="tiny muted">{version.note || "(no note)"}</div>
                                  </div>
                                  {ownPrompts && !garment?.prompt_set ? (
                                    <button
                                      className="btn btn-small"
                                      type="button"
                                      disabled={!!busyAction}
                                      onClick={() => void handleRevert(version.id)}
                                    >
                                      {busyAction === `revert-${version.id}` ? "Reverting…" : "Revert to this"}
                                    </button>
                                  ) : null}
                                </li>
                              ))}
                            </ul>
                          )
                        ) : null}
                        {historyOpen && garment?.prompt_set ? (
                          <p className="tiny muted">Library garments change version from the Prompts step or the Prompt library.</p>
                        ) : null}
                      </div>
                    ) : null}

                    {!isNew && ownPrompts ? (
                      <div className="stack">
                        <h4>Fabric slots (custom)</h4>
                        <p className="tiny muted">Changes here save immediately. Library garments get their slots from step 2.</p>
                        {slots.length === 0 ? (
                          <p className="tiny muted">No fabric slots.</p>
                        ) : (
                          <ul className="history-list">
                            {slots.map((slot) => (
                              <li key={slot.id}>
                                <span>
                                  {slot.sort_order}. {slot.label} <code>{slot.apply_to}</code>
                                </span>
                                <button className="btn btn-small btn-danger-outline" type="button" onClick={() => void handleDeleteSlot(slot)}>
                                  Delete
                                </button>
                              </li>
                            ))}
                          </ul>
                        )}
                        <div className="grid-3">
                          <input className="input" value={newSlotLabel} onChange={(event) => setNewSlotLabel(event.target.value)} placeholder="Label, e.g. Shirt fabric" />
                          <input className="input" value={newSlotApplyTo} onChange={(event) => setNewSlotApplyTo(event.target.value)} placeholder="Apply to, e.g. shirt" />
                          <input className="input" type="number" min={0} value={newSlotSortOrder} onChange={(event) => setNewSlotSortOrder(Number(event.target.value))} />
                        </div>
                        <div className="row">
                          <button className="btn btn-small" type="button" onClick={() => void handleAddSlot()} disabled={!newSlotLabel.trim() || !newSlotApplyTo.trim()}>
                            Add slot
                          </button>
                          {slotStatus ? <span className="tiny muted">{slotStatus}</span> : null}
                        </div>
                      </div>
                    ) : null}
                  </div>
                ) : null}
              </section>
            </div>
          </div>
        </form>

        <footer className="drawer-foot">
          <div className="row">
            {!isNew && !archived ? (
              <button className="btn btn-danger-outline" type="button" onClick={() => void handleArchive()} disabled={saving || !!busyAction}>
                {busyAction === "archive" ? "Archiving…" : "Archive garment"}
              </button>
            ) : null}
            {archived ? (
              <button className="btn" type="button" onClick={() => void handleRestore()} disabled={saving || !!busyAction}>
                {busyAction === "restore" ? "Restoring…" : "Restore garment"}
              </button>
            ) : null}
          </div>
          <div className="foot-right">
            {saveError ? <p className="error-text">{saveError}</p> : null}
            {!saveError && blockers.length ? <p className="tiny warn-text">{blockers[0]}</p> : null}
            <input className="input note-input" value={note} onChange={(event) => setNote(event.target.value)} placeholder="Note (optional, saved with the prompt version)" />
            <button className="btn" type="button" onClick={() => onClose()} disabled={saving}>
              Cancel
            </button>
            <button className="btn btn-primary" type="button" onClick={() => void handleSave()} disabled={saving || blockers.length > 0}>
              {saving ? "Saving…" : isNew ? "Save garment" : "Save"}
            </button>
          </div>
        </footer>
      </aside>
    </div>
  );
}
