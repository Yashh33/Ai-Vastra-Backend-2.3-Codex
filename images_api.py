from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from auth_deps import CurrentShopContext, get_current_shop_context
from supabase_client import get_supabase_admin_client

router = APIRouter(tags=["Images"])


class HeroImageCreateRequest(BaseModel):
    folder_id: str
    storage_path: str = Field(..., min_length=1, max_length=500)
    original_filename: Optional[str] = Field(default=None, max_length=255)
    mime_type: Optional[str] = Field(default=None, max_length=100)
    file_size_bytes: Optional[int] = Field(default=None, ge=0)
    width: Optional[int] = Field(default=None, gt=0)
    height: Optional[int] = Field(default=None, gt=0)


class FabricImageCreateRequest(BaseModel):
    storage_path: str = Field(..., min_length=1, max_length=500)
    original_filename: Optional[str] = Field(default=None, max_length=255)
    mime_type: Optional[str] = Field(default=None, max_length=100)
    file_size_bytes: Optional[int] = Field(default=None, ge=0)
    width: Optional[int] = Field(default=None, gt=0)
    height: Optional[int] = Field(default=None, gt=0)


def _clean_required_text(value: str, field_name: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{field_name} cannot be blank",
        )
    return cleaned


def _clean_optional_text(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned if cleaned else None


def _base_image_payload_dict(
    storage_path: str,
    original_filename: Optional[str],
    mime_type: Optional[str],
    file_size_bytes: Optional[int],
    width: Optional[int],
    height: Optional[int],
) -> dict:
    return {
        "storage_path": _clean_required_text(storage_path, "storage_path"),
        "original_filename": _clean_optional_text(original_filename),
        "mime_type": _clean_optional_text(mime_type),
        "file_size_bytes": file_size_bytes,
        "width": width,
        "height": height,
    }


@router.post("/hero-images")
def create_hero_image_metadata(
    body: HeroImageCreateRequest,
    current: CurrentShopContext = Depends(get_current_shop_context),
):
    supabase = get_supabase_admin_client()

    folder_id = _clean_required_text(body.folder_id, "folder_id")

    # Validate folder belongs to current shop
    try:
        folder_check = (
            supabase.table("hero_folders")
            .select("id")
            .eq("id", folder_id)
            .eq("shop_id", current.shop_id)
            .limit(1)
            .execute()
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to validate folder",
        ) from exc

    folder_rows = getattr(folder_check, "data", None) or []
    if not folder_rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Folder not found",
        )

    payload = {
        "shop_id": current.shop_id,
        "folder_id": folder_id,
        **_base_image_payload_dict(
            storage_path=body.storage_path,
            original_filename=body.original_filename,
            mime_type=body.mime_type,
            file_size_bytes=body.file_size_bytes,
            width=body.width,
            height=body.height,
        ),
    }

    try:
        result = supabase.table("hero_images").insert(payload).execute()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create hero image metadata",
        ) from exc

    rows = getattr(result, "data", None) or []
    if not rows:
        fetch = (
            supabase.table("hero_images")
            .select("*")
            .eq("shop_id", current.shop_id)
            .eq("folder_id", folder_id)
            .eq("storage_path", payload["storage_path"])
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        rows = getattr(fetch, "data", None) or []

    if not rows:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Hero image metadata created but could not fetch response",
        )

    return rows[0]


@router.get("/hero-images")
def list_hero_image_metadata(
    folder_id: Optional[str] = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    current: CurrentShopContext = Depends(get_current_shop_context),
):
    supabase = get_supabase_admin_client()

    query = (
        supabase.table("hero_images")
        .select("*")
        .eq("shop_id", current.shop_id)
        .order("created_at", desc=True)
    )

    if folder_id:
        query = query.eq("folder_id", folder_id.strip())

    query = query.range(offset, offset + limit - 1)

    try:
        result = query.execute()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch hero image metadata",
        ) from exc

    return getattr(result, "data", None) or []


@router.post("/fabric-images")
def create_fabric_image_metadata(
    body: FabricImageCreateRequest,
    current: CurrentShopContext = Depends(get_current_shop_context),
):
    supabase = get_supabase_admin_client()

    payload = {
        "shop_id": current.shop_id,
        **_base_image_payload_dict(
            storage_path=body.storage_path,
            original_filename=body.original_filename,
            mime_type=body.mime_type,
            file_size_bytes=body.file_size_bytes,
            width=body.width,
            height=body.height,
        ),
    }

    try:
        result = supabase.table("fabric_images").insert(payload).execute()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create fabric image metadata",
        ) from exc

    rows = getattr(result, "data", None) or []
    if not rows:
        fetch = (
            supabase.table("fabric_images")
            .select("*")
            .eq("shop_id", current.shop_id)
            .eq("storage_path", payload["storage_path"])
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        rows = getattr(fetch, "data", None) or []

    if not rows:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Fabric image metadata created but could not fetch response",
        )

    return rows[0]


@router.get("/fabric-images")
def list_fabric_image_metadata(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    current: CurrentShopContext = Depends(get_current_shop_context),
):
    supabase = get_supabase_admin_client()

    query = (
        supabase.table("fabric_images")
        .select("*")
        .eq("shop_id", current.shop_id)
        .order("created_at", desc=True)
        .range(offset, offset + limit - 1)
    )

    try:
        result = query.execute()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch fabric image metadata",
        ) from exc

    return getattr(result, "data", None) or []
