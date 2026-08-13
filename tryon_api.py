import base64
import io
import threading
from typing import Optional, Tuple

import anyio.to_thread
from cachetools import TTLCache
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Response,
    UploadFile,
    status,
)
from pydantic import BaseModel, Field

from auth_deps import CurrentShopContext, get_current_shop_context
from config import get_settings
from prompting import (
    build_tryon_multi_quick_prompt,
    build_tryon_prompt,
    build_tryon_quick_prompt,
)
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


def _call_gemini_tryon(
    prompt: str,
    image_parts: list[tuple[bytes, str]],
) -> TryOnResponse:
    settings = get_settings()
    client = _get_gemini_client()

    contents_parts: list = [prompt]
    for img_bytes, mime_type in image_parts:
        contents_parts.append(
            types.Part.from_bytes(data=img_bytes, mime_type=mime_type)
        )

    response = client.models.generate_content(
        model=settings.GEMINI_IMAGE_MODEL_ID,
        contents=contents_parts,
        config=types.GenerateContentConfig(
            response_modalities=["IMAGE", "TEXT"],
        ),
    )

    for part in response.candidates[0].content.parts:
        if part.inline_data is not None:
            return TryOnResponse(
                result_b64=base64.b64encode(
                    part.inline_data.data
                ).decode("utf-8"),
                result_mime=part.inline_data.mime_type or "image/png",
            )

    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail="Gemini did not return an image for try-on",
    )


async def _call_gemini_tryon_async(
    prompt: str,
    image_parts: list[tuple[bytes, str]],
) -> Tuple[bytes, str]:
    settings = get_settings()
    client = _get_gemini_client()

    contents_parts: list = [prompt]
    for img_bytes, mime_type in image_parts:
        contents_parts.append(
            types.Part.from_bytes(data=img_bytes, mime_type=mime_type)
        )

    response = await client.aio.models.generate_content(
        model=settings.GEMINI_IMAGE_MODEL_ID,
        contents=contents_parts,
        config=types.GenerateContentConfig(
            response_modalities=["IMAGE", "TEXT"],
        ),
    )

    for part in response.candidates[0].content.parts:
        if part.inline_data is not None:
            return part.inline_data.data, part.inline_data.mime_type or "image/png"

    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail="Gemini did not return an image for try-on",
    )


def _prepare_tryon_assets_sync(
    supabase, shop_id: str, generation_id: str
) -> Tuple[bytes, Optional[str]]:
    """Blocking DB + storage prep phase for /tryon/v2. Runs off the event loop."""
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
    folder_id = gen.get("folder_id")
    if folder_id:
        folder_result = (
            supabase.table("garment_types")
            .select("name")
            .eq("id", folder_id)
            .limit(1)
            .execute()
        )
        folder_rows = getattr(folder_result, "data", None) or []
        if folder_rows:
            folder_name = folder_rows[0].get("name")

    garment_bytes = _fetch_storage_bytes(
        supabase,
        "generated-outputs",
        gen["output_path"],
    )

    return garment_bytes, folder_name


def _prepare_tryon_quick_assets_sync(
    supabase, shop_id: str, folder_id: str, fabric_image_id: str
) -> Tuple[bytes, str, bytes, str, Optional[str]]:
    """Blocking DB + storage prep phase for /tryon/quick/v2. Runs off the event loop."""
    folder_result = (
        supabase.table("garment_types")
        .select("id, name, default_hero_image_id")
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
    )


def _prepare_tryon_multi_assets_sync(
    supabase,
    shop_id: str,
    hero_image_id: str,
    folder_id: str,
    fabric_image_ids: list[str],
) -> Tuple[bytes, str, list[Tuple[bytes, str]], Optional[str]]:
    """Blocking DB + storage prep phase for /tryon/multi/v2. Runs off the event loop."""
    folder_result = (
        supabase.table("garment_types")
        .select("id, name")
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
    for fabric_image_id in fabric_image_ids:
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

    return hero_bytes, hero_mime, fabric_assets, folder_name


@router.post("/", response_model=TryOnResponse)
def tryon(
    body: TryOnRequest,
    background_tasks: BackgroundTasks,
    current: CurrentShopContext = Depends(get_current_shop_context),
):
    """
    Approach B - Stage 2 only.
    Takes an existing generation output + customer photo.
    Calls Gemini with: customer_photo + garment_image.
    Returns composite image as base64. Nothing is stored.
    """
    supabase = get_supabase_admin_client()

    if not body.consent_confirmed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_CONSENT_REJECTED_DETAIL,
        )
    background_tasks.add_task(_log_tryon_consent, supabase, current.shop_id)

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

    # Fetch folder name for prompt context
    folder_name: Optional[str] = None
    folder_use_custom_prompt = False
    folder_custom_tryon_prompt: Optional[str] = None
    folder_id = gen.get("folder_id")
    if folder_id:
        folder_result = (
            supabase.table("garment_types")
            .select("name, use_custom_prompt, custom_tryon_prompt")
            .eq("id", folder_id)
            .limit(1)
            .execute()
        )
        folder_rows = getattr(folder_result, "data", None) or []
        if folder_rows:
            folder_name = folder_rows[0].get("name")
            folder_use_custom_prompt = bool(folder_rows[0].get("use_custom_prompt"))
            folder_custom_tryon_prompt = folder_rows[0].get("custom_tryon_prompt")

    # Download garment image (Stage 1 output)
    garment_bytes = _fetch_storage_bytes(
        supabase,
        "generated-outputs",
        gen["output_path"],
    )

    # Decode customer photo from base64 (never stored)
    customer_bytes = _decode_photo(body.customer_photo_b64)

    # Build prompt
    if folder_use_custom_prompt and folder_custom_tryon_prompt and folder_custom_tryon_prompt.strip():
        prompt = folder_custom_tryon_prompt
    else:
        prompt = build_tryon_prompt(folder_name=folder_name)

    # Call Gemini: customer first, then garment
    return _call_gemini_tryon(
        prompt=prompt,
        image_parts=[
            (customer_bytes, body.customer_photo_mime),
            (garment_bytes, "image/jpeg"),
        ],
    )


@router.post("/quick", response_model=TryOnResponse)
def tryon_quick(
    body: TryOnQuickRequest,
    background_tasks: BackgroundTasks,
    current: CurrentShopContext = Depends(get_current_shop_context),
):
    """
    Approach A - all three images in one call.
    Takes fabric_image_id + folder_id + customer photo.
    Calls Gemini with: hero_image + fabric_image + customer_photo.
    Returns composite image as base64. Nothing is stored.
    """
    supabase = get_supabase_admin_client()

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
        .select("id, name, default_hero_image_id, use_custom_prompt, custom_tryon_prompt")
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
    folder_use_custom_prompt = bool(folder.get("use_custom_prompt"))
    folder_custom_tryon_prompt = folder.get("custom_tryon_prompt")

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
        .select("id, sort_order")
        .eq("folder_id", body.folder_id.strip())
        .order("sort_order")
        .limit(6)
        .execute()
    )
    slot_rows = getattr(slots_result, "data", None) or []

    if slot_rows and body.fabric_image_ids:
        fabric_ids = [fid.strip() for fid in body.fabric_image_ids if fid and fid.strip()]
    else:
        fabric_ids = [body.fabric_image_id.strip()]

    # Fetch fabric image storage paths, in the given order
    fabric_images: list[dict] = []
    for fabric_id in fabric_ids:
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

    # Download hero and fabric images from storage
    hero_bytes = _fetch_storage_bytes(
        supabase,
        "hero-images",
        hero["storage_path"],
    )
    fabric_image_parts = [
        (
            _fetch_storage_bytes(supabase, "fabric-images", fabric["storage_path"]),
            fabric.get("mime_type") or "image/jpeg",
        )
        for fabric in fabric_images
    ]

    # Decode customer photo (never stored)
    customer_bytes = _decode_photo(body.customer_photo_b64)

    # Build combined prompt
    if folder_use_custom_prompt and folder_custom_tryon_prompt and folder_custom_tryon_prompt.strip():
        prompt = folder_custom_tryon_prompt
    else:
        prompt = build_tryon_quick_prompt(folder_name=folder_name)

    # Call Gemini: hero first, then fabrics in slot order, customer last
    return _call_gemini_tryon(
        prompt=prompt,
        image_parts=[
            (hero_bytes, hero.get("mime_type") or "image/jpeg"),
            *fabric_image_parts,
            (customer_bytes, body.customer_photo_mime),
        ],
    )


@router.post("/v2")
async def tryon_v2(
    background_tasks: BackgroundTasks,
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

    if not consent_confirmed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_CONSENT_REJECTED_DETAIL,
        )
    background_tasks.add_task(_log_tryon_consent, supabase, current.shop_id)

    garment_bytes, folder_name = await anyio.to_thread.run_sync(
        _prepare_tryon_assets_sync,
        supabase,
        current.shop_id,
        generation_id.strip(),
    )

    customer_bytes = await customer_photo.read()
    customer_mime = customer_photo.content_type or "image/jpeg"

    customer_bytes, customer_mime = _downscale_image_if_needed(customer_bytes, customer_mime)
    garment_bytes, garment_mime = _downscale_image_if_needed(garment_bytes, "image/jpeg")

    prompt = build_tryon_prompt(folder_name=folder_name)

    image_bytes, result_mime = await _call_gemini_tryon_async(
        prompt=prompt,
        image_parts=[
            (customer_bytes, customer_mime),
            (garment_bytes, garment_mime),
        ],
    )

    return Response(content=image_bytes, media_type=result_mime)


@router.post("/quick/v2")
async def tryon_quick_v2(
    background_tasks: BackgroundTasks,
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

    if not consent_confirmed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_CONSENT_REJECTED_DETAIL,
        )
    background_tasks.add_task(_log_tryon_consent, supabase, current.shop_id)

    (
        hero_bytes,
        hero_mime,
        fabric_bytes,
        fabric_mime,
        folder_name,
    ) = await anyio.to_thread.run_sync(
        _prepare_tryon_quick_assets_sync,
        supabase,
        current.shop_id,
        folder_id.strip(),
        fabric_image_id.strip(),
    )

    customer_bytes = await customer_photo.read()
    customer_mime = customer_photo.content_type or "image/jpeg"

    hero_bytes, hero_mime = _downscale_image_if_needed(hero_bytes, hero_mime)
    fabric_bytes, fabric_mime = _downscale_image_if_needed(fabric_bytes, fabric_mime)
    customer_bytes, customer_mime = _downscale_image_if_needed(customer_bytes, customer_mime)

    prompt = build_tryon_quick_prompt(folder_name=folder_name)

    image_bytes, result_mime = await _call_gemini_tryon_async(
        prompt=prompt,
        image_parts=[
            (hero_bytes, hero_mime),
            (fabric_bytes, fabric_mime),
            (customer_bytes, customer_mime),
        ],
    )

    return Response(content=image_bytes, media_type=result_mime)


@router.post("/multi/v2")
async def tryon_multi_v2(
    background_tasks: BackgroundTasks,
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
    current: CurrentShopContext = Depends(get_current_shop_context),
):
    """
    Async/multipart variant supporting multiple fabric slots (up to 4).
    Returns raw binary image bytes instead of a base64 JSON payload.
    """
    supabase = get_supabase_admin_client()

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

    (
        hero_bytes,
        hero_mime,
        fabric_assets,
        folder_name,
    ) = await anyio.to_thread.run_sync(
        _prepare_tryon_multi_assets_sync,
        supabase,
        current.shop_id,
        hero_image_id.strip(),
        folder_id.strip(),
        [fabric_id for fabric_id, _ in fabric_pairs],
    )

    customer_bytes = await customer_photo.read()
    customer_mime = customer_photo.content_type or "image/jpeg"

    hero_bytes, hero_mime = _downscale_image_if_needed(hero_bytes, hero_mime)
    customer_bytes, customer_mime = _downscale_image_if_needed(customer_bytes, customer_mime)
    fabric_assets = [
        _downscale_image_if_needed(fabric_bytes, fabric_mime)
        for fabric_bytes, fabric_mime in fabric_assets
    ]

    fabric_assignments = [
        {"apply_to": apply_to, "image_index": idx}
        for idx, (_, apply_to) in enumerate(fabric_pairs)
    ]

    prompt = build_tryon_multi_quick_prompt(
        fabric_assignments=fabric_assignments,
        folder_name=folder_name,
    )

    image_parts = [(hero_bytes, hero_mime), *fabric_assets, (customer_bytes, customer_mime)]

    image_bytes, result_mime = await _call_gemini_tryon_async(
        prompt=prompt,
        image_parts=image_parts,
    )

    return Response(content=image_bytes, media_type=result_mime)
