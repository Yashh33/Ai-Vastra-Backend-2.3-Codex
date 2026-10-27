import base64
import io
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Optional, Tuple
from uuid import uuid4

import anyio.to_thread
from cachetools import TTLCache
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)
from pydantic import BaseModel, Field

import attempt_log
import default_fabrics
from auth_deps import CurrentShopContext, get_current_shop_context
from config import get_settings
from prompting import DEFAULT_TRYON_PROMPT, fill_prompt_placeholders
from supabase_client import get_supabase_admin_client

from google import genai
from google.genai import types

router = APIRouter(prefix="/tryon", tags=["TryOn"])

_MAX_INPUT_IMAGE_DIMENSION = 1536

_CONSENT_REJECTED_DETAIL = (
    "Customer consent must be confirmed before processing a try-on request"
)


class TryOnRequest(BaseModel):
    generation_id: str = Field(..., min_length=1)
    customer_photo_b64: str = Field(..., min_length=1)
    customer_photo_mime: str = Field(
        default="image/jpeg",
        min_length=1
    )
    consent_confirmed: bool = Field(
        default=False,
        description="Must be true â€” confirms tailor obtained customer consent before photo was taken"
    )


class TryOnQuickRequest(BaseModel):
    fabric_image_id: str = Field(..., min_length=1)
    fabric_image_ids: Optional[list[str]] = Field(default=None)
    # Optional slots left empty: {"<slot apply_to>": "white" | "black"} (missing -> white).
    default_fabrics: Optional[dict[str, Any]] = None
    folder_id: str = Field(..., min_length=1)
    customer_photo_b64: str = Field(..., min_length=1)
    customer_photo_mime: str = Field(
        default="image/jpeg",
        min_length=1
    )
    consent_confirmed: bool = Field(
        default=False,
        description="Must be true â€” confirms tailor obtained customer consent before photo was taken"
    )


class TryOnResponse(BaseModel):
    result_b64: str
    result_mime: str


# --- Gemini client singleton -----------------------------------------------

_gemini_client: Optional[genai.Client] = None
_gemini_client_lock = threading.Lock()


def _get_gemini_client() -> genai.Client:
    global _gemini_client
    if _gemini_client is None:
        with _gemini_client_lock:
            if _gemini_client is None:
                settings = get_settings()
                _gemini_client = genai.Client(api_key=settings.GEMINI_API_KEY)
    return _gemini_client


# --- Storage byte cache ------------------------------------------------------

_storage_bytes_cache: TTLCache = TTLCache(maxsize=32, ttl=600)
_storage_bytes_cache_lock = threading.Lock()


def _decode_photo(b64: str) -> bytes:
    try:
        return base64.b64decode(b64)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid base64 for customer photo",
        )


def _fetch_storage_bytes(supabase, bucket: str, path: str) -> bytes:
    cache_key = (bucket, path)

    with _storage_bytes_cache_lock:
        cached = _storage_bytes_cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        result = supabase.storage.from_(bucket).download(path)
        if isinstance(result, (bytes, bytearray)):
            data = bytes(result)
        else:
            raise RuntimeError("Unexpected storage response type")
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Storage fetch failed: {exc}",
        )

    with _storage_bytes_cache_lock:
        _storage_bytes_cache[cache_key] = data

    return data


def _downscale_image_if_needed(image_bytes: bytes, mime_type: str) -> Tuple[bytes, str]:
    try:
        from PIL import Image

        image = Image.open(io.BytesIO(image_bytes))
        image.load()
        width, height = image.size

        if max(width, height) <= _MAX_INPUT_IMAGE_DIMENSION:
            return image_bytes, mime_type

        if width >= height:
            new_width = _MAX_INPUT_IMAGE_DIMENSION
            new_height = max(1, round(height * (_MAX_INPUT_IMAGE_DIMENSION / width)))
        else:
            new_height = _MAX_INPUT_IMAGE_DIMENSION
            new_width = max(1, round(width * (_MAX_INPUT_IMAGE_DIMENSION / height)))

        resized = image.convert("RGB").resize((new_width, new_height), Image.Resampling.LANCZOS)

        buf = io.BytesIO()
        resized.save(buf, format="JPEG", quality=90)
        return buf.getvalue(), "image/jpeg"
    except Exception as exc:
        print(f"[tryon] WARNING: image downscale failed, using original bytes: {exc}")
        return image_bytes, mime_type


def _log_tryon_consent(supabase, shop_id: str) -> None:
    try:
        supabase.table("customer_consent_logs").insert({
            "shop_id": shop_id,
            "purpose": "virtual_tryon",
            "confirmed_by_staff": True,
        }).execute()
    except Exception:
        pass


def _resolve_tryon_prompt(
    tryon_prompt: Optional[str],
    *,
    garment_name: Optional[str],
    fabric_assignments: Optional[list[dict]] = None,
    image_count: int,
    context: str = "",
) -> str:
    """Fill a garment's stored tryon_prompt with the dynamic placeholders,
    falling back to a minimal safe prompt (with a warning) if none is
    configured for this garment."""
    if not tryon_prompt or not tryon_prompt.strip():
        suffix = f" ({context})" if context else ""
        print(f"[tryon_api] WARNING: garment{suffix} has no tryon_prompt configured; using fallback prompt")
        tryon_prompt = DEFAULT_TRYON_PROMPT

    return fill_prompt_placeholders(
        tryon_prompt,
        garment_name=garment_name,
        fabric_assignments=fabric_assignments,
        image_count=image_count,
    )


_NO_IMAGE_DETAIL = "Gemini did not return an image for try-on"


def _first_inline_image(response) -> Optional[Any]:
    # A blocked request comes back with no candidates or no content: same 502 as "no image".
    candidates = getattr(response, "candidates", None) or []
    content = getattr(candidates[0], "content", None) if candidates else None
    for part in getattr(content, "parts", None) or []:
        if part.inline_data is not None:
            return part.inline_data
    return None


def _note_ai_response(ai_info: Optional[dict], response, model_id: str) -> None:
    """Operations log: AI timing, usage and finish reason (ai_info is filled in place)."""
    if ai_info is None:
        return
    ai_info["ai_finished_at"] = attempt_log.utc_now()
    if response is not None:
        ai_info.update(attempt_log.ai_response_fields(response, model_id))
        if _first_inline_image(response) is None:
            ai_info["no_image_step"] = attempt_log.no_image_step(response)


def _call_gemini_tryon(
    prompt: str,
    image_parts: list[tuple[bytes, str]],
    ai_info: Optional[dict] = None,
) -> TryOnResponse:
    settings = get_settings()
    client = _get_gemini_client()

    contents_parts: list = [prompt]
    for img_bytes, mime_type in image_parts:
        contents_parts.append(
            types.Part.from_bytes(data=img_bytes, mime_type=mime_type)
        )

    if ai_info is not None:
        ai_info["ai_started_at"] = attempt_log.utc_now()
    response = None
    try:
        response = client.models.generate_content(
            model=settings.GEMINI_IMAGE_MODEL_ID,
            contents=contents_parts,
            config=types.GenerateContentConfig(
                response_modalities=["IMAGE", "TEXT"],
            ),
        )
    finally:
        _note_ai_response(ai_info, response, settings.GEMINI_IMAGE_MODEL_ID)

    inline = _first_inline_image(response)
    if inline is not None:
        return TryOnResponse(
            result_b64=base64.b64encode(inline.data).decode("utf-8"),
            result_mime=inline.mime_type or "image/png",
        )

    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail=_NO_IMAGE_DETAIL,
    )


async def _call_gemini_tryon_async(
    prompt: str,
    image_parts: list[tuple[bytes, str]],
    ai_info: Optional[dict] = None,
) -> Tuple[bytes, str]:
    settings = get_settings()
    client = _get_gemini_client()

    contents_parts: list = [prompt]
    for img_bytes, mime_type in image_parts:
        contents_parts.append(
            types.Part.from_bytes(data=img_bytes, mime_type=mime_type)
        )

    if ai_info is not None:
        ai_info["ai_started_at"] = attempt_log.utc_now()
    response = None
    try:
        response = await client.aio.models.generate_content(
            model=settings.GEMINI_IMAGE_MODEL_ID,
            contents=contents_parts,
            config=types.GenerateContentConfig(
                response_modalities=["IMAGE", "TEXT"],
            ),
        )
    finally:
        _note_ai_response(ai_info, response, settings.GEMINI_IMAGE_MODEL_ID)

    inline = _first_inline_image(response)
    if inline is not None:
        return inline.data, inline.mime_type or "image/png"

    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail=_NO_IMAGE_DETAIL,
    )


# --- Operations log helpers ---------------------------------------------------

def _start_attempt(request: Request, current: CurrentShopContext, **fields: Any):
    attempt = attempt_log.from_request(request)
    attempt.set_step("validation")
    attempt.update(
        shop_id=current.shop_id,
        user_id=current.auth_user_id,
        user_email=current.email,
        model_id=get_settings().GEMINI_IMAGE_MODEL_ID,
        **fields,
    )
    return attempt


@contextmanager
def _input_stage(attempt):
    """Loading stored inputs: 4xx = validation, 502 = a stored image could not be read."""
    attempt.set_step("validation")
    try:
        yield
    except HTTPException as exc:
        attempt.note_error(exc)
        if exc.status_code == status.HTTP_502_BAD_GATEWAY:
            attempt.set_step("upload")
        raise
    except Exception as exc:
        attempt.note_error(exc)
        attempt.set_step("unknown")
        raise


def _log_prompt(
    attempt, prompt: str, *, garment_type_id: Optional[str], prompt_field: str, template: Optional[str]
) -> None:
    attempt.update(prompt_text=prompt)
    attempt.enrich(
        attempt_log.prompt_version_builder(
            garment_type_id,
            prompt_field,
            template,
            # Only the try-on prompt has a built-in fallback; look try-on has none.
            prompt_field == "tryon_prompt" and not (template or "").strip(),
        )
    )


def _ai_fields(ai_info: dict) -> dict:
    return {key: value for key, value in ai_info.items() if key != "no_image_step"}


def _log_ai_failure(attempt, ai_info: dict, exc: Exception) -> None:
    if isinstance(exc, HTTPException):
        message, code = exc.detail, exc.status_code
    else:
        message, code = "Internal Server Error", status.HTTP_500_INTERNAL_SERVER_ERROR
    attempt.fail(
        ai_info.get("no_image_step") or "ai_call",
        user_message=message if isinstance(message, str) else str(message),
        error=exc,
        http_status=code,
        **_ai_fields(ai_info),
    )


_LOOK_TRYON_PROMPT_MISSING_DETAIL = (
    "This garment type has no Look try-on prompt. Add it in the admin panel."
)


def _build_look_tryon_prompt(look_tryon_prompt: Optional[str], *, garment_name: Optional[str]) -> str:
    """Prompt for try-on from an already-generated look (Image 1 = look,
    Image 2 = customer). Admin-authored only: there is deliberately no fallback."""
    if not look_tryon_prompt or not look_tryon_prompt.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_LOOK_TRYON_PROMPT_MISSING_DETAIL,
        )

    return fill_prompt_placeholders(
        look_tryon_prompt,
        garment_name=garment_name,
        fabric_assignments=None,
        image_count=2,
    )


def _prepare_tryon_assets_sync(
    supabase, shop_id: str, generation_id: str, meta: Optional[dict] = None
) -> Tuple[bytes, Optional[str], Optional[str]]:
    """Blocking DB + storage prep phase for try-on from a look.
    Returns (look_bytes, garment_name, look_tryon_prompt). meta (optional) gets folder_id."""
    gen_result = (
        supabase.table("generations")
        .select("id, shop_id, output_path, folder_id, status")
        .eq("id", generation_id)
        .eq("shop_id", shop_id)
        .limit(1)
        .execute()
    )
    gen_rows = getattr(gen_result, "data", None) or []
    if not gen_rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Generation not found",
        )
    gen = gen_rows[0]

    if gen.get("status") != "done" or not gen.get("output_path"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Generation is not ready yet",
        )

    folder_name: Optional[str] = None
    look_tryon_prompt: Optional[str] = None
    folder_id = gen.get("folder_id")
    if meta is not None:
        meta["folder_id"] = folder_id
    if folder_id:
        folder_result = (
            supabase.table("garment_types")
            .select("name, look_tryon_prompt")
            .eq("id", folder_id)
            .limit(1)
            .execute()
        )
        folder_rows = getattr(folder_result, "data", None) or []
        if folder_rows:
            folder_name = folder_rows[0].get("name")
            look_tryon_prompt = folder_rows[0].get("look_tryon_prompt")

    garment_bytes = _fetch_storage_bytes(
        supabase,
        "generated-outputs",
        gen["output_path"],
    )

    return garment_bytes, folder_name, look_tryon_prompt


def _prepare_tryon_quick_assets_sync(
    supabase, shop_id: str, folder_id: str, fabric_image_id: str
) -> Tuple[bytes, str, bytes, str, Optional[str], Optional[str]]:
    """Blocking DB + storage prep phase for /tryon/quick/v2. Runs off the event loop."""
    folder_result = (
        supabase.table("garment_types")
        .select("id, name, default_hero_image_id, tryon_prompt")
        .eq("id", folder_id)
        .eq("shop_id", shop_id)
        .limit(1)
        .execute()
    )
    folder_rows = getattr(folder_result, "data", None) or []
    if not folder_rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Garment type not found",
        )
    folder = folder_rows[0]
    folder_name = folder.get("name")
    default_hero_id = folder.get("default_hero_image_id")
    tryon_prompt = folder.get("tryon_prompt")

    if not default_hero_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No hero image set for this garment type",
        )

    hero_result = (
        supabase.table("hero_images")
        .select("id, storage_path, mime_type")
        .eq("id", default_hero_id)
        .eq("shop_id", shop_id)
        .limit(1)
        .execute()
    )
    hero_rows = getattr(hero_result, "data", None) or []
    if not hero_rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Hero image not found",
        )
    hero = hero_rows[0]

    fabric_result = (
        supabase.table("fabric_images")
        .select("id, storage_path, mime_type")
        .eq("id", fabric_image_id)
        .eq("shop_id", shop_id)
        .limit(1)
        .execute()
    )
    fabric_rows = getattr(fabric_result, "data", None) or []
    if not fabric_rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Fabric image not found",
        )
    fabric = fabric_rows[0]

    hero_bytes = _fetch_storage_bytes(
        supabase,
        "hero-images",
        hero["storage_path"],
    )
    fabric_bytes = _fetch_storage_bytes(
        supabase,
        "fabric-images",
        fabric["storage_path"],
    )

    return (
        hero_bytes,
        str(hero.get("mime_type") or "image/jpeg"),
        fabric_bytes,
        str(fabric.get("mime_type") or "image/jpeg"),
        folder_name,
        tryon_prompt,
    )


def _load_fabric_slots_sync(supabase, shop_id: str, folder_id: str) -> list[dict]:
    """This shop's fabric slots for a garment type, in sort order (empty apply_to skipped)."""
    result = (
        supabase.table("garment_fabric_slots")
        .select("label, apply_to, sort_order")
        .eq("folder_id", folder_id)
        .eq("shop_id", shop_id)
        .order("sort_order")
        .execute()
    )
    return [row for row in (getattr(result, "data", None) or []) if str(row.get("apply_to") or "").strip()]


def _prepare_tryon_multi_assets_sync(
    supabase,
    shop_id: str,
    hero_image_id: str,
    folder_id: str,
    fabric_entries: list[dict],
) -> Tuple[bytes, str, list[Tuple[bytes, str]], Optional[str], Optional[str]]:
    """Blocking DB + storage prep phase for /tryon/multi/v2. Runs off the event loop.
    fabric_entries: {"fabric_image_id"} or a built-in {"default_fabric"}, in image order."""
    folder_result = (
        supabase.table("garment_types")
        .select("id, name, tryon_prompt")
        .eq("id", folder_id)
        .eq("shop_id", shop_id)
        .limit(1)
        .execute()
    )
    folder_rows = getattr(folder_result, "data", None) or []
    if not folder_rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Garment type not found",
        )
    folder_name = folder_rows[0].get("name")
    tryon_prompt = folder_rows[0].get("tryon_prompt")

    hero_result = (
        supabase.table("hero_images")
        .select("id, storage_path, mime_type")
        .eq("id", hero_image_id)
        .eq("shop_id", shop_id)
        .limit(1)
        .execute()
    )
    hero_rows = getattr(hero_result, "data", None) or []
    if not hero_rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Hero image not found",
        )
    hero = hero_rows[0]
    hero_bytes = _fetch_storage_bytes(
        supabase,
        "hero-images",
        hero["storage_path"],
    )
    hero_mime = str(hero.get("mime_type") or "image/jpeg")

    fabric_assets: list[Tuple[bytes, str]] = []
    for entry in fabric_entries:
        if entry.get("default_fabric"):
            fabric_assets.append(default_fabrics.load(entry["default_fabric"]))
            continue
        fabric_image_id = entry["fabric_image_id"]
        fabric_result = (
            supabase.table("fabric_images")
            .select("id, storage_path, mime_type")
            .eq("id", fabric_image_id)
            .eq("shop_id", shop_id)
            .limit(1)
            .execute()
        )
        fabric_rows = getattr(fabric_result, "data", None) or []
        if not fabric_rows:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Fabric image not found",
            )
        fabric = fabric_rows[0]
        fabric_bytes = _fetch_storage_bytes(
            supabase,
            "fabric-images",
            fabric["storage_path"],
        )
        fabric_assets.append(
            (fabric_bytes, str(fabric.get("mime_type") or "image/jpeg"))
        )

    return hero_bytes, hero_mime, fabric_assets, folder_name, tryon_prompt


@router.post("/", response_model=TryOnResponse)
def tryon(
    body: TryOnRequest,
    background_tasks: BackgroundTasks,
    request: Request,
    current: CurrentShopContext = Depends(get_current_shop_context),
):
    """
    Approach B - Stage 2 only.
    Takes an existing generation output + customer photo.
    Calls Gemini with: look image first, customer photo last.
    Returns composite image as base64. Nothing is stored.
    """
    supabase = get_supabase_admin_client()
    source_generation_id = attempt_log.valid_uuid(body.generation_id)
    attempt = _start_attempt(request, current, source_generation_id=source_generation_id)

    if not body.consent_confirmed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_CONSENT_REJECTED_DETAIL,
        )
    background_tasks.add_task(_log_tryon_consent, supabase, current.shop_id)
    if source_generation_id:
        attempt.enrich(attempt_log.inputs_builder(current.shop_id, source_generation_id=source_generation_id))

    # Fetch the generation and verify it belongs to this shop
    gen_result = (
        supabase.table("generations")
        .select("id, shop_id, output_path, folder_id, status")
        .eq("id", body.generation_id.strip())
        .eq("shop_id", current.shop_id)
        .limit(1)
        .execute()
    )
    gen_rows = getattr(gen_result, "data", None) or []
    if not gen_rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Generation not found",
        )
    gen = gen_rows[0]

    if gen.get("status") != "done" or not gen.get("output_path"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Generation is not ready yet",
        )

    # Fetch folder name + look try-on prompt for prompt context
    folder_name: Optional[str] = None
    folder_look_tryon_prompt: Optional[str] = None
    folder_id = gen.get("folder_id")
    if folder_id:
        folder_result = (
            supabase.table("garment_types")
            .select("name, look_tryon_prompt")
            .eq("id", folder_id)
            .limit(1)
            .execute()
        )
        folder_rows = getattr(folder_result, "data", None) or []
        if folder_rows:
            folder_name = folder_rows[0].get("name")
            folder_look_tryon_prompt = folder_rows[0].get("look_tryon_prompt")
    attempt.update(garment_type_id=folder_id, garment_type_name=folder_name)

    # Build prompt (400 if the garment type has no Look try-on prompt)
    attempt.set_step("prompt_build")
    prompt = _build_look_tryon_prompt(folder_look_tryon_prompt, garment_name=folder_name)
    _log_prompt(
        attempt, prompt, garment_type_id=folder_id, prompt_field="look_tryon_prompt", template=folder_look_tryon_prompt
    )

    # Download garment image (Stage 1 output)
    with _input_stage(attempt):
        garment_bytes = _fetch_storage_bytes(
            supabase,
            "generated-outputs",
            gen["output_path"],
        )

        # Decode customer photo from base64 (never stored)
        customer_bytes = _decode_photo(body.customer_photo_b64)

    # Call Gemini: look first, customer last
    attempt.set_step("ai_call")
    ai_info: dict = {}
    try:
        result = _call_gemini_tryon(
            prompt=prompt,
            image_parts=[
                (garment_bytes, "image/jpeg"),
                (customer_bytes, body.customer_photo_mime),
            ],
            ai_info=ai_info,
        )
    except Exception as exc:
        _log_ai_failure(attempt, ai_info, exc)
        raise
    attempt.succeed(**_ai_fields(ai_info))
    return result


@router.post("/quick", response_model=TryOnResponse)
def tryon_quick(
    body: TryOnQuickRequest,
    background_tasks: BackgroundTasks,
    request: Request,
    current: CurrentShopContext = Depends(get_current_shop_context),
):
    """
    Approach A - all three images in one call.
    Takes fabric_image_id + folder_id + customer photo.
    Calls Gemini with: hero_image + fabric_image + customer_photo.
    Returns composite image as base64. Nothing is stored.
    """
    supabase = get_supabase_admin_client()
    attempt = _start_attempt(request, current, garment_type_id=attempt_log.valid_uuid(body.folder_id))

    if not body.consent_confirmed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_CONSENT_REJECTED_DETAIL,
        )
    background_tasks.add_task(_log_tryon_consent, supabase, current.shop_id)

    shop_id = current.shop_id

    # Fetch folder -> default hero image
    folder_result = (
        supabase.table("garment_types")
        .select("id, name, default_hero_image_id, tryon_prompt")
        .eq("id", body.folder_id.strip())
        .eq("shop_id", shop_id)
        .limit(1)
        .execute()
    )
    folder_rows = getattr(folder_result, "data", None) or []
    if not folder_rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Garment type not found",
        )
    folder = folder_rows[0]
    folder_name = folder.get("name")
    default_hero_id = folder.get("default_hero_image_id")
    folder_tryon_prompt = folder.get("tryon_prompt")
    attempt.update(garment_type_name=folder_name)

    if not default_hero_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No hero image set for this garment type",
        )

    # Fetch hero image storage path
    hero_result = (
        supabase.table("hero_images")
        .select("id, storage_path, mime_type")
        .eq("id", default_hero_id)
        .eq("shop_id", shop_id)
        .limit(1)
        .execute()
    )
    hero_rows = getattr(hero_result, "data", None) or []
    if not hero_rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Hero image not found",
        )
    hero = hero_rows[0]

    # Fetch this garment's fabric slots (multi-fabric garments, e.g. saree)
    slots_result = (
        supabase.table("garment_fabric_slots")
        .select("id, label, apply_to, sort_order")
        .eq("folder_id", body.folder_id.strip())
        .order("sort_order")
        .limit(6)
        .execute()
    )
    slot_rows = getattr(slots_result, "data", None) or []

    if slot_rows and body.fabric_image_ids:
        fabric_ids = [fid.strip() for fid in body.fabric_image_ids if fid and fid.strip()]
        # Ids fill the slots in order; the first slot is required, later empty slots get
        # a built-in default fabric in their place.
        slot_entries = default_fabrics.resolve_slot_entries(
            slot_rows,
            {
                str(slot.get("apply_to") or "").strip().casefold(): {
                    "fabric_image_id": fabric_id,
                    "apply_to": slot.get("apply_to", "unknown"),
                }
                for slot, fabric_id in zip(slot_rows, fabric_ids)
            },
            default_fabrics.parse_choices(body.default_fabrics),
        )
        fabric_assignments = [{"apply_to": entry["apply_to"]} for entry in slot_entries]
        # More ids than slots: kept as before (sent, without a slot assignment).
        fabric_entries = slot_entries + [{"fabric_image_id": fid} for fid in fabric_ids[len(slot_rows):]]
    else:
        fabric_entries = [{"fabric_image_id": body.fabric_image_id.strip()}]
        fabric_assignments = [{"apply_to": "suit_full_body"}]

    # Fetch fabric image storage paths, in the given order (defaults have none)
    fabric_images: list[dict] = []
    for entry in fabric_entries:
        if entry.get("default_fabric"):
            fabric_images.append({"default_fabric": entry["default_fabric"]})
            continue
        fabric_id = entry["fabric_image_id"]
        fabric_result = (
            supabase.table("fabric_images")
            .select("id, storage_path, mime_type")
            .eq("id", fabric_id)
            .eq("shop_id", shop_id)
            .limit(1)
            .execute()
        )
        fabric_rows = getattr(fabric_result, "data", None) or []
        if not fabric_rows:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Fabric image not found",
            )
        fabric_images.append(fabric_rows[0])

    slot_labels = [slot.get("label") for slot in slot_rows] if slot_rows and body.fabric_image_ids else []
    attempt.update(
        hero_path=hero.get("storage_path"),
        hero_colour_corrected=False,
        fabric_mode="multi" if len(fabric_images) > 1 else "single",
        fabric_inputs=[
            {
                "slot_label": slot_labels[index] if index < len(slot_labels) else None,
                "apply_to": fabric_assignments[index]["apply_to"] if index < len(fabric_assignments) else None,
                "sort_order": index,
                "storage_path": fabric.get("storage_path"),
                "colour_corrected": False,
                **(
                    {
                        "default_fabric": fabric["default_fabric"],
                        "fabric_label": default_fabrics.label(fabric["default_fabric"]),
                    }
                    if fabric.get("default_fabric")
                    else {}
                ),
            }
            for index, fabric in enumerate(fabric_images)
        ],
    )
    default_fabrics.ensure_available(
        [fabric["default_fabric"] for fabric in fabric_images if fabric.get("default_fabric")]
    )

    # Download hero and fabric images from storage (built-in defaults come from disk)
    with _input_stage(attempt):
        hero_bytes = _fetch_storage_bytes(
            supabase,
            "hero-images",
            hero["storage_path"],
        )
        fabric_image_parts = [
            default_fabrics.load(fabric["default_fabric"])
            if fabric.get("default_fabric")
            else (
                _fetch_storage_bytes(supabase, "fabric-images", fabric["storage_path"]),
                fabric.get("mime_type") or "image/jpeg",
            )
            for fabric in fabric_images
        ]

        # Decode customer photo (never stored)
        customer_bytes = _decode_photo(body.customer_photo_b64)

    # Build combined prompt
    attempt.set_step("prompt_build")
    prompt = _resolve_tryon_prompt(
        folder_tryon_prompt,
        garment_name=folder_name,
        fabric_assignments=fabric_assignments,
        image_count=1 + len(fabric_images) + 1,
        context=f"folder {body.folder_id.strip()}",
    )
    _log_prompt(
        attempt, prompt, garment_type_id=folder.get("id"), prompt_field="tryon_prompt", template=folder_tryon_prompt
    )

    # Call Gemini: hero first, then fabrics in slot order, customer last
    attempt.set_step("ai_call")
    ai_info: dict = {}
    try:
        result = _call_gemini_tryon(
            prompt=prompt,
            image_parts=[
                (hero_bytes, hero.get("mime_type") or "image/jpeg"),
                *fabric_image_parts,
                (customer_bytes, body.customer_photo_mime),
            ],
            ai_info=ai_info,
        )
    except Exception as exc:
        _log_ai_failure(attempt, ai_info, exc)
        raise
    attempt.succeed(**_ai_fields(ai_info))
    return result


@router.post("/v2")
async def tryon_v2(
    background_tasks: BackgroundTasks,
    request: Request,
    generation_id: str = Form(...),
    consent_confirmed: bool = Form(...),
    customer_photo: UploadFile = File(...),
    current: CurrentShopContext = Depends(get_current_shop_context),
):
    """
    Async/multipart variant of POST /tryon/. Returns raw binary image bytes
    instead of a base64 JSON payload.
    """
    supabase = get_supabase_admin_client()
    source_generation_id = attempt_log.valid_uuid(generation_id)
    attempt = _start_attempt(request, current, source_generation_id=source_generation_id)

    if not consent_confirmed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_CONSENT_REJECTED_DETAIL,
        )
    background_tasks.add_task(_log_tryon_consent, supabase, current.shop_id)
    if source_generation_id:
        attempt.enrich(attempt_log.inputs_builder(current.shop_id, source_generation_id=source_generation_id))

    look_meta: dict = {}
    with _input_stage(attempt):
        garment_bytes, folder_name, folder_look_tryon_prompt = await anyio.to_thread.run_sync(
            _prepare_tryon_assets_sync,
            supabase,
            current.shop_id,
            generation_id.strip(),
            look_meta,
        )
    attempt.update(garment_type_id=look_meta.get("folder_id"), garment_type_name=folder_name)

    attempt.set_step("prompt_build")
    prompt = _build_look_tryon_prompt(folder_look_tryon_prompt, garment_name=folder_name)
    _log_prompt(
        attempt,
        prompt,
        garment_type_id=look_meta.get("folder_id"),
        prompt_field="look_tryon_prompt",
        template=folder_look_tryon_prompt,
    )

    attempt.set_step("upload")
    customer_bytes = await customer_photo.read()
    customer_mime = customer_photo.content_type or "image/jpeg"

    customer_bytes, customer_mime = _downscale_image_if_needed(customer_bytes, customer_mime)
    garment_bytes, garment_mime = _downscale_image_if_needed(garment_bytes, "image/jpeg")

    # Look first, customer last
    attempt.set_step("ai_call")
    ai_info: dict = {}
    try:
        image_bytes, result_mime = await _call_gemini_tryon_async(
            prompt=prompt,
            image_parts=[
                (garment_bytes, garment_mime),
                (customer_bytes, customer_mime),
            ],
            ai_info=ai_info,
        )
    except Exception as exc:
        _log_ai_failure(attempt, ai_info, exc)
        raise
    attempt.succeed(**_ai_fields(ai_info))

    return Response(content=image_bytes, media_type=result_mime)


@router.post("/quick/v2")
async def tryon_quick_v2(
    background_tasks: BackgroundTasks,
    request: Request,
    fabric_image_id: str = Form(...),
    folder_id: str = Form(...),
    consent_confirmed: bool = Form(...),
    customer_photo: UploadFile = File(...),
    current: CurrentShopContext = Depends(get_current_shop_context),
):
    """
    Async/multipart variant of POST /tryon/quick. Returns raw binary image
    bytes instead of a base64 JSON payload.
    """
    supabase = get_supabase_admin_client()
    garment_type_id = attempt_log.valid_uuid(folder_id)
    attempt = _start_attempt(request, current, garment_type_id=garment_type_id, fabric_mode="single")

    if not consent_confirmed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_CONSENT_REJECTED_DETAIL,
        )
    background_tasks.add_task(_log_tryon_consent, supabase, current.shop_id)
    if garment_type_id and attempt_log.valid_uuid(fabric_image_id):
        attempt.enrich(
            attempt_log.inputs_builder(
                current.shop_id,
                hero_from_garment_type_id=garment_type_id,
                slot_labels_garment_type_id=garment_type_id,
                fabrics=[{"fabric_image_id": fabric_image_id.strip(), "apply_to": "suit_full_body", "sort_order": 0}],
            )
        )

    with _input_stage(attempt):
        (
            hero_bytes,
            hero_mime,
            fabric_bytes,
            fabric_mime,
            folder_name,
            folder_tryon_prompt,
        ) = await anyio.to_thread.run_sync(
            _prepare_tryon_quick_assets_sync,
            supabase,
            current.shop_id,
            folder_id.strip(),
            fabric_image_id.strip(),
        )
    attempt.update(garment_type_name=folder_name)

    attempt.set_step("upload")
    customer_bytes = await customer_photo.read()
    customer_mime = customer_photo.content_type or "image/jpeg"

    hero_bytes, hero_mime = _downscale_image_if_needed(hero_bytes, hero_mime)
    fabric_bytes, fabric_mime = _downscale_image_if_needed(fabric_bytes, fabric_mime)
    customer_bytes, customer_mime = _downscale_image_if_needed(customer_bytes, customer_mime)

    attempt.set_step("prompt_build")
    prompt = _resolve_tryon_prompt(
        folder_tryon_prompt,
        garment_name=folder_name,
        fabric_assignments=[{"apply_to": "suit_full_body"}],
        image_count=3,
        context=f"folder {folder_id.strip()}",
    )
    _log_prompt(attempt, prompt, garment_type_id=garment_type_id, prompt_field="tryon_prompt", template=folder_tryon_prompt)

    attempt.set_step("ai_call")
    ai_info: dict = {}
    try:
        image_bytes, result_mime = await _call_gemini_tryon_async(
            prompt=prompt,
            image_parts=[
                (hero_bytes, hero_mime),
                (fabric_bytes, fabric_mime),
                (customer_bytes, customer_mime),
            ],
            ai_info=ai_info,
        )
    except Exception as exc:
        _log_ai_failure(attempt, ai_info, exc)
        raise
    attempt.succeed(**_ai_fields(ai_info))

    return Response(content=image_bytes, media_type=result_mime)


@router.post("/multi/v2")
async def tryon_multi_v2(
    background_tasks: BackgroundTasks,
    request: Request,
    hero_image_id: str = Form(...),
    folder_id: str = Form(...),
    consent_confirmed: str = Form(...),
    customer_photo: UploadFile = File(...),
    fabric_image_id_1: str = Form(...),
    apply_to_1: str = Form(...),
    fabric_image_id_2: Optional[str] = Form(default=None),
    apply_to_2: Optional[str] = Form(default=None),
    fabric_image_id_3: Optional[str] = Form(default=None),
    apply_to_3: Optional[str] = Form(default=None),
    fabric_image_id_4: Optional[str] = Form(default=None),
    apply_to_4: Optional[str] = Form(default=None),
    # JSON object {"<slot apply_to>": "white" | "black"} for optional slots left empty.
    default_fabrics_json: Optional[str] = Form(default=None, alias="default_fabrics"),
    current: CurrentShopContext = Depends(get_current_shop_context),
):
    """
    Async/multipart variant supporting multiple fabric slots (up to 4).
    Returns raw binary image bytes instead of a base64 JSON payload.
    """
    supabase = get_supabase_admin_client()
    garment_type_id = attempt_log.valid_uuid(folder_id)
    attempt = _start_attempt(request, current, garment_type_id=garment_type_id)

    if consent_confirmed.strip().lower() != "true":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_CONSENT_REJECTED_DETAIL,
        )
    background_tasks.add_task(_log_tryon_consent, supabase, current.shop_id)

    raw_pairs = [
        (fabric_image_id_1, apply_to_1),
        (fabric_image_id_2, apply_to_2),
        (fabric_image_id_3, apply_to_3),
        (fabric_image_id_4, apply_to_4),
    ]
    fabric_pairs = [
        (fabric_id.strip(), (apply_to or "").strip())
        for fabric_id, apply_to in raw_pairs
        if fabric_id and fabric_id.strip()
    ]

    if not fabric_pairs:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="At least one fabric is required",
        )

    fabric_entries = [{"fabric_image_id": fabric_id, "apply_to": apply_to} for fabric_id, apply_to in fabric_pairs]
    with _input_stage(attempt):
        slot_rows = await anyio.to_thread.run_sync(
            _load_fabric_slots_sync, supabase, current.shop_id, folder_id.strip()
        )
        slot_keys = {str(slot["apply_to"]).strip().casefold() for slot in slot_rows}
        pair_keys = [apply_to.casefold() for _, apply_to in fabric_pairs]
        # Garments with slots, when every fabric names one of them: slot order, the first
        # slot required, later empty slots filled with a built-in default fabric. Anything
        # else keeps the previous behaviour exactly.
        if slot_rows and len(set(pair_keys)) == len(pair_keys) and all(key in slot_keys for key in pair_keys):
            fabric_entries = default_fabrics.resolve_slot_entries(
                slot_rows,
                {entry["apply_to"].casefold(): entry for entry in fabric_entries},
                default_fabrics.parse_choices(default_fabrics_json),
            )
            default_fabrics.ensure_available(
                [entry["default_fabric"] for entry in fabric_entries if entry.get("default_fabric")]
            )

    if all(
        attempt_log.valid_uuid(entry["fabric_image_id"]) for entry in fabric_entries if not entry.get("default_fabric")
    ):
        attempt.enrich(
            attempt_log.inputs_builder(
                current.shop_id,
                hero_image_id=attempt_log.valid_uuid(hero_image_id),
                slot_labels_garment_type_id=garment_type_id,
                fabrics=[
                    {
                        "fabric_image_id": entry.get("fabric_image_id"),
                        "default_fabric": entry.get("default_fabric"),
                        "apply_to": entry["apply_to"],
                        "sort_order": index,
                    }
                    for index, entry in enumerate(fabric_entries)
                ],
            )
        )

    with _input_stage(attempt):
        (
            hero_bytes,
            hero_mime,
            fabric_assets,
            folder_name,
            folder_tryon_prompt,
        ) = await anyio.to_thread.run_sync(
            _prepare_tryon_multi_assets_sync,
            supabase,
            current.shop_id,
            hero_image_id.strip(),
            folder_id.strip(),
            fabric_entries,
        )
    attempt.update(garment_type_name=folder_name)

    attempt.set_step("upload")
    customer_bytes = await customer_photo.read()
    customer_mime = customer_photo.content_type or "image/jpeg"

    hero_bytes, hero_mime = _downscale_image_if_needed(hero_bytes, hero_mime)
    customer_bytes, customer_mime = _downscale_image_if_needed(customer_bytes, customer_mime)
    fabric_assets = [
        _downscale_image_if_needed(fabric_bytes, fabric_mime)
        for fabric_bytes, fabric_mime in fabric_assets
    ]

    fabric_assignments = [
        {"apply_to": entry["apply_to"], "image_index": idx}
        for idx, entry in enumerate(fabric_entries)
    ]

    attempt.set_step("prompt_build")
    prompt = _resolve_tryon_prompt(
        folder_tryon_prompt,
        garment_name=folder_name,
        fabric_assignments=fabric_assignments,
        image_count=1 + len(fabric_entries) + 1,
        context=f"folder {folder_id.strip()}",
    )
    _log_prompt(attempt, prompt, garment_type_id=garment_type_id, prompt_field="tryon_prompt", template=folder_tryon_prompt)

    image_parts = [(hero_bytes, hero_mime), *fabric_assets, (customer_bytes, customer_mime)]

    attempt.set_step("ai_call")
    ai_info: dict = {}
    try:
        image_bytes, result_mime = await _call_gemini_tryon_async(
            prompt=prompt,
            image_parts=image_parts,
            ai_info=ai_info,
        )
    except Exception as exc:
        _log_ai_failure(attempt, ai_info, exc)
        raise
    attempt.succeed(**_ai_fields(ai_info))

    return Response(content=image_bytes, media_type=result_mime)


def _upload_generated_output(supabase, *, bucket: str, path: str, data: bytes, content_type: str) -> None:
    options = {"content-type": content_type, "upsert": "true"}
    try:
        supabase.storage.from_(bucket).upload(path, data, options)
    except Exception:
        supabase.storage.from_(bucket).update(path, data, options)


@router.post("/push-to-screen")
async def push_tryon_to_screen(
    request: Request,
    result_image: UploadFile = File(...),
    folder_id: str = Form(...),
    fabric_image_id: Optional[str] = Form(default=None),
    hero_image_id: Optional[str] = Form(default=None),
    source_generation_id: Optional[str] = Form(default=None),
    current: CurrentShopContext = Depends(get_current_shop_context),
):
    """
    Persist-on-push: the web-app equivalent of the WhatsApp TV flow. Takes
    an ALREADY-GENERATED try-on output image and, only when the shopkeeper
    pushes it live, stores it and puts it on the shop's TV. No customer
    input photo is stored — this endpoint only ever receives the output
    image.
    """
    supabase = get_supabase_admin_client()
    settings = get_settings()

    shop_id = current.shop_id
    new_generation_id = str(uuid4())

    data = await result_image.read()
    mime = result_image.content_type or "image/png"
    ext = "png" if "png" in mime else "jpg"

    output_path = f"{shop_id}/{new_generation_id}/output_v{int(time.time())}.{ext}"
    _upload_generated_output(
        supabase,
        bucket="generated-outputs",
        path=output_path,
        data=data,
        content_type=mime,
    )

    now_iso = datetime.now(timezone.utc).isoformat()

    supabase.table("generations").insert(
        {
            "id": new_generation_id,
            "shop_id": shop_id,
            "hero_image_id": (hero_image_id or "").strip() or None,
            "fabric_image_id": (fabric_image_id or "").strip() or None,
            "folder_id": folder_id.strip(),
            "status": "done",
            "generation_type": "tryon",
            "model_used": settings.GEMINI_IMAGE_MODEL_ID,
            "prompt_used": "web-app try-on (pushed to screen)",
            "output_path": output_path,
            "credits_used": 0,
            "show_on_screen": True,
            "created_at": now_iso,
            "started_at": now_iso,
            "completed_at": now_iso,
        }
    ).execute()

    supabase.table("shop_screen_state").upsert(
        {
            "shop_id": shop_id,
            "live_generation_id": new_generation_id,
            "mode": "live",
            "updated_at": now_iso,
        }
    ).execute()

    # Optional X-Client-Request-Id (the try-on's id): attach the stored result to its log row.
    attempt_log.link_tryon_result(
        request.headers.get("x-client-request-id"), shop_id, output_path, generation_id=new_generation_id
    )

    return {"generation_id": new_generation_id, "live": True}
