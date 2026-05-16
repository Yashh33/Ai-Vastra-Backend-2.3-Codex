from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status

from auth_deps import CurrentShopContext, get_current_shop_context
from config import get_settings
from supabase_client import get_supabase_admin_client

router = APIRouter(prefix="/garment-types", tags=["Garment Types"])


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
    cleaned_path = storage_path.strip()
    if not cleaned_path:
        return None

    try:
        signed = supabase.storage.from_("hero-images").create_signed_url(
            cleaned_path,
            3600,
        )
    except Exception:
        return None

    signed_url = _extract_signed_url(signed)
    if not signed_url:
        return None

    if signed_url.startswith("/"):
        signed_url = f"{get_settings().SUPABASE_URL}{signed_url}"

    return signed_url


@router.get("")
def list_garment_types(
    current: CurrentShopContext = Depends(get_current_shop_context),
):
    supabase = get_supabase_admin_client()

    try:
        folder_result = (
            supabase.table("hero_folders")
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
    items = []

    for folder in folders:
        default_hero_image_id = folder.get("default_hero_image_id")
        hero_image_signed_url = None

        if default_hero_image_id:
            try:
                image_result = (
                    supabase.table("hero_images")
                    .select("id, storage_path")
                    .eq("id", default_hero_image_id)
                    .eq("shop_id", current.shop_id)
                    .limit(1)
                    .execute()
                )
                image_rows = getattr(image_result, "data", None) or []
                if image_rows:
                    storage_path = str(image_rows[0].get("storage_path") or "")
                    hero_image_signed_url = _create_hero_image_signed_url(
                        supabase,
                        storage_path,
                    )
            except Exception:
                hero_image_signed_url = None

        items.append(
            {
                "id": folder.get("id"),
                "name": folder.get("name"),
                "prompt_template": folder.get("prompt_template"),
                "default_hero_image_id": default_hero_image_id or None,
                "hero_image_signed_url": hero_image_signed_url,
            }
        )

    return items
