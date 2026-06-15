import base64
import io
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from auth_deps import CurrentShopContext, get_current_shop_context
from config import get_settings
from prompting import build_tryon_prompt, build_tryon_quick_prompt
from supabase_client import get_supabase_admin_client

from google import genai
from google.genai import types

router = APIRouter(prefix="/tryon", tags=["TryOn"])


class TryOnRequest(BaseModel):
    generation_id: str = Field(..., min_length=1)
    customer_photo_b64: str = Field(..., min_length=1)
    customer_photo_mime: str = Field(
        default="image/jpeg",
        min_length=1
    )


class TryOnQuickRequest(BaseModel):
    fabric_image_id: str = Field(..., min_length=1)
    folder_id: str = Field(..., min_length=1)
    customer_photo_b64: str = Field(..., min_length=1)
    customer_photo_mime: str = Field(
        default="image/jpeg",
        min_length=1
    )


class TryOnResponse(BaseModel):
    result_b64: str
    result_mime: str


def _decode_photo(b64: str) -> bytes:
    try:
        return base64.b64decode(b64)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid base64 for customer photo",
        )


def _fetch_storage_bytes(supabase, bucket: str, path: str) -> bytes:
    try:
        result = supabase.storage.from_(bucket).download(path)
        if isinstance(result, (bytes, bytearray)):
            return bytes(result)
        raise RuntimeError("Unexpected storage response type")
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Storage fetch failed: {exc}",
        )


def _call_gemini_tryon(
    prompt: str,
    image_parts: list[tuple[bytes, str]],
) -> TryOnResponse:
    settings = get_settings()
    client = genai.Client(api_key=settings.GEMINI_API_KEY)

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


@router.post("/", response_model=TryOnResponse)
def tryon(
    body: TryOnRequest,
    current: CurrentShopContext = Depends(get_current_shop_context),
):
    """
    Approach B - Stage 2 only.
    Takes an existing generation output + customer photo.
    Calls Gemini with: customer_photo + garment_image.
    Returns composite image as base64. Nothing is stored.
    """
    supabase = get_supabase_admin_client()

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
    folder_id = gen.get("folder_id")
    if folder_id:
        folder_result = (
            supabase.table("hero_folders")
            .select("name")
            .eq("id", folder_id)
            .limit(1)
            .execute()
        )
        folder_rows = getattr(folder_result, "data", None) or []
        if folder_rows:
            folder_name = folder_rows[0].get("name")

    # Download garment image (Stage 1 output)
    garment_bytes = _fetch_storage_bytes(
        supabase,
        "generated-outputs",
        gen["output_path"],
    )

    # Decode customer photo from base64 (never stored)
    customer_bytes = _decode_photo(body.customer_photo_b64)

    # Build prompt
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
    current: CurrentShopContext = Depends(get_current_shop_context),
):
    """
    Approach A - all three images in one call.
    Takes fabric_image_id + folder_id + customer photo.
    Calls Gemini with: hero_image + fabric_image + customer_photo.
    Returns composite image as base64. Nothing is stored.
    """
    supabase = get_supabase_admin_client()
    shop_id = current.shop_id

    # Fetch folder -> default hero image
    folder_result = (
        supabase.table("hero_folders")
        .select("id, name, default_hero_image_id")
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

    # Fetch fabric image storage path
    fabric_result = (
        supabase.table("fabric_images")
        .select("id, storage_path, mime_type")
        .eq("id", body.fabric_image_id.strip())
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

    # Download hero and fabric images from storage
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

    # Decode customer photo (never stored)
    customer_bytes = _decode_photo(body.customer_photo_b64)

    # Build combined prompt
    prompt = build_tryon_quick_prompt(folder_name=folder_name)

    # Call Gemini: hero first, fabric second, customer third
    return _call_gemini_tryon(
        prompt=prompt,
        image_parts=[
            (hero_bytes, hero.get("mime_type") or "image/jpeg"),
            (fabric_bytes, fabric.get("mime_type") or "image/jpeg"),
            (customer_bytes, body.customer_photo_mime),
        ],
    )
