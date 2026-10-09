"""Thumbnail path conventions and display-thumbnail creation.

Thumbnails are for screens only. The original files they are derived from are
never modified and remain what generation / try-on sends to Gemini.
"""
import io
from typing import Optional

import httpx

from config import get_settings

_HERO_BUCKET = "hero-images"
_DISPLAY_THUMB_MAX_EDGE = 600
_DISPLAY_THUMB_JPEG_QUALITY = 80


def look_thumbnail_path(output_path: str) -> str:
    """Thumbnail storage path for a look's output path.

    "shop/gen/output.png" -> "shop/gen/thumb.jpg"
    "shop/gen/output_v1720000000.webp" -> "shop/gen/thumb_v1720000000.jpg"
    """
    path = output_path or ""
    if "/" in path:
        directory, filename = path.rsplit("/", 1)
    else:
        directory, filename = "", path

    name = filename.rsplit(".", 1)[0] if "." in filename else filename

    if name.startswith("output"):
        name = "thumb" + name[len("output"):]
    else:
        name = f"thumb_{name}"

    thumb_filename = f"{name}.jpg"
    return f"{directory}/{thumb_filename}" if directory else thumb_filename


assert look_thumbnail_path("shop/gen/output.png") == "shop/gen/thumb.jpg"
assert look_thumbnail_path("shop/gen/output_v123.webp") == "shop/gen/thumb_v123.jpg"
assert look_thumbnail_path("output.png") == "thumb.jpg"


def hero_thumbnail_path(storage_path: str) -> str:
    """Thumbnail storage path for a hero image: same folder, "<name>_thumb.jpg".

    "shop/folder/1720000000-ab12cd34.png" -> "shop/folder/1720000000-ab12cd34_thumb.jpg"
    """
    path = storage_path or ""
    if "/" in path:
        directory, filename = path.rsplit("/", 1)
    else:
        directory, filename = "", path

    name = filename.rsplit(".", 1)[0] if "." in filename else filename
    thumb_filename = f"{name}_thumb.jpg"
    return f"{directory}/{thumb_filename}" if directory else thumb_filename


assert hero_thumbnail_path("shop/folder/hero.png") == "shop/folder/hero_thumb.jpg"
assert hero_thumbnail_path("hero") == "hero_thumb.jpg"


def make_display_thumbnail(image_bytes: bytes) -> bytes:
    """JPEG thumbnail for on-screen display: upright, longest edge <= 600px, never upscaled."""
    from PIL import Image, ImageOps

    image = Image.open(io.BytesIO(image_bytes))
    image.load()
    image = ImageOps.exif_transpose(image)

    if image.mode in ("RGBA", "LA", "P"):
        # Flatten transparency onto white; a plain convert("RGB") would turn it black.
        rgba = image.convert("RGBA")
        flattened = Image.new("RGB", rgba.size, (255, 255, 255))
        flattened.paste(rgba, mask=rgba.getchannel("A"))
        image = flattened
    else:
        image = image.convert("RGB")

    image.thumbnail(
        (_DISPLAY_THUMB_MAX_EDGE, _DISPLAY_THUMB_MAX_EDGE), Image.Resampling.LANCZOS
    )

    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=_DISPLAY_THUMB_JPEG_QUALITY)
    return buf.getvalue()


def store_hero_thumbnail(
    supabase, storage_path: str, original_bytes: Optional[bytes] = None
) -> str:
    """Create and upload the display thumbnail for a hero image. Returns its path.

    Only ever reads the original; the thumbnail is written to a separate file.
    Raises on failure - callers decide whether that matters.
    """
    if original_bytes is None:
        original_bytes = bytes(supabase.storage.from_(_HERO_BUCKET).download(storage_path))

    thumb_path = hero_thumbnail_path(storage_path)
    supabase.storage.from_(_HERO_BUCKET).upload(
        thumb_path,
        make_display_thumbnail(original_bytes),
        {"content-type": "image/jpeg", "upsert": "true"},
    )
    return thumb_path


def sign_existing_paths(
    bucket: str, paths: list[str], expires_in: int = 3600
) -> dict[str, Optional[str]]:
    """Batch-sign paths in ONE request. Paths whose file does not exist map to None.

    Calls the Storage REST endpoint directly: the Python client raises on the
    whole batch as soon as one path is missing, and for thumbnails "missing"
    is an expected answer, not an error.
    """
    result: dict[str, Optional[str]] = {path: None for path in paths}
    if not paths:
        return result

    settings = get_settings()
    base_url = f"{settings.SUPABASE_URL.rstrip('/')}/storage/v1"
    try:
        response = httpx.post(
            f"{base_url}/object/sign/{bucket}",
            headers={
                "Authorization": f"Bearer {settings.SUPABASE_SERVICE_ROLE_KEY}",
                "apikey": settings.SUPABASE_SERVICE_ROLE_KEY,
            },
            json={"paths": list(result), "expiresIn": expires_in},
            timeout=20,
        )
        response.raise_for_status()
        items = response.json()
    except Exception as exc:
        print(f"[image_thumbs] WARNING: batch sign failed for {bucket}: {exc.__class__.__name__}")
        return result

    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        path, signed_url = item.get("path"), item.get("signedURL")
        if path in result and signed_url:
            result[path] = f"{base_url}{signed_url}"
    return result


def store_hero_thumbnail_best_effort(
    supabase, storage_path: str, original_bytes: Optional[bytes] = None
) -> None:
    try:
        store_hero_thumbnail(supabase, storage_path, original_bytes)
    except Exception as exc:
        print(f"[image_thumbs] WARNING: failed to create hero thumbnail for {storage_path}: {exc}")
