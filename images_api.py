from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from auth_deps import CurrentShopContext, get_current_shop_context
from config import get_settings
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


class CatalogImageCreateRequest(BaseModel):
    folder_id: str
    storage_path: str = Field(..., min_length=1, max_length=500)
    original_filename: Optional[str] = Field(default=None, max_length=255)
    mime_type: Optional[str] = Field(default=None, max_length=100)
    file_size_bytes: Optional[int] = Field(default=None, ge=0)
    width: Optional[int] = Field(default=None, gt=0)
    height: Optional[int] = Field(default=None, gt=0)
    sort_order: int = Field(default=0, ge=0)
    is_active: bool = True


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


def _extract_signed_url(signed_payload: object) -> Optional[str]:
    if isinstance(signed_payload, dict):
        return (
            signed_payload.get("signedURL")
            or signed_payload.get("signedUrl")
            or signed_payload.get("signed_url")
            or signed_payload.get("data", {}).get("signedURL")
        )
    return None


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
            supabase.table("garment_types")
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


@router.post("/catalog-images")
def create_catalog_image_metadata(
    body: CatalogImageCreateRequest,
    current: CurrentShopContext = Depends(get_current_shop_context),
):
    supabase = get_supabase_admin_client()

    folder_id = _clean_required_text(body.folder_id, "folder_id")

    try:
        folder_check = (
            supabase.table("garment_types")
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
        "sort_order": body.sort_order,
        "is_active": bool(body.is_active),
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
        result = supabase.table("catalog_images").insert(payload).execute()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create catalog image metadata",
        ) from exc

    rows = getattr(result, "data", None) or []
    if not rows:
        fetch = (
            supabase.table("catalog_images")
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
            detail="Catalog image metadata created but could not fetch response",
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


@router.get("/catalog-images")
def list_catalog_image_metadata(
    folder_id: Optional[str] = Query(default=None),
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current: CurrentShopContext = Depends(get_current_shop_context),
):
    supabase = get_supabase_admin_client()

    query = (
        supabase.table("catalog_images")
        .select("*")
        .eq("shop_id", current.shop_id)
        .eq("is_active", True)
        .order("sort_order", desc=False)
        .order("created_at", desc=False)
    )

    if folder_id:
        query = query.eq("folder_id", folder_id.strip())

    query = query.range(offset, offset + limit - 1)

    try:
        result = query.execute()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch catalog image metadata",
        ) from exc

    return getattr(result, "data", None) or []


@router.get("/catalog-images/{catalog_image_id}/download-url")
def get_catalog_image_download_url(
    catalog_image_id: str,
    expires_in_seconds: int = Query(default=300, ge=60, le=3600),
    current: CurrentShopContext = Depends(get_current_shop_context),
):
    supabase = get_supabase_admin_client()
    settings = get_settings()

    image_id = _clean_required_text(catalog_image_id, "catalog_image_id")

    try:
        result = (
            supabase.table("catalog_images")
            .select("id, storage_path, is_active")
            .eq("id", image_id)
            .eq("shop_id", current.shop_id)
            .limit(1)
            .execute()
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch catalog image",
        ) from exc

    rows = getattr(result, "data", None) or []
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Catalog image not found",
        )

    row = rows[0]
    storage_path = str(row.get("storage_path") or "").strip()
    if not storage_path:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Catalog image storage path is missing",
        )

    if row.get("is_active") is False:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Catalog image is inactive",
        )

    try:
        signed = (
            supabase.storage.from_("catalog-images")
            .create_signed_url(storage_path, expires_in_seconds)
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create signed download URL",
        ) from exc

    signed_url = _extract_signed_url(signed)
    if not signed_url:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Storage signed URL response was empty",
        )

    if signed_url.startswith("/"):
        signed_url = f"{settings.SUPABASE_URL}{signed_url}"

    return {
        "catalog_image_id": row["id"],
        "download_url": signed_url,
        "expires_in_seconds": expires_in_seconds,
    }


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


@router.get("/fabric-images/{fabric_image_id}")
def get_fabric_image_metadata(
    fabric_image_id: str,
    current: CurrentShopContext = Depends(get_current_shop_context),
):
    supabase = get_supabase_admin_client()

    image_id = _clean_required_text(fabric_image_id, "fabric_image_id")

    try:
        result = (
            supabase.table("fabric_images")
            .select("*")
            .eq("id", image_id)
            .eq("shop_id", current.shop_id)
            .limit(1)
            .execute()
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch fabric image metadata",
        ) from exc

    rows = getattr(result, "data", None) or []
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Fabric image not found",
        )

    return rows[0]
