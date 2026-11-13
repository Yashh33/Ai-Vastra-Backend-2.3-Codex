import threading
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status

from cachetools import TTLCache

from auth_deps import CurrentShopContext, get_current_shop_context
from config import get_settings
from image_thumbs import hero_thumbnail_path, sign_existing_paths, store_hero_thumbnail
from supabase_client import get_supabase_admin_client

router = APIRouter(prefix="/garment-types", tags=["Garment Types"])

_MAX_LAZY_HERO_THUMBS_PER_REQUEST = 10
# Heroes whose thumbnail could not be created (e.g. original file deleted):
# don't retry them on every request.
_hero_thumb_failures: TTLCache = TTLCache(maxsize=2000, ttl=600)

# Phones cache pictures by URL: hand out the SAME signed link for a while instead of a new one
# on every call. Links are signed for 6 hours and reused for 4, so a reused link always has
# at least 2 hours left. Keyed by storage path (paths include the shop id).
_SIGNED_URL_SECONDS = 6 * 60 * 60
_SIGNED_URL_REUSE_SECONDS = 4 * 60 * 60
_hero_image_url_cache: TTLCache = TTLCache(maxsize=5000, ttl=_SIGNED_URL_REUSE_SECONDS)
_hero_thumb_url_cache: TTLCache = TTLCache(maxsize=5000, ttl=_SIGNED_URL_REUSE_SECONDS)
_url_cache_lock = threading.Lock()


def _reuse_signed_urls(cache: TTLCache, storage_paths: list[str], sign) -> dict[str, Optional[str]]:
    """storage path -> signed URL. Paths with a remembered link get it back; the others are
    signed with sign(paths) and remembered (only real links; None is never remembered)."""
    result: dict[str, Optional[str]] = {}
    missing: list[str] = []
    with _url_cache_lock:
        for path in storage_paths:
            url = cache.get(path)
            if url:
                result[path] = url
            else:
                missing.append(path)
    if not missing:
        return result

    fresh = sign(missing) or {}
    with _url_cache_lock:
        for path in missing:
            url = fresh.get(path)
            result[path] = url
            if url:
                cache[path] = url
    return result


def _hero_thumb_signed_urls(supabase, storage_paths: list[str]) -> dict[str, Optional[str]]:
    """Hero storage path -> signed display-thumbnail URL (None if unavailable).

    Missing thumbnails are created from the original, at most
    _MAX_LAZY_HERO_THUMBS_PER_REQUEST per call. Never raises.
    """
    try:
        thumb_path_by_storage_path = {path: hero_thumbnail_path(path) for path in storage_paths}
        url_by_thumb_path = sign_existing_paths(
            "hero-images", list(thumb_path_by_storage_path.values()), expires_in=_SIGNED_URL_SECONDS
        )

        created: list[str] = []
        for storage_path, thumb_path in thumb_path_by_storage_path.items():
            if len(created) >= _MAX_LAZY_HERO_THUMBS_PER_REQUEST:
                break
            if url_by_thumb_path.get(thumb_path) or storage_path in _hero_thumb_failures:
                continue
            try:
                created.append(store_hero_thumbnail(supabase, storage_path))
                print(f"[garment_types_api] created hero thumbnail: {thumb_path}")
            except Exception as exc:
                _hero_thumb_failures[storage_path] = True
                print(
                    f"[garment_types_api] WARNING: could not create hero thumbnail "
                    f"for {storage_path}: {exc}"
                )

        if created:
            url_by_thumb_path.update(
                sign_existing_paths("hero-images", created, expires_in=_SIGNED_URL_SECONDS)
            )

        return {
            storage_path: url_by_thumb_path.get(thumb_path)
            for storage_path, thumb_path in thumb_path_by_storage_path.items()
        }
    except Exception as exc:
        print(f"[garment_types_api] WARNING: hero thumbnail URLs unavailable: {exc}")
        return {}


def _extract_signed_url(signed_payload: object) -> Optional[str]:
    if isinstance(signed_payload, dict):
        data = signed_payload.get("data")
        nested = data if isinstance(data, dict) else {}
        return (
            signed_payload.get("signedURL")
            or signed_payload.get("signedUrl")
            or signed_payload.get("signed_url")
            or nested.get("signedURL")
            or nested.get("signedUrl")
            or nested.get("signed_url")
        )
    return None


def _create_hero_image_signed_url(supabase, storage_path: str) -> Optional[str]:
    try:
        cleaned_path = storage_path.strip()
        if not cleaned_path:
            return None

        signed = supabase.storage.from_("hero-images").create_signed_url(
            cleaned_path,
            _SIGNED_URL_SECONDS,
        )

        signed_url = _extract_signed_url(signed)
        if not signed_url:
            return None

        if signed_url.startswith("/"):
            signed_url = f"{get_settings().SUPABASE_URL}{signed_url}"

        return signed_url
    except Exception:
        return None


def _create_hero_image_signed_urls_batch(
    supabase, storage_paths: list[str]
) -> dict[str, Optional[str]]:
    """Batch-sign storage paths. Returns path -> signed_url (None on per-item failure)."""
    result: dict[str, Optional[str]] = {path: None for path in storage_paths}
    if not storage_paths:
        return result

    try:
        signed_items = supabase.storage.from_("hero-images").create_signed_urls(
            storage_paths,
            _SIGNED_URL_SECONDS,
        )
    except Exception:
        # Fall back to per-path signing so the endpoint never regresses to broken.
        for path in storage_paths:
            result[path] = _create_hero_image_signed_url(supabase, path)
        return result

    if not isinstance(signed_items, list):
        for path in storage_paths:
            result[path] = _create_hero_image_signed_url(supabase, path)
        return result

    settings = get_settings()
    for item in signed_items:
        if not isinstance(item, dict) or item.get("error"):
            continue

        path = item.get("path") or item.get("Path")
        signed_url = _extract_signed_url(item)
        if not path or not signed_url:
            continue

        if signed_url.startswith("/"):
            signed_url = f"{settings.SUPABASE_URL}{signed_url}"

        result[path] = signed_url

    return result


@router.get("")
def list_garment_types(
    current: CurrentShopContext = Depends(get_current_shop_context),
):
    supabase = get_supabase_admin_client()

    try:
        folder_result = (
            supabase.table("garment_types")
            .select("id, name, prompt_template, default_hero_image_id")
            .eq("shop_id", current.shop_id)
            .eq("is_active", True)
            .order("name", desc=False)
            .execute()
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch garment types",
        ) from exc

    folders = getattr(folder_result, "data", None) or []

    hero_image_ids = sorted(
        {
            str(folder.get("default_hero_image_id"))
            for folder in folders
            if folder.get("default_hero_image_id")
        }
    )

    storage_path_by_hero_image_id: dict[str, str] = {}
    if hero_image_ids:
        try:
            image_result = (
                supabase.table("hero_images")
                .select("id, storage_path")
                .in_("id", hero_image_ids)
                .eq("shop_id", current.shop_id)
                .execute()
            )
            image_rows = getattr(image_result, "data", None) or []
            for image_row in image_rows:
                image_id = str(image_row.get("id") or "")
                storage_path = str(image_row.get("storage_path") or "")
                if image_id and storage_path:
                    storage_path_by_hero_image_id[image_id] = storage_path
        except Exception:
            storage_path_by_hero_image_id = {}

    storage_paths = sorted(set(storage_path_by_hero_image_id.values()))
    try:
        signed_url_by_storage_path = _reuse_signed_urls(
            _hero_image_url_cache,
            storage_paths,
            lambda paths: _create_hero_image_signed_urls_batch(supabase, paths),
        )
    except Exception as exc:
        print(f"[garment_types_api] failed to sign hero image URLs: {exc}")
        signed_url_by_storage_path = {}

    try:
        thumb_url_by_storage_path = _reuse_signed_urls(
            _hero_thumb_url_cache,
            storage_paths,
            lambda paths: _hero_thumb_signed_urls(supabase, paths),
        )
    except Exception as exc:
        print(f"[garment_types_api] failed to sign hero thumbnail URLs: {exc}")
        thumb_url_by_storage_path = {}

    all_folder_ids = [folder.get("id") for folder in folders if folder.get("id")]

    fabric_slots_by_folder_id: dict[str, list[dict]] = {}
    if all_folder_ids:
        try:
            fabric_slot_result = (
                supabase.table("garment_fabric_slots")
                .select("id, label, apply_to, sort_order, folder_id")
                .eq("shop_id", current.shop_id)
                .in_("folder_id", all_folder_ids)
                .order("sort_order", desc=False)
                .execute()
            )
            fabric_slot_rows = getattr(fabric_slot_result, "data", None) or []
        except Exception:
            fabric_slot_rows = []

        for slot_row in fabric_slot_rows:
            folder_id = str(slot_row.get("folder_id") or "")
            if not folder_id:
                continue
            fabric_slots_by_folder_id.setdefault(folder_id, []).append(
                {
                    "id": slot_row.get("id"),
                    "label": slot_row.get("label"),
                    "apply_to": slot_row.get("apply_to"),
                    "sort_order": slot_row.get("sort_order"),
                }
            )

    items = []
    for folder in folders:
        default_hero_image_id = folder.get("default_hero_image_id")
        hero_image_signed_url = None
        hero_thumb_signed_url = None

        if default_hero_image_id:
            storage_path = storage_path_by_hero_image_id.get(
                str(default_hero_image_id)
            )
            if storage_path:
                hero_image_signed_url = signed_url_by_storage_path.get(storage_path)
                hero_thumb_signed_url = thumb_url_by_storage_path.get(storage_path)

        items.append(
            {
                "id": folder.get("id"),
                "name": folder.get("name"),
                "prompt_template": folder.get("prompt_template"),
                "default_hero_image_id": default_hero_image_id or None,
                "hero_image_signed_url": hero_image_signed_url,
                "hero_thumb_signed_url": hero_thumb_signed_url,
                "fabric_slots": fabric_slots_by_folder_id.get(
                    str(folder.get("id")), []
                ),
            }
        )

    return items
