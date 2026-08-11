import re
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from pydantic import BaseModel, Field

from admin_deps import verify_admin_secret
from config import get_settings
from supabase_client import get_supabase_admin_client

router = APIRouter(prefix="/admin", tags=["Admin"], dependencies=[Depends(verify_admin_secret)])


class AdminCreateShopRequest(BaseModel):
    shop_name: str = Field(..., min_length=1, max_length=120)
    email: str
    password: str = Field(..., min_length=6, max_length=128)
    opening_credits: int = Field(default=0, ge=0, le=1_000_000)
    carousel_mode_default: bool = False


class AdminUpdateShopRequest(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    header_display_text: Optional[str] = Field(default=None, max_length=120)
    carousel_mode_default: Optional[bool] = None


class AdminResetPasswordRequest(BaseModel):
    password: str = Field(..., min_length=6, max_length=128)


class AdminSuspendShopRequest(BaseModel):
    suspended: bool = True


class AdminGrantCreditsRequest(BaseModel):
    delta: int
    reason: str


class AdminSetWhatsappMultifabricRequest(BaseModel):
    enabled: bool


class AdminCreateFolderRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    prompt_template: str = ""


class AdminUpdateDefaultHeroRequest(BaseModel):
    default_hero_image_id: Optional[str] = Field(...)


class AdminSetWhatsappMenuRequest(BaseModel):
    enabled: bool


_ALLOWED_FABRIC_SLOT_APPLY_TO = {"shirt", "pant", "suit_full_body", "suit_upper", "koti"}


class AdminCreateFabricSlotRequest(BaseModel):
    label: str = Field(..., min_length=1, max_length=80)
    apply_to: str
    sort_order: int = Field(default=0, ge=0, le=100)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_text(value: str, field_name: str) -> str:
    cleaned = (value or "").strip()
    if not cleaned:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{field_name} cannot be blank",
        )
    return cleaned


def _clean_email(value: str) -> str:
    cleaned = (value or "").strip().lower()
    if not cleaned or "@" not in cleaned:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Valid email is required",
        )
    return cleaned


def _extract_user_id(payload: Any) -> Optional[str]:
    if payload is None:
        return None

    if isinstance(payload, dict):
        direct = payload.get("id")
        if direct:
            return str(direct)

        for key in ("user", "data"):
            nested = _extract_user_id(payload.get(key))
            if nested:
                return nested

        return None

    model_dump = getattr(payload, "model_dump", None)
    if callable(model_dump):
        try:
            nested = _extract_user_id(model_dump())
            if nested:
                return nested
        except Exception:
            pass

    direct_id = getattr(payload, "id", None)
    if direct_id:
        return str(direct_id)

    nested_user = _extract_user_id(getattr(payload, "user", None))
    if nested_user:
        return nested_user

    nested_data = _extract_user_id(getattr(payload, "data", None))
    if nested_data:
        return nested_data

    return None


def _extract_user_email(payload: Any) -> Optional[str]:
    if payload is None:
        return None

    if isinstance(payload, dict):
        direct = payload.get("email")
        if direct:
            return str(direct)

        for key in ("user", "data"):
            nested = _extract_user_email(payload.get(key))
            if nested:
                return nested

        return None

    model_dump = getattr(payload, "model_dump", None)
    if callable(model_dump):
        try:
            nested = _extract_user_email(model_dump())
            if nested:
                return nested
        except Exception:
            pass

    direct_email = getattr(payload, "email", None)
    if direct_email:
        return str(direct_email)

    nested_user = _extract_user_email(getattr(payload, "user", None))
    if nested_user:
        return nested_user

    nested_data = _extract_user_email(getattr(payload, "data", None))
    if nested_data:
        return nested_data

    return None


def _get_shop_mapping_for_auth_user(supabase, auth_user_id: str) -> Optional[dict[str, Any]]:
    result = (
        supabase.table("shop_users")
        .select("shop_id, role")
        .eq("auth_user_id", auth_user_id)
        .limit(1)
        .execute()
    )

    rows = getattr(result, "data", None) or []
    return rows[0] if rows else None


def _insert_shop(
    *,
    supabase,
    shop_name: str,
    carousel_mode_default: bool,
) -> str:
    payload = {
        "name": shop_name,
        "carousel_mode_default": carousel_mode_default,
        "is_suspended": False,
    }

    try:
        inserted = supabase.table("shops").insert(payload).execute()
    except Exception as exc:
        message = str(exc).lower()
        if "is_suspended" in message:
            payload.pop("is_suspended", None)
            inserted = supabase.table("shops").insert(payload).execute()
        else:
            raise

    rows = getattr(inserted, "data", None) or []
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create shop record",
        )

    return str(rows[0]["id"])


def _update_shop_with_fallback(supabase, shop_id: str, payload: dict[str, Any]) -> Any:
    try:
        return (
            supabase.table("shops")
            .update(payload)
            .eq("id", shop_id)
            .execute()
        )
    except Exception as exc:
        message = str(exc).lower()
        if "is_suspended" in message and "is_suspended" in payload:
            retry_payload = dict(payload)
            retry_payload.pop("is_suspended", None)
            return (
                supabase.table("shops")
                .update(retry_payload)
                .eq("id", shop_id)
                .execute()
            )
        raise


def _ensure_shop_for_auth_user(
    *,
    supabase,
    auth_user_id: str,
    shop_name: str,
    carousel_mode_default: bool,
) -> tuple[str, str]:
    mapping = _get_shop_mapping_for_auth_user(supabase, auth_user_id)
    if mapping:
        return str(mapping["shop_id"]), str(mapping.get("role") or "owner")

    shop_id = _insert_shop(
        supabase=supabase,
        shop_name=shop_name,
        carousel_mode_default=carousel_mode_default,
    )

    (
        supabase.table("shop_users")
        .insert(
            {
                "shop_id": shop_id,
                "auth_user_id": auth_user_id,
                "role": "owner",
            }
        )
        .execute()
    )

    return shop_id, "owner"


def _get_shop_balance(supabase, shop_id: str) -> int:
    result = (
        supabase.table("credit_ledger")
        .select("balance_after")
        .eq("shop_id", shop_id)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )

    rows = getattr(result, "data", None) or []
    if not rows:
        return 0

    return int(rows[0].get("balance_after") or 0)


def _append_credit_ledger(supabase, shop_id: str, delta: int, reason: str) -> dict[str, int]:
    balance_before = _get_shop_balance(supabase, shop_id)
    balance_after = balance_before + delta

    (
        supabase.table("credit_ledger")
        .insert(
            {
                "shop_id": shop_id,
                "delta": delta,
                "reason": reason,
                "balance_after": balance_after,
            }
        )
        .execute()
    )

    return {"balance_before": balance_before, "balance_after": balance_after}


def _guess_extension(filename: Optional[str], content_type: Optional[str]) -> str:
    if filename and "." in filename:
        ext = filename.rsplit(".", 1)[1].strip().lower()
        if re.fullmatch(r"[a-z0-9]{1,12}", ext):
            return ext

    if content_type and "/" in content_type:
        ext = content_type.split("/", 1)[1].strip().lower()
        ext = ext.replace("jpeg", "jpg")
        ext = re.sub(r"[^a-z0-9]", "", ext)
        if ext:
            return ext[:12]

    return "jpg"


def _upload_bytes_to_bucket(*, supabase, bucket: str, path: str, data: bytes, content_type: str) -> None:
    options = {
        "content-type": content_type,
        "upsert": "true",
    }

    try:
        supabase.storage.from_(bucket).upload(path, data, options)
    except Exception:
        try:
            supabase.storage.from_(bucket).update(path, data, options)
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to upload file to {bucket}",
            ) from exc


def _collect_column_values(
    *,
    supabase,
    table: str,
    column: str,
    shop_id: str,
) -> list[str]:
    result = (
        supabase.table(table)
        .select(column)
        .eq("shop_id", shop_id)
        .limit(5000)
        .execute()
    )

    rows = getattr(result, "data", None) or []
    values: list[str] = []
    for row in rows:
        value = str(row.get(column) or "").strip()
        if value:
            values.append(value)

    return values


def _remove_storage_paths(*, supabase, bucket: str, paths: list[str]) -> list[str]:
    cleaned = [path.strip() for path in paths if path and path.strip()]
    if not cleaned:
        return []

    warnings: list[str] = []
    chunk_size = 100

    for index in range(0, len(cleaned), chunk_size):
        chunk = cleaned[index : index + chunk_size]
        try:
            supabase.storage.from_(bucket).remove(chunk)
        except Exception as exc:
            warnings.append(f"{bucket} cleanup failed for {len(chunk)} object(s): {exc}")

    return warnings


def _get_folder_start_sort_order(*, supabase, shop_id: str, folder_id: str) -> int:
    result = (
        supabase.table("catalog_images")
        .select("sort_order")
        .eq("shop_id", shop_id)
        .eq("folder_id", folder_id)
        .order("sort_order", desc=True)
        .limit(1)
        .execute()
    )
    rows = getattr(result, "data", None) or []
    if not rows:
        return 0
    return int(rows[0].get("sort_order") or 0) + 1


def _delete_generation_fabrics_if_present(*, supabase, shop_id: str) -> None:
    try:
        supabase.table("generation_fabrics").delete().eq("shop_id", shop_id).execute()
    except Exception as exc:
        message = str(exc).lower()
        if "generation_fabrics" in message and "does not exist" in message:
            return
        if "relation" in message and "generation_fabrics" in message:
            return
        raise


@router.get("/session")
def admin_session_ping():
    return {"ok": True}


@router.get("/shops")
def list_shops(
    search: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    supabase = get_supabase_admin_client()

    query = supabase.table("shops").select("*").order("created_at", desc=True)

    if search and search.strip():
        query = query.ilike("name", f"%{search.strip()}%")

    query = query.range(offset, offset + limit - 1)

    try:
        result = query.execute()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch shops",
        ) from exc

    shops = getattr(result, "data", None) or []

    shop_ids = [str(row.get("id") or "") for row in shops if row.get("id")]
    owners_by_shop: dict[str, str] = {}

    if shop_ids:
        try:
            owner_result = (
                supabase.table("shop_users")
                .select("shop_id, auth_user_id, created_at")
                .in_("shop_id", shop_ids)
                .order("created_at", desc=False)
                .execute()
            )
            owner_rows = getattr(owner_result, "data", None) or []
            for row in owner_rows:
                target_shop_id = str(row.get("shop_id") or "")
                auth_user_id = str(row.get("auth_user_id") or "")
                if target_shop_id and auth_user_id and target_shop_id not in owners_by_shop:
                    owners_by_shop[target_shop_id] = auth_user_id
        except Exception:
            owners_by_shop = {}

    phones_by_shop: dict[str, str] = {}
    if shop_ids:
        try:
            phone_result = (
                supabase.table("whatsapp_sessions")
                .select("shop_id, phone_number, created_at")
                .in_("shop_id", shop_ids)
                .order("created_at", desc=False)
                .execute()
            )
            phone_rows = getattr(phone_result, "data", None) or []
            for row in phone_rows:
                target_shop_id = str(row.get("shop_id") or "")
                phone_number = str(row.get("phone_number") or "")
                if target_shop_id and phone_number and target_shop_id not in phones_by_shop:
                    phones_by_shop[target_shop_id] = phone_number
        except Exception:
            phones_by_shop = {}

    emails_by_uid: dict[str, Optional[str]] = {}
    owner_uids = {uid for uid in owners_by_shop.values() if uid}
    for uid in owner_uids:
        try:
            user_result = supabase.auth.admin.get_user_by_id(uid)
            emails_by_uid[uid] = _extract_user_email(user_result)
        except Exception:
            emails_by_uid[uid] = None

    master_shop_id = get_settings().MASTER_SHOP_ID

    enriched = []
    for shop in shops:
        target_shop_id = str(shop.get("id") or "")
        balance = _get_shop_balance(supabase, target_shop_id) if target_shop_id else 0
        owner_auth_user_id = owners_by_shop.get(target_shop_id)
        whatsapp_phone = phones_by_shop.get(target_shop_id)
        shop_name = str(shop.get("name") or "")

        if master_shop_id and target_shop_id == master_shop_id:
            channel = "master"
            whatsapp_phone = None
        elif whatsapp_phone:
            channel = "whatsapp"
        elif shop_name.startswith("WA "):
            channel = "whatsapp"
            whatsapp_phone = shop_name[len("WA "):].strip()
        elif owner_auth_user_id:
            channel = "react"
        else:
            channel = "other"

        enriched.append(
            {
                **shop,
                "is_suspended": bool(shop.get("is_suspended", False)),
                "owner_auth_user_id": owner_auth_user_id,
                "credits_balance": balance,
                "whatsapp_phone": whatsapp_phone,
                "owner_email": emails_by_uid.get(owner_auth_user_id) if owner_auth_user_id else None,
                "channel": channel,
                "status": shop.get("status") or "active",
            }
        )

    return enriched


@router.get("/shops/{shop_id}")
def get_shop(shop_id: str):
    supabase = get_supabase_admin_client()

    result = (
        supabase.table("shops")
        .select("*")
        .eq("id", shop_id)
        .limit(1)
        .execute()
    )

    rows = getattr(result, "data", None) or []
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Shop not found",
        )

    shop = rows[0]
    shop["is_suspended"] = bool(shop.get("is_suspended", False))
    shop["credits_balance"] = _get_shop_balance(supabase, shop_id)

    mapping_result = (
        supabase.table("shop_users")
        .select("auth_user_id")
        .eq("shop_id", shop_id)
        .order("created_at", desc=False)
        .limit(1)
        .execute()
    )
    mapping_rows = getattr(mapping_result, "data", None) or []
    shop["owner_auth_user_id"] = (
        str(mapping_rows[0].get("auth_user_id")) if mapping_rows else None
    )

    return shop


@router.post("/shops")
def create_shop_with_login(body: AdminCreateShopRequest):
    supabase = get_supabase_admin_client()

    shop_name = _clean_text(body.shop_name, "shop_name")
    email = _clean_email(body.email)

    try:
        created_user = supabase.auth.admin.create_user(
            {
                "email": email,
                "password": body.password,
                "email_confirm": True,
                "user_metadata": {
                    "shop_name": shop_name,
                },
            }
        )
    except Exception as exc:
        message = str(exc).lower()
        if "already" in message and "registered" in message:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Email is already registered",
            ) from exc

        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create auth user",
        ) from exc

    auth_user_id = _extract_user_id(created_user)
    if not auth_user_id:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Auth user created but id was not returned",
        )

    try:
        mapping = _get_shop_mapping_for_auth_user(supabase, auth_user_id)
        if mapping:
            shop_id = str(mapping["shop_id"])
            role = str(mapping.get("role") or "owner")
        else:
            shop_id, role = _ensure_shop_for_auth_user(
                supabase=supabase,
                auth_user_id=auth_user_id,
                shop_name=shop_name,
                carousel_mode_default=body.carousel_mode_default,
            )

        _update_shop_with_fallback(
            supabase,
            shop_id,
            {
                "name": shop_name,
                "carousel_mode_default": body.carousel_mode_default,
                "is_suspended": False,
                "updated_at": _utc_now_iso(),
            },
        )

        balance_before = _get_shop_balance(supabase, shop_id)
        balance_after = balance_before

        if body.opening_credits > 0:
            balances = _append_credit_ledger(
                supabase,
                shop_id=shop_id,
                delta=body.opening_credits,
                reason="admin_opening_credits",
            )
            balance_before = balances["balance_before"]
            balance_after = balances["balance_after"]

        return {
            "shop_id": shop_id,
            "shop_name": shop_name,
            "auth_user_id": auth_user_id,
            "email": email,
            "role": role,
            "opening_credits": body.opening_credits,
            "balance_before": balance_before,
            "balance_after": balance_after,
        }
    except Exception:
        try:
            supabase.auth.admin.delete_user(auth_user_id)
        except Exception:
            pass
        raise


@router.patch("/shops/{shop_id}")
def update_shop(shop_id: str, body: AdminUpdateShopRequest):
    supabase = get_supabase_admin_client()

    update_data = body.model_dump(exclude_unset=True)
    if not update_data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provide at least one field to update",
        )

    if "name" in update_data and update_data["name"] is not None:
        update_data["name"] = _clean_text(update_data["name"], "name")
    if "header_display_text" in update_data and update_data["header_display_text"] is not None:
        update_data["header_display_text"] = _clean_text(
            update_data["header_display_text"], "header_display_text"
        )

    update_data["updated_at"] = _utc_now_iso()

    try:
        result = _update_shop_with_fallback(supabase, shop_id, update_data)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update shop",
        ) from exc

    rows = getattr(result, "data", None) or []
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Shop not found",
        )

    return rows[0]


@router.post("/shops/{shop_id}/suspend")
def suspend_shop(shop_id: str, body: AdminSuspendShopRequest):
    supabase = get_supabase_admin_client()

    try:
        result = (
            supabase.table("shops")
            .update(
                {
                    "is_suspended": bool(body.suspended),
                    "updated_at": _utc_now_iso(),
                }
            )
            .eq("id", shop_id)
            .execute()
        )
    except Exception as exc:
        message = str(exc).lower()
        if "is_suspended" in message:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="shops.is_suspended column is missing. Run migration first.",
            ) from exc

        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update shop suspend status",
        ) from exc

    rows = getattr(result, "data", None) or []
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Shop not found",
        )

    return {
        "shop_id": shop_id,
        "is_suspended": bool(rows[0].get("is_suspended", body.suspended)),
    }


@router.post("/shops/{shop_id}/credits")
def grant_shop_credits(shop_id: str, body: AdminGrantCreditsRequest):
    supabase = get_supabase_admin_client()

    reason = body.reason.strip()
    if not reason:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="reason is required",
        )

    delta = body.delta
    if delta == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="delta must be a non-zero integer",
        )

    if abs(delta) > 1_000_000:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="delta must not exceed 1,000,000 in magnitude",
        )

    shop_check = (
        supabase.table("shops")
        .select("id")
        .eq("id", shop_id)
        .limit(1)
        .execute()
    )
    shop_rows = getattr(shop_check, "data", None) or []
    if not shop_rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Shop not found",
        )

    balances = _append_credit_ledger(supabase, shop_id=shop_id, delta=delta, reason=reason)

    try:
        supabase.table("shops").update({"status": "active"}).eq("id", shop_id).execute()
    except Exception:
        pass

    return {
        "shop_id": shop_id,
        "delta": delta,
        "reason": reason,
        "balance_before": balances["balance_before"],
        "balance_after": balances["balance_after"],
    }


@router.post("/shops/{shop_id}/whatsapp-multifabric")
def set_whatsapp_multifabric(shop_id: str, body: AdminSetWhatsappMultifabricRequest):
    supabase = get_supabase_admin_client()

    shop_check = (
        supabase.table("shops")
        .select("id")
        .eq("id", shop_id)
        .limit(1)
        .execute()
    )
    shop_rows = getattr(shop_check, "data", None) or []
    if not shop_rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Shop not found",
        )

    supabase.table("shops").update(
        {"whatsapp_multifabric_enabled": body.enabled}
    ).eq("id", shop_id).execute()

    return {
        "shop_id": shop_id,
        "whatsapp_multifabric_enabled": body.enabled,
    }


@router.delete("/shops/{shop_id}")
def delete_shop(shop_id: str):
    supabase = get_supabase_admin_client()

    shop_result = (
        supabase.table("shops")
        .select("id, name, logo_path")
        .eq("id", shop_id)
        .limit(1)
        .execute()
    )
    shop_rows = getattr(shop_result, "data", None) or []
    if not shop_rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Shop not found",
        )

    shop = shop_rows[0]

    mapping_result = (
        supabase.table("shop_users")
        .select("auth_user_id")
        .eq("shop_id", shop_id)
        .execute()
    )
    mapping_rows = getattr(mapping_result, "data", None) or []
    auth_user_ids = [str(row.get("auth_user_id") or "") for row in mapping_rows if row.get("auth_user_id")]

    hero_paths = _collect_column_values(
        supabase=supabase,
        table="hero_images",
        column="storage_path",
        shop_id=shop_id,
    )
    fabric_paths = _collect_column_values(
        supabase=supabase,
        table="fabric_images",
        column="storage_path",
        shop_id=shop_id,
    )
    output_paths = _collect_column_values(
        supabase=supabase,
        table="generations",
        column="output_path",
        shop_id=shop_id,
    )
    catalog_paths = _collect_column_values(
        supabase=supabase,
        table="catalog_images",
        column="storage_path",
        shop_id=shop_id,
    )
    logo_path = str(shop.get("logo_path") or "").strip()

    try:
        _delete_generation_fabrics_if_present(supabase=supabase, shop_id=shop_id)
        supabase.table("credit_ledger").delete().eq("shop_id", shop_id).execute()
        supabase.table("generations").delete().eq("shop_id", shop_id).execute()
        supabase.table("catalog_images").delete().eq("shop_id", shop_id).execute()
        supabase.table("hero_images").delete().eq("shop_id", shop_id).execute()
        supabase.table("fabric_images").delete().eq("shop_id", shop_id).execute()
        supabase.table("garment_types").delete().eq("shop_id", shop_id).execute()
        supabase.table("shop_users").delete().eq("shop_id", shop_id).execute()
        supabase.table("shops").delete().eq("id", shop_id).execute()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete shop data: {exc}",
        ) from exc

    warnings: list[str] = []
    warnings.extend(_remove_storage_paths(supabase=supabase, bucket="hero-images", paths=hero_paths))
    warnings.extend(_remove_storage_paths(supabase=supabase, bucket="fabric-images", paths=fabric_paths))
    warnings.extend(_remove_storage_paths(supabase=supabase, bucket="generated-outputs", paths=output_paths))
    warnings.extend(_remove_storage_paths(supabase=supabase, bucket="catalog-images", paths=catalog_paths))
    if logo_path:
        warnings.extend(_remove_storage_paths(supabase=supabase, bucket="shop-logos", paths=[logo_path]))

    deleted_auth_user_ids: list[str] = []
    skipped_auth_user_ids: list[str] = []

    for auth_user_id in auth_user_ids:
        try:
            remaining = (
                supabase.table("shop_users")
                .select("id")
                .eq("auth_user_id", auth_user_id)
                .limit(1)
                .execute()
            )
            remaining_rows = getattr(remaining, "data", None) or []
            if remaining_rows:
                skipped_auth_user_ids.append(auth_user_id)
                continue

            supabase.auth.admin.delete_user(auth_user_id)
            deleted_auth_user_ids.append(auth_user_id)
        except Exception as exc:
            skipped_auth_user_ids.append(auth_user_id)
            warnings.append(f"Failed to delete auth user {auth_user_id}: {exc}")

    response = {
        "deleted": True,
        "shop_id": shop_id,
        "shop_name": str(shop.get("name") or ""),
        "deleted_auth_user_ids": deleted_auth_user_ids,
        "skipped_auth_user_ids": skipped_auth_user_ids,
    }
    if warnings:
        response["warnings"] = warnings

    return response


@router.post("/shops/{shop_id}/reset-password")
def reset_shop_password(shop_id: str, body: AdminResetPasswordRequest):
    supabase = get_supabase_admin_client()

    mapping_result = (
        supabase.table("shop_users")
        .select("auth_user_id, created_at")
        .eq("shop_id", shop_id)
        .order("created_at", desc=False)
        .limit(1)
        .execute()
    )

    rows = getattr(mapping_result, "data", None) or []
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Shop login mapping not found",
        )

    auth_user_id = str(rows[0].get("auth_user_id") or "")
    if not auth_user_id:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Invalid auth_user_id mapping",
        )

    try:
        supabase.auth.admin.update_user_by_id(
            auth_user_id,
            {
                "password": body.password,
                "email_confirm": True,
            },
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to reset password",
        ) from exc

    return {
        "shop_id": shop_id,
        "auth_user_id": auth_user_id,
        "password_reset": True,
    }


@router.post("/shops/{shop_id}/logo")
async def upload_shop_logo(shop_id: str, file: UploadFile = File(...)):
    supabase = get_supabase_admin_client()

    content_type = (file.content_type or "").strip().lower()
    if not content_type.startswith("image/"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only image files are allowed for logo",
        )

    file_bytes = await file.read()
    if not file_bytes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Uploaded logo file is empty",
        )

    ext = _guess_extension(file.filename, content_type)
    logo_path = f"{shop_id}/{int(datetime.now().timestamp())}-{uuid4().hex[:8]}.{ext}"

    _upload_bytes_to_bucket(
        supabase=supabase,
        bucket="shop-logos",
        path=logo_path,
        data=file_bytes,
        content_type=content_type,
    )

    try:
        updated = (
            supabase.table("shops")
            .update({"logo_path": logo_path, "updated_at": _utc_now_iso()})
            .eq("id", shop_id)
            .execute()
        )
    except Exception as exc:
        message = str(exc).lower()
        if "logo_path" in message:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="shops.logo_path column is missing. Run the migration first.",
            ) from exc

        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update shop logo path",
        ) from exc

    rows = getattr(updated, "data", None) or []
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Shop not found",
        )

    return {
        "shop_id": shop_id,
        "logo_path": logo_path,
    }


@router.get("/shops/{shop_id}/folders")
def list_shop_folders(shop_id: str):
    supabase = get_supabase_admin_client()

    result = (
        supabase.table("garment_types")
        .select("*")
        .eq("shop_id", shop_id)
        .order("created_at", desc=True)
        .execute()
    )

    return getattr(result, "data", None) or []


@router.post("/shops/{shop_id}/folders")
def create_shop_folder(shop_id: str, body: AdminCreateFolderRequest):
    supabase = get_supabase_admin_client()

    payload = {
        "shop_id": shop_id,
        "name": _clean_text(body.name, "name"),
        "prompt_template": (body.prompt_template or "").strip(),
    }

    try:
        result = supabase.table("garment_types").insert(payload).execute()
    except Exception as exc:
        message = str(exc)
        if "hero_folders_shop_id_name_key" in message:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A folder with this name already exists for this shop",
            ) from exc

        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create folder",
        ) from exc

    rows = getattr(result, "data", None) or []
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Folder created but no response was returned",
        )

    return rows[0]


@router.delete("/shops/{shop_id}/folders/{folder_id}")
async def delete_folder(
    shop_id: str,
    folder_id: str,
    _: None = Depends(verify_admin_secret),
):
    supabase = get_supabase_admin_client()

    result = (
        supabase.table("garment_types")
        .delete()
        .eq("id", folder_id)
        .eq("shop_id", shop_id)
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Folder not found")
    return {"deleted": folder_id}


@router.patch("/shops/{shop_id}/folders/{folder_id}/default-hero")
def update_shop_folder_default_hero(
    shop_id: str,
    folder_id: str,
    body: AdminUpdateDefaultHeroRequest,
):
    supabase = get_supabase_admin_client()

    default_hero_image_id = body.default_hero_image_id
    if default_hero_image_id is not None:
        default_hero_image_id = default_hero_image_id.strip()
        if not default_hero_image_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="default_hero_image_id must be a UUID string or null",
            )

        image_check = (
            supabase.table("hero_images")
            .select("id")
            .eq("id", default_hero_image_id)
            .eq("shop_id", shop_id)
            .limit(1)
            .execute()
        )
        image_rows = getattr(image_check, "data", None) or []
        if not image_rows:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Hero image not found for this shop",
            )

    try:
        result = (
            supabase.table("garment_types")
            .update(
                {
                    "default_hero_image_id": default_hero_image_id,
                    "updated_at": _utc_now_iso(),
                }
            )
            .eq("id", folder_id)
            .eq("shop_id", shop_id)
            .execute()
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update default hero image",
        ) from exc

    rows = getattr(result, "data", None) or []
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Folder not found for this shop",
        )

    return rows[0]


@router.post("/shops/{shop_id}/folders/{folder_id}/whatsapp-menu")
def set_folder_whatsapp_menu(
    shop_id: str,
    folder_id: str,
    body: AdminSetWhatsappMenuRequest,
):
    supabase = get_supabase_admin_client()

    try:
        result = (
            supabase.table("garment_types")
            .update(
                {
                    "show_in_whatsapp_menu": body.enabled,
                    "updated_at": _utc_now_iso(),
                }
            )
            .eq("id", folder_id)
            .eq("shop_id", shop_id)
            .execute()
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update WhatsApp menu flag",
        ) from exc

    rows = getattr(result, "data", None) or []
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Folder not found for this shop",
        )

    return {
        "folder_id": folder_id,
        "show_in_whatsapp_menu": body.enabled,
    }


@router.post("/shops/{shop_id}/folders/{folder_id}/fabric-slots")
def create_folder_fabric_slot(
    shop_id: str,
    folder_id: str,
    body: AdminCreateFabricSlotRequest,
):
    supabase = get_supabase_admin_client()

    folder_check = (
        supabase.table("garment_types")
        .select("id")
        .eq("id", folder_id)
        .eq("shop_id", shop_id)
        .limit(1)
        .execute()
    )
    folder_rows = getattr(folder_check, "data", None) or []
    if not folder_rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Folder not found",
        )

    apply_to = (body.apply_to or "").strip().lower()
    if apply_to not in _ALLOWED_FABRIC_SLOT_APPLY_TO:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="apply_to must be one of: shirt, pant, suit_full_body, suit_upper, koti",
        )

    existing_result = (
        supabase.table("garment_fabric_slots")
        .select("apply_to")
        .eq("folder_id", folder_id)
        .eq("shop_id", shop_id)
        .execute()
    )
    existing_rows = getattr(existing_result, "data", None) or []

    if len(existing_rows) >= 4:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A garment type can have at most 4 fabric slots",
        )

    existing_has_suit_full_body = any(
        row.get("apply_to") == "suit_full_body" for row in existing_rows
    )
    if (apply_to == "suit_full_body" and existing_rows) or existing_has_suit_full_body:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="suit_full_body cannot be combined with other fabric slots",
        )

    payload = {
        "folder_id": folder_id,
        "shop_id": shop_id,
        "label": _clean_text(body.label, "label"),
        "apply_to": apply_to,
        "sort_order": body.sort_order,
    }

    try:
        result = supabase.table("garment_fabric_slots").insert(payload).execute()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create fabric slot",
        ) from exc

    rows = getattr(result, "data", None) or []
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create fabric slot",
        )

    return rows[0]


@router.get("/shops/{shop_id}/folders/{folder_id}/fabric-slots")
def list_folder_fabric_slots(shop_id: str, folder_id: str):
    supabase = get_supabase_admin_client()

    folder_check = (
        supabase.table("garment_types")
        .select("id")
        .eq("id", folder_id)
        .eq("shop_id", shop_id)
        .limit(1)
        .execute()
    )
    folder_rows = getattr(folder_check, "data", None) or []
    if not folder_rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Folder not found",
        )

    result = (
        supabase.table("garment_fabric_slots")
        .select("*")
        .eq("folder_id", folder_id)
        .eq("shop_id", shop_id)
        .order("sort_order", desc=False)
        .order("created_at", desc=False)
        .execute()
    )

    return getattr(result, "data", None) or []


@router.delete("/shops/{shop_id}/folders/{folder_id}/fabric-slots/{slot_id}")
def delete_folder_fabric_slot(shop_id: str, folder_id: str, slot_id: str):
    supabase = get_supabase_admin_client()

    result = (
        supabase.table("garment_fabric_slots")
        .delete()
        .eq("id", slot_id)
        .eq("shop_id", shop_id)
        .eq("folder_id", folder_id)
        .execute()
    )
    if not getattr(result, "data", None):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Fabric slot not found",
        )
    return {"deleted": True}


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
        cleaned_path = (storage_path or "").strip()
        if not cleaned_path:
            return None

        signed = supabase.storage.from_("hero-images").create_signed_url(
            cleaned_path,
            3600,
        )

        signed_url = _extract_signed_url(signed)
        if not signed_url:
            return None

        if signed_url.startswith("/"):
            signed_url = f"{get_settings().SUPABASE_URL}{signed_url}"

        return signed_url
    except Exception:
        return None


@router.get("/shops/{shop_id}/hero-images")
def list_shop_hero_images(
    shop_id: str,
    folder_id: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    supabase = get_supabase_admin_client()

    query = (
        supabase.table("hero_images")
        .select("*")
        .eq("shop_id", shop_id)
        .order("created_at", desc=True)
    )

    if folder_id and folder_id.strip():
        query = query.eq("folder_id", folder_id.strip())

    query = query.range(offset, offset + limit - 1)

    result = query.execute()
    rows = getattr(result, "data", None) or []

    for row in rows:
        row["signed_url"] = _create_hero_image_signed_url(supabase, row.get("storage_path"))

    return rows


@router.post("/shops/{shop_id}/hero-images/upload")
async def upload_shop_hero_image(
    shop_id: str,
    folder_id: str = Form(...),
    file: UploadFile = File(...),
):
    supabase = get_supabase_admin_client()

    normalized_folder_id = _clean_text(folder_id, "folder_id")
    content_type = (file.content_type or "").strip().lower() or "image/jpeg"

    if not content_type.startswith("image/"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only image files are allowed",
        )

    folder_check = (
        supabase.table("garment_types")
        .select("id")
        .eq("id", normalized_folder_id)
        .eq("shop_id", shop_id)
        .limit(1)
        .execute()
    )
    folder_rows = getattr(folder_check, "data", None) or []
    if not folder_rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Folder not found for this shop",
        )

    data = await file.read()
    if not data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Uploaded image file is empty",
        )

    ext = _guess_extension(file.filename, content_type)
    filename = f"{int(datetime.now().timestamp())}-{uuid4().hex[:8]}.{ext}"
    storage_path = f"{shop_id}/{normalized_folder_id}/{filename}"

    _upload_bytes_to_bucket(
        supabase=supabase,
        bucket="hero-images",
        path=storage_path,
        data=data,
        content_type=content_type,
    )

    metadata_payload = {
        "shop_id": shop_id,
        "folder_id": normalized_folder_id,
        "storage_path": storage_path,
        "original_filename": file.filename,
        "mime_type": content_type,
        "file_size_bytes": len(data),
        "width": None,
        "height": None,
    }

    result = supabase.table("hero_images").insert(metadata_payload).execute()
    rows = getattr(result, "data", None) or []
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Hero image metadata created but no response returned",
        )

    return rows[0]


@router.get("/shops/{shop_id}/catalog-images")
def list_shop_catalog_images(
    shop_id: str,
    folder_id: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    supabase = get_supabase_admin_client()

    query = (
        supabase.table("catalog_images")
        .select("*")
        .eq("shop_id", shop_id)
        .order("sort_order", desc=False)
        .order("created_at", desc=False)
    )

    if folder_id and folder_id.strip():
        query = query.eq("folder_id", folder_id.strip())

    query = query.range(offset, offset + limit - 1)

    result = query.execute()
    return getattr(result, "data", None) or []


@router.post("/shops/{shop_id}/catalog-images/upload-bulk")
async def upload_shop_catalog_images_bulk(
    shop_id: str,
    folder_id: str = Form(...),
    files: list[UploadFile] = File(...),
):
    supabase = get_supabase_admin_client()

    normalized_folder_id = _clean_text(folder_id, "folder_id")

    folder_check = (
        supabase.table("garment_types")
        .select("id")
        .eq("id", normalized_folder_id)
        .eq("shop_id", shop_id)
        .limit(1)
        .execute()
    )
    folder_rows = getattr(folder_check, "data", None) or []
    if not folder_rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Folder not found for this shop",
        )

    active_files = [file for file in files if file and (file.filename or "").strip()]
    if not active_files:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="At least one image file is required",
        )

    start_sort_order = _get_folder_start_sort_order(
        supabase=supabase,
        shop_id=shop_id,
        folder_id=normalized_folder_id,
    )

    inserted_rows: list[dict[str, Any]] = []
    sort_order = start_sort_order

    for file in active_files:
        content_type = (file.content_type or "").strip().lower() or "image/jpeg"
        if not content_type.startswith("image/"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Only image files are allowed ({file.filename})",
            )

        data = await file.read()
        if not data:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Uploaded image file is empty ({file.filename})",
            )

        ext = _guess_extension(file.filename, content_type)
        filename = f"{int(datetime.now().timestamp())}-{uuid4().hex[:8]}.{ext}"
        storage_path = f"{shop_id}/{normalized_folder_id}/{filename}"

        _upload_bytes_to_bucket(
            supabase=supabase,
            bucket="catalog-images",
            path=storage_path,
            data=data,
            content_type=content_type,
        )

        metadata_payload = {
            "shop_id": shop_id,
            "folder_id": normalized_folder_id,
            "storage_path": storage_path,
            "original_filename": file.filename,
            "mime_type": content_type,
            "file_size_bytes": len(data),
            "width": None,
            "height": None,
            "sort_order": sort_order,
            "is_active": True,
        }

        result = supabase.table("catalog_images").insert(metadata_payload).execute()
        rows = getattr(result, "data", None) or []
        if not rows:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Catalog image metadata created but no response returned ({file.filename})",
            )

        inserted_rows.append(rows[0])
        sort_order += 1

    return {
        "shop_id": shop_id,
        "folder_id": normalized_folder_id,
        "uploaded_count": len(inserted_rows),
        "items": inserted_rows,
    }

