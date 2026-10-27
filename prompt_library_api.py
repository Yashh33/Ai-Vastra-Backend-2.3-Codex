"""Admin endpoints for the shared prompt library and for saving garments from the new admin panel.

Same admin auth as admin_api (x-admin-secret). Nothing here changes how generation works:
saving a library garment renders its prompts and writes them into the garment's existing
look_prompt / tryon_prompt / look_tryon_prompt fields, plus a garment_prompt_versions row,
exactly like saving prompts by hand. Needs sql/2026-10-28_prompt_library.sql.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

import prompt_library as lib
from admin_deps import verify_admin_secret
from image_thumbs import hero_thumbnail_path, sign_existing_paths
from supabase_client import get_supabase_admin_client

router = APIRouter(prefix="/admin", tags=["Admin prompt library"], dependencies=[Depends(verify_admin_secret)])

_SQL_FILE = "sql/2026-10-28_prompt_library.sql"
_NEW_SCHEMA_NAMES = (
    "prompt_set_versions",
    "prompt_set",
    "design_text",
    "keep_text",
    "part1_name",
    "part1_sam_word",
    "part2_name",
    "part2_sam_word",
    "part2_default",
)
_ALLOWED_CATEGORIES = {"men", "women", "unisex"}
_GARMENT_LIBRARY_COLUMNS = (
    "prompt_set",
    "prompt_set_version",
    "design_text",
    "keep_text",
    "part1_name",
    "part1_sam_word",
    "part2_name",
    "part2_sam_word",
    "part2_default",
)


class PromptSetVersionCreateRequest(BaseModel):
    look_prompt: str
    tryon_prompt: str
    look_tryon_prompt: Optional[str] = None
    note: Optional[str] = Field(default=None, max_length=300)


class PromptSetRestoreRequest(BaseModel):
    note: Optional[str] = Field(default=None, max_length=300)


class PromptSetMoveAllRequest(BaseModel):
    version: int = Field(..., ge=1)


class ParseSkillRequest(BaseModel):
    text: str = Field(default="", max_length=20_000)


class GarmentSaveRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    category: str = "unisex"
    # library = prompts from the Prompt library set; own = the three prompts below, as typed.
    mode: Literal["library", "own"]
    # one / two fabrics. Required for library; for own, None leaves the fabric slots as they are.
    fabric_mode: Optional[Literal["one", "two"]] = None
    prompt_set_version: Optional[int] = Field(default=None, ge=1)
    design_text: Optional[str] = Field(default=None, max_length=4000)
    keep_text: Optional[str] = Field(default=None, max_length=4000)
    part1_name: Optional[str] = None
    part1_sam_word: Optional[str] = Field(default=None, max_length=80)
    part2_name: Optional[str] = None
    part2_sam_word: Optional[str] = Field(default=None, max_length=80)
    part2_default: Optional[str] = None
    look_prompt: Optional[str] = None
    tryon_prompt: Optional[str] = None
    look_tryon_prompt: Optional[str] = None
    note: Optional[str] = Field(default=None, max_length=300)


# ---------------------------------------------------------------- helpers

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bad_request(message: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=message)


def _is_missing_schema(exc: Exception) -> bool:
    message = str(exc)
    return any(name in message for name in _NEW_SCHEMA_NAMES) and (
        "does not exist" in message or "schema cache" in message or "Could not find" in message
        or "PGRST" in message or "42P01" in message or "42703" in message
    )


def _db_error(exc: Exception, action: str) -> HTTPException:
    if _is_missing_schema(exc):
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"The prompt library tables are not in the database yet. Run {_SQL_FILE} in Supabase first.",
        )
    message = str(exc)
    if "hero_folders_shop_id_name_key" in message or ("duplicate key" in message and "name" in message):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A garment with this name already exists for this shop",
        )
    print(f"[prompt_library_api] ERROR: {action}: {exc.__class__.__name__}: {message[:300]}")
    return HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to {action}")


def _rows(result: Any) -> list[dict[str, Any]]:
    return getattr(result, "data", None) or []


def _clean_optional(value: Optional[str]) -> Optional[str]:
    cleaned = (value or "").strip()
    return cleaned or None


def _set_key_or_400(set_key: str) -> str:
    try:
        return lib.check_set_key(set_key)
    except lib.PromptLibraryError as exc:
        raise _bad_request(str(exc)) from exc


def _list_versions(supabase, set_key: Optional[str] = None) -> list[dict[str, Any]]:
    query = supabase.table("prompt_set_versions").select("*")
    if set_key:
        query = query.eq("set_key", set_key)
    try:
        rows = _rows(query.execute())
    except Exception as exc:
        raise _db_error(exc, "load prompt set versions") from exc
    return sorted(rows, key=lambda row: int(row.get("version") or 0))


def _get_version(supabase, set_key: str, version: int) -> dict[str, Any]:
    for row in _list_versions(supabase, set_key):
        if int(row.get("version") or 0) == int(version):
            return row
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"{lib.set_label(set_key)} has no Version {version}",
    )


def _latest_version(supabase, set_key: str) -> Optional[dict[str, Any]]:
    rows = _list_versions(supabase, set_key)
    return rows[-1] if rows else None


def _insert_version(supabase, set_key: str, texts: dict[str, Optional[str]], note: Optional[str]) -> dict[str, Any]:
    """Save texts as the next version of the set. Never overwrites: (set_key, version) is unique,
    so a parallel save makes this one retry with the following number."""
    try:
        lib.check_version_texts(texts.get("look_prompt"), texts.get("tryon_prompt"))
    except lib.PromptLibraryError as exc:
        raise _bad_request(str(exc)) from exc

    look_tryon = texts.get("look_tryon_prompt")
    payload = {
        "set_key": set_key,
        "look_prompt": texts.get("look_prompt"),
        "tryon_prompt": texts.get("tryon_prompt"),
        "look_tryon_prompt": look_tryon if (look_tryon or "").strip() else None,
        "note": _clean_optional(note),
    }
    last_exc: Optional[Exception] = None
    for _attempt in range(3):
        number = lib.next_version([row.get("version") for row in _list_versions(supabase, set_key)])
        try:
            rows = _rows(supabase.table("prompt_set_versions").insert({**payload, "version": number}).execute())
        except Exception as exc:
            if "duplicate key" in str(exc) or "23505" in str(exc):
                last_exc = exc
                continue
            raise _db_error(exc, "save the prompt set version") from exc
        if not rows:
            raise HTTPException(status_code=500, detail="Version saved but no response was returned")
        return rows[0]
    raise _db_error(last_exc or RuntimeError("conflict"), "save the prompt set version")


def _load_garment(supabase, shop_id: str, garment_id: str) -> dict[str, Any]:
    try:
        rows = _rows(
            supabase.table("garment_types").select("*").eq("id", garment_id).eq("shop_id", shop_id).limit(1).execute()
        )
    except Exception as exc:
        raise _db_error(exc, "load the garment") from exc
    if not rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Garment not found")
    return rows[0]


def _load_slots(supabase, shop_id: str, garment_id: str) -> list[dict[str, Any]]:
    rows = _rows(
        supabase.table("garment_fabric_slots").select("*").eq("folder_id", garment_id).eq("shop_id", shop_id).execute()
    )
    return sorted(rows, key=lambda s: (int(s.get("sort_order") or 0), str(s.get("created_at") or "")))


def _generation_count(supabase, shop_id: str, garment_id: str) -> int:
    result = (
        supabase.table("generations")
        .select("id", count="exact")
        .eq("folder_id", garment_id)
        .eq("shop_id", shop_id)
        .limit(1)
        .execute()
    )
    count = getattr(result, "count", None)
    return int(count) if count is not None else len(_rows(result))


def _apply_slot_plan(supabase, shop_id: str, garment_id: str, plan: dict[str, list[dict[str, Any]]]) -> None:
    # Deletes first, then in-place updates, then inserts: no two slots ever share an apply_to.
    for slot in plan["deletes"]:
        supabase.table("garment_fabric_slots").delete().eq("id", slot["id"]).eq("shop_id", shop_id).eq(
            "folder_id", garment_id
        ).execute()
    for change in plan["updates"]:
        fields = {key: value for key, value in change.items() if key != "id"}
        supabase.table("garment_fabric_slots").update(fields).eq("id", change["id"]).eq("shop_id", shop_id).eq(
            "folder_id", garment_id
        ).execute()
    for slot in plan["inserts"]:
        supabase.table("garment_fabric_slots").insert(
            {"folder_id": garment_id, "shop_id": shop_id, **slot}
        ).execute()


def _record_prompt_version(supabase, shop_id: str, garment_id: str, prompts: dict[str, Any], note: Optional[str]) -> None:
    """Same row saving prompts by hand adds (admin_api.update_folder_prompt); best effort like there."""
    try:
        supabase.table("garment_prompt_versions").insert(
            {
                "garment_type_id": garment_id,
                "shop_id": shop_id,
                "look_prompt": prompts.get("look_prompt"),
                "tryon_prompt": prompts.get("tryon_prompt"),
                "look_tryon_prompt": prompts.get("look_tryon_prompt"),
                "note": _clean_optional(note),
            }
        ).execute()
    except Exception as exc:
        print(f"[prompt_library_api] WARNING: failed to record prompt version for garment {garment_id}: {exc}")


def _apply_version_to_garment(supabase, garment: dict[str, Any], version_row: dict[str, Any]) -> dict[str, Any]:
    """Re-render a library garment's prompts from version_row (its saved fields, unchanged)."""
    set_key = lib.check_set_key(garment.get("prompt_set"))
    version = int(version_row["version"])
    prompts = lib.render_prompts(set_key, version_row, garment)
    rows = _rows(
        supabase.table("garment_types")
        .update({**prompts, "prompt_set_version": version, "updated_at": _utc_now_iso()})
        .eq("id", garment["id"])
        .eq("shop_id", garment["shop_id"])
        .execute()
    )
    _record_prompt_version(
        supabase, str(garment["shop_id"]), str(garment["id"]), prompts, lib.version_note(set_key, version)
    )
    return rows[0] if rows else {**garment, **prompts, "prompt_set_version": version}


def _version_counts(supabase) -> dict[tuple[str, int], int]:
    """(set_key, version) -> how many garments (all shops, archived included) use it."""
    try:
        rows = _rows(
            supabase.table("garment_types")
            .select("id, prompt_set, prompt_set_version")
            .in_("prompt_set", list(lib.SET_KEYS))
            .execute()
        )
    except Exception as exc:
        raise _db_error(exc, "count garments per prompt set") from exc
    counts: dict[tuple[str, int], int] = {}
    for row in rows:
        key = (str(row.get("prompt_set")), int(row.get("prompt_set_version") or 0))
        counts[key] = counts.get(key, 0) + 1
    return counts


def _version_out(row: dict[str, Any], counts: dict[tuple[str, int], int]) -> dict[str, Any]:
    return {**row, "garments_count": counts.get((str(row.get("set_key")), int(row.get("version") or 0)), 0)}


# ---------------------------------------------------------------- prompt library

@router.get("/prompt-library")
def get_prompt_library():
    supabase = get_supabase_admin_client()
    versions = _list_versions(supabase)
    counts = _version_counts(supabase)

    sets = []
    for set_key in lib.SET_KEYS:
        rows = [row for row in versions if row.get("set_key") == set_key]
        sets.append(
            {
                "key": set_key,
                "label": lib.set_label(set_key),
                "blanks": list(lib.SET_BLANKS[set_key]),
                "garments_count": sum(n for (key, _v), n in counts.items() if key == set_key),
                "latest_version": int(rows[-1]["version"]) if rows else None,
                # Newest first, for the history list.
                "versions": [_version_out(row, counts) for row in reversed(rows)],
            }
        )
    return {"sets": sets}


@router.post("/prompt-library/{set_key}/versions")
def create_prompt_set_version(set_key: str, body: PromptSetVersionCreateRequest):
    key = _set_key_or_400(set_key)
    supabase = get_supabase_admin_client()
    row = _insert_version(
        supabase,
        key,
        {"look_prompt": body.look_prompt, "tryon_prompt": body.tryon_prompt, "look_tryon_prompt": body.look_tryon_prompt},
        body.note,
    )
    return _version_out(row, {})


@router.post("/prompt-library/{set_key}/versions/{version}/restore")
def restore_prompt_set_version(set_key: str, version: int, body: Optional[PromptSetRestoreRequest] = None):
    key = _set_key_or_400(set_key)
    supabase = get_supabase_admin_client()
    old = _get_version(supabase, key, version)
    row = _insert_version(
        supabase,
        key,
        {field: old.get(field) for field in lib.PROMPT_FIELDS},
        lib.restore_note(version, body.note if body else None),
    )
    return _version_out(row, {})


@router.post("/prompt-library/{set_key}/move-all")
def move_all_garments_to_version(set_key: str, body: PromptSetMoveAllRequest):
    """Re-render every garment (all shops) on this set from body.version."""
    key = _set_key_or_400(set_key)
    supabase = get_supabase_admin_client()
    target = _get_version(supabase, key, body.version)

    try:
        garments = _rows(supabase.table("garment_types").select("*").eq("prompt_set", key).execute())
    except Exception as exc:
        raise _db_error(exc, "load the garments on this prompt set") from exc

    moved, unchanged, failed = 0, 0, []
    for garment in garments:
        if int(garment.get("prompt_set_version") or 0) == body.version:
            unchanged += 1
            continue
        try:
            _apply_version_to_garment(supabase, garment, target)
            moved += 1
        except Exception as exc:
            reason = str(exc) if isinstance(exc, lib.PromptLibraryError) else "could not save"
            print(f"[prompt_library_api] WARNING: move-all skipped garment {garment.get('id')}: {exc}")
            failed.append(
                {"garment_id": garment.get("id"), "shop_id": garment.get("shop_id"), "name": garment.get("name"), "error": reason}
            )
    return {"set_key": key, "version": body.version, "moved": moved, "unchanged": unchanged, "failed": failed}


@router.post("/prompt-library/parse-skill")
def parse_skill_block(body: ParseSkillRequest):
    return lib.parse_skill_block(body.text)


# ---------------------------------------------------------------- garments

@router.get("/shops/{shop_id}/garments-overview")
def garments_overview(shop_id: str, include_archived: bool = Query(default=False)):
    """Everything the Garments table shows, in a few queries: garments, their fabric slots,
    a hero thumbnail URL and the latest version of each prompt set."""
    supabase = get_supabase_admin_client()
    garments = _rows(
        supabase.table("garment_types").select("*").eq("shop_id", shop_id).order("created_at", desc=True).execute()
    )
    if not include_archived:
        garments = [row for row in garments if row.get("is_active") is not False]

    slots_by_garment: dict[str, list[dict[str, Any]]] = {}
    for slot in _rows(supabase.table("garment_fabric_slots").select("*").eq("shop_id", shop_id).execute()):
        slots_by_garment.setdefault(str(slot.get("folder_id")), []).append(slot)

    hero_ids = [str(row["default_hero_image_id"]) for row in garments if row.get("default_hero_image_id")]
    hero_url_by_id: dict[str, Optional[str]] = {}
    if hero_ids:
        heroes = _rows(
            supabase.table("hero_images").select("id, storage_path").eq("shop_id", shop_id).in_("id", hero_ids).execute()
        )
        original_by_id = {str(h["id"]): str(h["storage_path"]) for h in heroes if h.get("storage_path")}
        thumb_by_id = {hid: hero_thumbnail_path(path) for hid, path in original_by_id.items()}
        signed = sign_existing_paths("hero-images", sorted(set(thumb_by_id.values())))
        # No thumbnail yet (older hero): fall back to the original image.
        without_thumb = sorted({original_by_id[hid] for hid, thumb in thumb_by_id.items() if not signed.get(thumb)})
        signed_originals = sign_existing_paths("hero-images", without_thumb) if without_thumb else {}
        for hid, thumb_path in thumb_by_id.items():
            hero_url_by_id[hid] = signed.get(thumb_path) or signed_originals.get(original_by_id[hid])

    latest_versions: dict[str, Optional[int]] = {key: None for key in lib.SET_KEYS}
    try:
        for row in _list_versions(supabase):
            key = str(row.get("set_key"))
            if key in latest_versions:
                latest_versions[key] = max(latest_versions[key] or 0, int(row.get("version") or 0))
    except HTTPException:
        pass  # SQL not run yet: the table still lists every garment as "Own prompts".

    out = []
    for row in garments:
        slots = sorted(
            slots_by_garment.get(str(row.get("id")), []),
            key=lambda s: (int(s.get("sort_order") or 0), str(s.get("created_at") or "")),
        )
        out.append(
            {
                **row,
                "fabric_slots": slots,
                "hero_thumb_url": hero_url_by_id.get(str(row.get("default_hero_image_id") or "")),
            }
        )
    return {"garments": out, "latest_versions": latest_versions}


@router.get("/shops/{shop_id}/garments/{garment_id}/usage")
def garment_usage(shop_id: str, garment_id: str):
    supabase = get_supabase_admin_client()
    _load_garment(supabase, shop_id, garment_id)
    return {"garment_id": garment_id, "generations": _generation_count(supabase, shop_id, garment_id)}


def _save_garment(shop_id: str, garment_id: Optional[str], body: GarmentSaveRequest) -> dict[str, Any]:
    supabase = get_supabase_admin_client()
    existing = _load_garment(supabase, shop_id, garment_id) if garment_id else None
    if existing is None:
        shop_rows = _rows(supabase.table("shops").select("id").eq("id", shop_id).limit(1).execute())
        if not shop_rows:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Shop not found")

    name = (body.name or "").strip()
    if not name:
        raise _bad_request("Name is required")
    category = (body.category or "").strip().lower()
    if category not in _ALLOWED_CATEGORIES:
        raise _bad_request("category must be one of: men, women, unisex")

    fabric_mode = body.fabric_mode
    if body.mode == "library" and fabric_mode is None:
        raise _bad_request("Pick One fabric or Two fabrics")

    fields: dict[str, Any] = {
        "name": name,
        "category": category,
        "design_text": _clean_optional(body.design_text),
        "keep_text": _clean_optional(body.keep_text),
    }
    desired: Optional[list[dict[str, Any]]] = None
    try:
        if fabric_mode == "two":
            part1 = lib.clean_part_name(body.part1_name, "Part 1")
            part2 = lib.clean_part_name(body.part2_name, "Part 2")
            part2_default = (body.part2_default or "white").strip().lower()
            if part2_default not in lib.PART2_DEFAULTS:
                raise lib.PromptLibraryError("Part 2 default must be white or black")
            desired = lib.desired_slots(part1, part2)
            fields.update(
                part1_name=part1,
                part1_sam_word=_clean_optional(body.part1_sam_word),
                part2_name=part2,
                part2_sam_word=_clean_optional(body.part2_sam_word),
                part2_default=part2_default,
            )
        elif fabric_mode == "one":
            desired = []
            fields.update(part1_name=None, part1_sam_word=None, part2_name=None, part2_sam_word=None, part2_default=None)

        if body.mode == "library":
            set_key = "single" if fabric_mode == "one" else "two"
            version = body.prompt_set_version
            if version is None and existing and existing.get("prompt_set") == set_key:
                version = existing.get("prompt_set_version")
            if version is None:
                latest = _latest_version(supabase, set_key)
                if latest is None:
                    raise lib.PromptLibraryError(
                        f"{lib.set_label(set_key)} has no version yet. Save Version 1 in Prompt library first."
                    )
                version_row = latest
            else:
                version_row = _get_version(supabase, set_key, int(version))
            prompts = lib.render_prompts(set_key, version_row, fields)
            fields.update(prompts, prompt_set=set_key, prompt_set_version=int(version_row["version"]))
            history_note = lib.version_note(set_key, int(version_row["version"]), body.note)
        else:
            look_prompt = (body.look_prompt or "").strip()
            tryon_prompt = (body.tryon_prompt or "").strip()
            if not look_prompt or not tryon_prompt:
                raise lib.PromptLibraryError("Both look and try-on prompts are required.")
            fields.update(
                look_prompt=look_prompt,
                tryon_prompt=tryon_prompt,
                look_tryon_prompt=(body.look_tryon_prompt or "").strip() or None,
                prompt_set=None,
                prompt_set_version=None,
            )
            history_note = body.note

        plan = None
        if desired is not None:
            current_slots = _load_slots(supabase, shop_id, garment_id) if garment_id else []
            has_generations = bool(garment_id) and _generation_count(supabase, shop_id, garment_id) > 0
            if has_generations and bool(current_slots) != bool(desired):
                raise lib.PromptLibraryError(
                    "This garment already has looks, so One fabric / Two fabrics cannot change. "
                    "Create a new garment to change this."
                )
            plan = lib.plan_slot_changes(current_slots, desired, has_generations=has_generations)
    except lib.PromptLibraryError as exc:
        raise _bad_request(str(exc)) from exc

    # Everything is validated; now write. Slots of an existing garment first, so a refused or
    # failed slot change never leaves new prompts on old slots.
    try:
        if garment_id and plan:
            _apply_slot_plan(supabase, shop_id, garment_id, plan)
        if existing:
            rows = _rows(
                supabase.table("garment_types")
                .update({**fields, "updated_at": _utc_now_iso()})
                .eq("id", garment_id)
                .eq("shop_id", shop_id)
                .execute()
            )
        else:
            rows = _rows(
                supabase.table("garment_types")
                .insert({"shop_id": shop_id, "prompt_template": "", "use_custom_prompt": False, **fields})
                .execute()
            )
    except Exception as exc:
        raise _db_error(exc, "save the garment") from exc
    if not rows:
        raise HTTPException(status_code=500, detail="Garment saved but no response was returned")
    saved = rows[0]
    saved_id = str(saved["id"])

    if not garment_id and plan:
        try:
            _apply_slot_plan(supabase, shop_id, saved_id, plan)
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail="Garment saved, but its fabric slots could not be created. Open it and save again.",
            ) from exc

    _record_prompt_version(supabase, shop_id, saved_id, saved, history_note)
    return {**saved, "fabric_slots": _load_slots(supabase, shop_id, saved_id)}


@router.post("/shops/{shop_id}/garments")
def create_garment(shop_id: str, body: GarmentSaveRequest):
    return _save_garment(shop_id, None, body)


@router.put("/shops/{shop_id}/garments/{garment_id}")
def update_garment(shop_id: str, garment_id: str, body: GarmentSaveRequest):
    return _save_garment(shop_id, garment_id, body)


@router.post("/shops/{shop_id}/garments/{garment_id}/move-to-latest")
def move_garment_to_latest(shop_id: str, garment_id: str):
    supabase = get_supabase_admin_client()
    garment = _load_garment(supabase, shop_id, garment_id)
    set_key = garment.get("prompt_set")
    if set_key not in lib.SET_KEYS:
        raise _bad_request("This garment uses its own prompts, not a Prompt library set")
    latest = _latest_version(supabase, set_key)
    if latest is None:
        raise _bad_request(f"{lib.set_label(set_key)} has no version yet")
    if int(garment.get("prompt_set_version") or 0) == int(latest["version"]):
        return {**garment, "fabric_slots": _load_slots(supabase, shop_id, garment_id), "moved": False}
    try:
        saved = _apply_version_to_garment(supabase, garment, latest)
    except lib.PromptLibraryError as exc:
        raise _bad_request(str(exc)) from exc
    return {**saved, "fabric_slots": _load_slots(supabase, shop_id, garment_id), "moved": True}
