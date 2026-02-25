from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from auth_deps import CurrentShopContext, get_current_shop_context
from supabase_client import get_supabase_admin_client

router = APIRouter(prefix="/folders", tags=["Folders"])


class FolderCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    prompt_template: str = ""


class FolderUpdateRequest(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    prompt_template: Optional[str] = None
    is_active: Optional[bool] = None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_folder_name(name: str) -> str:
    normalized = name.strip()
    if not normalized:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Folder name cannot be blank",
        )
    return normalized


@router.post("")
def create_folder(
    body: FolderCreateRequest,
    current: CurrentShopContext = Depends(get_current_shop_context),
):
    supabase = get_supabase_admin_client()

    payload = {
        "shop_id": current.shop_id,
        "name": _normalize_folder_name(body.name),
        "prompt_template": body.prompt_template or "",
    }

    try:
        result = (
            supabase.table("hero_folders")
            .insert(payload)
            .execute()
        )
    except Exception as exc:
        if "hero_folders_shop_id_name_key" in str(exc):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A folder with this name already exists in your shop",
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create folder",
        ) from exc

    rows = getattr(result, "data", None) or []
    if not rows:
        # Fallback fetch (in case insert returns no representation)
        fetch = (
            supabase.table("hero_folders")
            .select("*")
            .eq("shop_id", current.shop_id)
            .eq("name", payload["name"])
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        rows = getattr(fetch, "data", None) or []

    if not rows:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Folder created but could not fetch response",
        )

    return rows[0]


@router.get("")
def list_folders(
    include_inactive: bool = Query(default=True),
    current: CurrentShopContext = Depends(get_current_shop_context),
):
    supabase = get_supabase_admin_client()

    query = (
        supabase.table("hero_folders")
        .select("*")
        .eq("shop_id", current.shop_id)
        .order("created_at", desc=True)
    )

    if not include_inactive:
        query = query.eq("is_active", True)

    try:
        result = query.execute()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch folders",
        ) from exc

    return getattr(result, "data", None) or []


@router.patch("/{folder_id}")
def update_folder(
    folder_id: str,
    body: FolderUpdateRequest,
    current: CurrentShopContext = Depends(get_current_shop_context),
):
    supabase = get_supabase_admin_client()

    update_data = body.model_dump(exclude_unset=True)

    if not update_data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provide at least one field to update",
        )

    if "name" in update_data and update_data["name"] is not None:
        update_data["name"] = _normalize_folder_name(update_data["name"])

    update_data["updated_at"] = _utc_now_iso()

    # Confirm folder belongs to this shop
    existing = (
        supabase.table("hero_folders")
        .select("id")
        .eq("id", folder_id)
        .eq("shop_id", current.shop_id)
        .limit(1)
        .execute()
    )
    if not (getattr(existing, "data", None) or []):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Folder not found",
        )

    try:
        result = (
            supabase.table("hero_folders")
            .update(update_data)
            .eq("id", folder_id)
            .eq("shop_id", current.shop_id)
            .execute()
        )
    except Exception as exc:
        if "hero_folders_shop_id_name_key" in str(exc):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A folder with this name already exists in your shop",
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update folder",
        ) from exc

    rows = getattr(result, "data", None) or []
    if not rows:
        fetch = (
            supabase.table("hero_folders")
            .select("*")
            .eq("id", folder_id)
            .eq("shop_id", current.shop_id)
            .limit(1)
            .execute()
        )
        rows = getattr(fetch, "data", None) or []

    if not rows:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Folder updated but could not fetch response",
        )

    return rows[0]


@router.delete("/{folder_id}")
def delete_folder(
    folder_id: str,
    current: CurrentShopContext = Depends(get_current_shop_context),
):
    supabase = get_supabase_admin_client()

    # Confirm folder belongs to this shop
    existing = (
        supabase.table("hero_folders")
        .select("id, name")
        .eq("id", folder_id)
        .eq("shop_id", current.shop_id)
        .limit(1)
        .execute()
    )
    rows = getattr(existing, "data", None) or []
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Folder not found",
        )

    folder = rows[0]

    try:
        (
            supabase.table("hero_folders")
            .delete()
            .eq("id", folder_id)
            .eq("shop_id", current.shop_id)
            .execute()
        )
    except Exception as exc:
        # This may happen later if generations reference the folder (on delete restrict)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Folder could not be deleted (it may be referenced by other records)",
        ) from exc

    return {
        "deleted": True,
        "id": folder["id"],
        "name": folder["name"],
    }
