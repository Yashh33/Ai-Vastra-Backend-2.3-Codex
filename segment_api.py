import base64
import io
import re
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Literal, Optional

import httpx
from cachetools import TTLCache
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator

from auth_deps import CurrentShopContext, get_current_shop_context
from config import get_settings
from supabase_client import get_supabase_admin_client
from tryon_api import _fetch_storage_bytes, _upload_generated_output

router = APIRouter(prefix="/segment", tags=["Segment"])

_FAL_SAM3_URL = "https://fal.run/fal-ai/sam-3/image"
_FAL_TIMEOUT_SECONDS = 60
_MASK_MIN_SCORE = 0.35
_MAX_MASKS = 8
_MAX_UPLOAD_BYTES = 4 * 1024 * 1024
_SIGNED_URL_TTL_SECONDS = 600
_OUTPUT_BUCKET = "generated-outputs"

_RATE_LIMIT_PER_MINUTE = 30
_rate_limit_hits: TTLCache = TTLCache(maxsize=5000, ttl=60)
_rate_limit_lock = threading.Lock()

_DATA_URL_RE = re.compile(r"^data:image/(jpeg|jpg|png|webp);base64,(.+)$", re.DOTALL)
_ALLOWED_UPLOAD_FORMATS = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}


class SegmentPoint(BaseModel):
    x: float = Field(..., ge=0, le=1)
    y: float = Field(..., ge=0, le=1)
    label: Literal[0, 1]


class SegmentRequest(BaseModel):
    source: Literal["generation", "upload"]
    generation_id: Optional[str] = None
    image_data_url: Optional[str] = None
    part_key: str = Field(..., pattern=r"^[a-z0-9_]{1,40}$")
    prompt: str = Field(..., min_length=1, max_length=60)
    points: list[SegmentPoint] = Field(default_factory=list, max_length=20)
    use_cache: bool = True

    @field_validator("prompt", mode="before")
    @classmethod
    def _strip_prompt(cls, value):
        return value.strip() if isinstance(value, str) else value


def mask_storage_prefix(shop_id: str, generation_id: str) -> str:
    return f"masks/{shop_id}/{generation_id}"


def _bad_request(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


def _enforce_rate_limit(shop_id: str) -> None:
    now = time.monotonic()
    with _rate_limit_lock:
        hits: deque = _rate_limit_hits.get(shop_id) or deque()
        while hits and now - hits[0] >= 60:
            hits.popleft()
        if len(hits) >= _RATE_LIMIT_PER_MINUTE:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many segmentation requests. Please wait a minute and try again.",
            )
        hits.append(now)
        _rate_limit_hits[shop_id] = hits


def _image_size(data: bytes) -> tuple[int, int]:
    from PIL import Image

    with Image.open(io.BytesIO(data)) as image:
        return image.size


def _load_look_generation(supabase, *, shop_id: str, generation_id: str) -> dict:
    result = (
        supabase.table("generations")
        .select("id, generation_type, output_path")
        .eq("id", generation_id)
        .eq("shop_id", shop_id)
        .limit(1)
        .execute()
    )
    rows = getattr(result, "data", None) or []
    if not rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Generation not found")

    row = rows[0]
    # Try-on results contain a customer; they must never leave for a third party.
    if row.get("generation_type") != "look":
        raise _bad_request("Only looks can be segmented")
    if not row.get("output_path"):
        raise _bad_request("This look has no output image yet")
    return row


def _load_cached_mask(supabase, *, shop_id: str, generation_id: str, part_key: str) -> Optional[bytes]:
    try:
        result = (
            supabase.table("generation_masks")
            .select("storage_path")
            .eq("shop_id", shop_id)
            .eq("generation_id", generation_id)
            .eq("part_key", part_key)
            .limit(1)
            .execute()
        )
        rows = getattr(result, "data", None) or []
        if not rows or not rows[0].get("storage_path"):
            return None
        data = supabase.storage.from_(_OUTPUT_BUCKET).download(rows[0]["storage_path"])
        return bytes(data) if isinstance(data, (bytes, bytearray)) else None
    except Exception as exc:
        print(f"[segment] WARNING: cached mask lookup failed for {generation_id}/{part_key}: {exc}")
        return None


def _signed_output_url(supabase, output_path: str) -> str:
    signed = supabase.storage.from_(_OUTPUT_BUCKET).create_signed_url(
        output_path, _SIGNED_URL_TTL_SECONDS
    )
    payload = signed if isinstance(signed, dict) else {}
    nested = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    url = (
        payload.get("signedURL")
        or payload.get("signedUrl")
        or payload.get("signed_url")
        or nested.get("signedURL")
        or nested.get("signedUrl")
    )
    if not url:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to sign look image URL",
        )
    if url.startswith("/"):
        url = f"{get_settings().SUPABASE_URL}{url}"
    return url


def _prepare_upload(image_data_url: Optional[str]) -> tuple[str, int, int]:
    """Validate an uploaded data URL. Returns (data_url_for_fal, width, height).
    The image only ever lives in memory for this request."""
    from PIL import Image, ImageOps

    match = _DATA_URL_RE.match((image_data_url or "").strip())
    if not match:
        raise _bad_request("image_data_url must be a base64 JPEG, PNG or WebP data URL")

    try:
        data = base64.b64decode(match.group(2), validate=True)
    except Exception as exc:
        raise _bad_request("image_data_url is not valid base64") from exc

    if not data or len(data) > _MAX_UPLOAD_BYTES:
        raise _bad_request("Image must be 4MB or smaller")

    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except Exception as exc:
        raise _bad_request("image_data_url is not a valid image") from exc

    mime = _ALLOWED_UPLOAD_FORMATS.get(image.format or "")
    if not mime:
        raise _bad_request("Only JPEG, PNG or WebP images are allowed")

    # Phone photos carry an EXIF rotation that browsers apply but raw pixels don't.
    # Bake it in so the mask lines up with what the user sees.
    if image.getexif().get(0x0112, 1) != 1:
        upright = ImageOps.exif_transpose(image).convert("RGB")
        buf = io.BytesIO()
        upright.save(buf, format="JPEG", quality=92)
        data, mime, size = buf.getvalue(), "image/jpeg", upright.size
    else:
        size = image.size

    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}", size[0], size[1]


def _call_fal_sam3(*, image_url: str, prompt: str, point_prompts: list[dict]) -> dict:
    fal_key = get_settings().FAL_KEY
    if not fal_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Segmentation is not configured",
        )

    payload = {
        "image_url": image_url,
        "prompt": prompt,
        "point_prompts": point_prompts,
        "apply_mask": False,
        "output_format": "png",
        "return_multiple_masks": True,
        "max_masks": _MAX_MASKS,
        "include_scores": True,
        # Masks come back inline as data URIs: no second download, nothing left on fal's CDN.
        "sync_mode": True,
    }

    try:
        response = httpx.post(
            _FAL_SAM3_URL,
            headers={"Authorization": f"Key {fal_key}"},
            json=payload,
            timeout=_FAL_TIMEOUT_SECONDS,
        )
    except httpx.TimeoutException as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Segmentation timed out"
        ) from exc
    except httpx.HTTPError as exc:
        print(f"[segment] fal request error: {exc.__class__.__name__}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Segmentation service unreachable"
        ) from exc

    if response.status_code >= 400:
        print(f"[segment] fal error status={response.status_code} body={response.text[:300]!r}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Segmentation service error ({response.status_code})",
        )

    try:
        body = response.json()
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Segmentation service returned bad data"
        ) from exc
    return body if isinstance(body, dict) else {}


def _mask_scores(body: dict, count: int) -> Optional[list[Optional[float]]]:
    scores = body.get("scores")
    if isinstance(scores, list) and len(scores) == count:
        return [float(s) if isinstance(s, (int, float)) else None for s in scores]

    metadata = body.get("metadata")
    if isinstance(metadata, list) and metadata:
        by_index = {
            m.get("index"): m.get("score")
            for m in metadata
            if isinstance(m, dict) and isinstance(m.get("score"), (int, float))
        }
        if by_index:
            return [by_index.get(i) for i in range(count)]
    return None


def _fetch_mask_bytes(url: str) -> bytes:
    if url.startswith("data:"):
        return base64.b64decode(url.split(",", 1)[1])
    response = httpx.get(url, timeout=30)
    response.raise_for_status()
    return response.content


def _union_masks(body: dict, *, width: int, height: int) -> tuple[Optional[bytes], int]:
    """Union every confident mask into one L-mode PNG at the source size.
    Returns (png_bytes or None, number of masks fal returned)."""
    from PIL import Image, ImageChops

    masks = [m for m in (body.get("masks") or []) if isinstance(m, dict) and m.get("url")]
    scores = _mask_scores(body, len(masks))

    union = None
    for index, mask in enumerate(masks):
        if scores is not None and (scores[index] is None or scores[index] < _MASK_MIN_SCORE):
            continue

        image = Image.open(io.BytesIO(_fetch_mask_bytes(mask["url"])))
        image.load()
        layer = image.convert("L")
        if "A" in image.getbands() and layer.getextrema()[0] == layer.getextrema()[1]:
            # Mask delivered as a cut-out on transparency rather than black/white.
            layer = image.getchannel("A")
        if layer.size != (width, height):
            layer = layer.resize((width, height), Image.Resampling.BILINEAR)

        union = layer if union is None else ImageChops.lighter(union, layer)

    if union is None or union.getextrema()[1] == 0:
        return None, len(masks)

    buf = io.BytesIO()
    union.save(buf, format="PNG", optimize=True)
    return buf.getvalue(), len(masks)


def _store_generation_mask(
    supabase, *, shop_id: str, generation_id: str, part_key: str, mask_png: bytes
) -> None:
    storage_path = f"{mask_storage_prefix(shop_id, generation_id)}/{part_key}.png"
    try:
        _upload_generated_output(
            supabase,
            bucket=_OUTPUT_BUCKET,
            path=storage_path,
            data=mask_png,
            content_type="image/png",
        )
        supabase.table("generation_masks").upsert(
            {
                "shop_id": shop_id,
                "generation_id": generation_id,
                "part_key": part_key,
                "storage_path": storage_path,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
            on_conflict="generation_id,part_key",
        ).execute()
    except Exception as exc:
        # The caller still gets its mask; it just won't be cached for next time.
        print(f"[segment] WARNING: failed to cache mask {storage_path}: {exc}")


def _mask_response(mask_png: bytes, *, width: int, height: int, cached: bool, part_key: str) -> dict:
    return {
        "mask_png_base64": base64.b64encode(mask_png).decode("ascii"),
        "width": width,
        "height": height,
        "cached": cached,
        "part_key": part_key,
    }


@router.post("")
def segment_image(
    body: SegmentRequest,
    current: CurrentShopContext = Depends(get_current_shop_context),
):
    supabase = get_supabase_admin_client()
    shop_id = current.shop_id
    generation_id: Optional[str] = None

    if body.source == "generation":
        generation_id = (body.generation_id or "").strip()
        if not generation_id:
            raise _bad_request("generation_id is required when source is 'generation'")

        generation = _load_look_generation(supabase, shop_id=shop_id, generation_id=generation_id)

        if body.use_cache and not body.points:
            cached = _load_cached_mask(
                supabase, shop_id=shop_id, generation_id=generation_id, part_key=body.part_key
            )
            if cached:
                try:
                    cached_width, cached_height = _image_size(cached)
                    return _mask_response(
                        cached,
                        width=cached_width,
                        height=cached_height,
                        cached=True,
                        part_key=body.part_key,
                    )
                except Exception as exc:
                    print(f"[segment] WARNING: unreadable cached mask for {generation_id}: {exc}")

        output_path = generation["output_path"]
        width, height = _image_size(_fetch_storage_bytes(supabase, _OUTPUT_BUCKET, output_path))
        image_url = _signed_output_url(supabase, output_path)
    else:
        image_url, width, height = _prepare_upload(body.image_data_url)

    point_prompts = [
        {
            "x": min(width - 1, max(0, round(point.x * width))),
            "y": min(height - 1, max(0, round(point.y * height))),
            "label": point.label,
            "object_id": 1,
        }
        for point in body.points
    ]

    _enforce_rate_limit(shop_id)

    started = time.monotonic()
    mask_count = 0
    try:
        fal_body = _call_fal_sam3(image_url=image_url, prompt=body.prompt, point_prompts=point_prompts)
        try:
            mask_png, mask_count = _union_masks(fal_body, width=width, height=height)
        except Exception as exc:
            print(f"[segment] failed to read fal masks: {exc.__class__.__name__}: {exc}")
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Segmentation service returned unreadable masks",
            ) from exc
    finally:
        print(
            f"[segment] fal call shop_id={shop_id} source={body.source} part_key={body.part_key} "
            f"had_points={bool(point_prompts)} duration_ms={round((time.monotonic() - started) * 1000)} "
            f"mask_count={mask_count}"
        )

    if not mask_png:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Nothing found for '{body.prompt}'. Tap + on the garment and try again.",
        )

    if generation_id:
        _store_generation_mask(
            supabase,
            shop_id=shop_id,
            generation_id=generation_id,
            part_key=body.part_key,
            mask_png=mask_png,
        )

    return _mask_response(mask_png, width=width, height=height, cached=False, part_key=body.part_key)
