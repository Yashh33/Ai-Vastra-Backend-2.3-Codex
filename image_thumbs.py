"""Thumbnail path conventions and display-thumbnail creation.

Thumbnails are for screens only. The original files they are derived from are
never modified and remain what generation / try-on sends to Gemini.
"""
import io
from typing import Optional

import httpx

from config import get_settings
from supabase_client import retry_once

_HERO_BUCKET = "hero-images"
_DISPLAY_THUMB_MAX_EDGE = 600
_DISPLAY_THUMB_JPEG_QUALITY = 80

GENERATED_OUTPUTS_BUCKET = "generated-outputs"
FABRIC_BUCKET = "fabric-images"

# Light "view copy" of a look: what apps show full screen instead of the original.
_VIEW_MAX_EDGE = 1440
_VIEW_JPEG_QUALITY = 82
_VIEW_SKIP_MAX_BYTES = 400 * 1024  # a JPEG this small (and <= 1440 px) needs no view copy

_FABRIC_THUMB_MAX_EDGE = 400
_FABRIC_THUMB_JPEG_QUALITY = 72

# Files whose path never gets different content (look output / thumbnail / view copy, hero and
# fabric thumbnails): phones may keep them for a year. storage3 2.28 takes the number of
# seconds as a string under "cache-control" and sends it as "max-age=<seconds>".
IMMUTABLE_CACHE_SECONDS = "31536000"


def immutable_upload_options(content_type: str, *, upsert: bool) -> dict:
    """Fresh upload options for a never-changing file (storage3 pops keys from the dict it gets)."""
    return {
        "content-type": content_type,
        "upsert": "true" if upsert else "false",
        "cache-control": IMMUTABLE_CACHE_SECONDS,
    }


def is_already_exists_error(exc: BaseException) -> bool:
    """Storage refused an upload with upsert off because the file is already there."""
    for attr in ("status", "code", "statusCode"):
        if str(getattr(exc, attr, "") or "") == "409":
            return True
    text = str(exc).lower()
    return "already exists" in text or "duplicate" in text or "'statuscode': 409" in text


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


def look_view_path(output_path: str) -> str:
    """View-copy storage path for a look's output path: the look_thumbnail_path rule with
    "view" instead of "thumb", always .jpg.

    "shop/gen/output_v123.jpg"     -> "shop/gen/view_v123.jpg"
    "shop/gen/output-cc-17000.jpg" -> "shop/gen/view-cc-17000.jpg"
    "shop/gen/output.png"          -> "shop/gen/view.jpg"
    "shop/gen/other.png"           -> "shop/gen/view_other.jpg"
    """
    path = output_path or ""
    if "/" in path:
        directory, filename = path.rsplit("/", 1)
    else:
        directory, filename = "", path

    name = filename.rsplit(".", 1)[0] if "." in filename else filename

    if name.startswith("output"):
        name = "view" + name[len("output"):]
    else:
        name = f"view_{name}"

    view_filename = f"{name}.jpg"
    return f"{directory}/{view_filename}" if directory else view_filename


def fabric_thumbnail_path(storage_path: str) -> str:
    """Thumbnail storage path for a fabric image: same folder, "<name>_thumb.jpg"
    (the hero_thumbnail_path rule)."""
    return hero_thumbnail_path(storage_path)


def _upright_rgb(image_bytes: bytes):
    """Open, apply the EXIF rotation, flatten any transparency onto white. Returns an RGB image."""
    from PIL import Image, ImageOps

    image = Image.open(io.BytesIO(image_bytes))
    image.load()
    image = ImageOps.exif_transpose(image)

    if image.mode in ("RGBA", "LA", "P", "PA") or "transparency" in image.info:
        # Flatten transparency onto white; a plain convert("RGB") would turn it black.
        rgba = image.convert("RGBA")
        flattened = Image.new("RGB", rgba.size, (255, 255, 255))
        flattened.paste(rgba, mask=rgba.getchannel("A"))
        return flattened
    return image.convert("RGB")


def make_look_view_copy(image_bytes: bytes) -> Optional[bytes]:
    """Light JPEG for showing a look full screen: upright, transparency on white, longest edge
    <= 1440 px (never upscaled), quality 82, progressive, optimised.

    None = no view copy needed: the original is already a JPEG of <= 400 KB and <= 1440 px."""
    from PIL import Image

    with Image.open(io.BytesIO(image_bytes)) as probe:
        if (
            probe.format == "JPEG"
            and len(image_bytes) <= _VIEW_SKIP_MAX_BYTES
            and max(probe.size) <= _VIEW_MAX_EDGE
        ):
            return None

    image = _upright_rgb(image_bytes)
    image.thumbnail((_VIEW_MAX_EDGE, _VIEW_MAX_EDGE), Image.Resampling.LANCZOS)

    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=_VIEW_JPEG_QUALITY, progressive=True, optimize=True)
    return buf.getvalue()


def make_fabric_thumbnail(image_bytes: bytes) -> bytes:
    """JPEG thumbnail for a fabric photo: upright, longest edge <= 400 px (never upscaled), quality 72."""
    from PIL import Image

    image = _upright_rgb(image_bytes)
    image.thumbnail((_FABRIC_THUMB_MAX_EDGE, _FABRIC_THUMB_MAX_EDGE), Image.Resampling.LANCZOS)

    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=_FABRIC_THUMB_JPEG_QUALITY, optimize=True)
    return buf.getvalue()


def upload_new_file(supabase, bucket: str, path: str, data: bytes, content_type: str = "image/jpeg") -> None:
    """Upload a never-changing file with upsert OFF (an existing file is never overwritten) and
    the one-year cache lifetime. "Already exists" counts as done. Raises on any other failure."""
    try:
        retry_once(
            lambda: supabase.storage.from_(bucket).upload(
                path, data, immutable_upload_options(content_type, upsert=False)
            )
        )
    except Exception as exc:
        if is_already_exists_error(exc) or is_already_exists_error(exc.__cause__ or exc):
            return
        raise


def store_look_view_copy(supabase, output_path: str, image_bytes: bytes) -> Optional[str]:
    """Create and upload the view copy for a look's output. Returns its path, or None when the
    original is already light enough. Never overwrites. Raises on failure."""
    view_bytes = make_look_view_copy(image_bytes)
    if view_bytes is None:
        return None
    view_path = look_view_path(output_path)
    upload_new_file(supabase, GENERATED_OUTPUTS_BUCKET, view_path, view_bytes)
    return view_path


def store_look_view_copy_best_effort(
    supabase, output_path: str, image_bytes: bytes, *, log_prefix: str = "[image_thumbs]"
) -> Optional[str]:
    """store_look_view_copy that never raises: a failure is logged and never fails the look."""
    try:
        view_path = store_look_view_copy(supabase, output_path, image_bytes)
        if view_path:
            print(f"{log_prefix} uploaded view copy: {view_path}")
        return view_path
    except Exception as exc:
        print(f"{log_prefix} WARNING: failed to create view copy for {output_path}: {exc.__class__.__name__}: {exc}")
        return None


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
        immutable_upload_options("image/jpeg", upsert=True),
    )
    return thumb_path


def sign_existing_paths(
    bucket: str, paths: list[str], expires_in: int = 3600, *, raise_on_error: bool = False
) -> dict[str, Optional[str]]:
    """Batch-sign paths in ONE request. Paths whose file does not exist map to None.

    Calls the Storage REST endpoint directly: the Python client raises on the
    whole batch as soon as one path is missing, and for thumbnails "missing"
    is an expected answer, not an error.

    raise_on_error: raise when the request itself fails, instead of answering "all missing"
    (for callers that would otherwise act on a false "missing").
    """
    result: dict[str, Optional[str]] = {path: None for path in paths}
    if not paths:
        return result

    settings = get_settings()
    base_url = f"{settings.SUPABASE_URL.rstrip('/')}/storage/v1"
    try:
        # Signing is safe to repeat: one retry on a dropped connection.
        response = retry_once(
            lambda: httpx.post(
                f"{base_url}/object/sign/{bucket}",
                headers={
                    "Authorization": f"Bearer {settings.SUPABASE_SERVICE_ROLE_KEY}",
                    "apikey": settings.SUPABASE_SERVICE_ROLE_KEY,
                },
                json={"paths": list(result), "expiresIn": expires_in},
                timeout=20,
            )
        )
        response.raise_for_status()
        items = response.json()
    except Exception as exc:
        print(f"[image_thumbs] WARNING: batch sign failed for {bucket}: {exc.__class__.__name__}")
        if raise_on_error:
            raise
        return result

    if raise_on_error and not isinstance(items, list):
        raise RuntimeError(f"batch sign for {bucket}: unexpected answer")

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
