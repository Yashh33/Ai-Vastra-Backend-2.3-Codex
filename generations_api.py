import io
import time
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from auth_deps import CurrentShopContext, get_current_shop_context
from config import get_settings
from prompting import build_generation_prompt
from supabase_client import get_supabase_admin_client

router = APIRouter(prefix="/generations", tags=["Generations"])

_ALLOWED_STATUSES = {"queued", "processing", "done", "failed"}
_ALLOWED_APPLY_TO = {"shirt", "pant", "suit_full_body", "suit_upper", "koti"}


def derive_thumb_path(output_path: str) -> str:
    """Derive the thumbnail storage path for an output path.

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


# Inline sanity checks for derive_thumb_path (see VERIFY step in task):
assert derive_thumb_path("shop/gen/output.png") == "shop/gen/thumb.jpg"
assert derive_thumb_path("shop/gen/output_v123.webp") == "shop/gen/thumb_v123.jpg"
assert derive_thumb_path("output.png") == "thumb.jpg"


def _guess_extension_from_mime(mime_type: str) -> str:
    mime = (mime_type or "").lower()
    if "png" in mime:
        return "png"
    if "jpeg" in mime or "jpg" in mime:
        return "jpg"
    if "webp" in mime:
        return "webp"
    return "png"


def _extract_signed_url(payload: object) -> Optional[str]:
    if isinstance(payload, dict):
        nested = payload.get("data")
        nested = nested if isinstance(nested, dict) else {}
        return (
            payload.get("signedURL")
            or payload.get("signedUrl")
            or payload.get("signed_url")
            or nested.get("signedURL")
            or nested.get("signedUrl")
            or nested.get("signed_url")
        )
    return None


def _build_signed_url_map(signed_items: object, settings) -> dict[str, str]:
    result: dict[str, str] = {}
    if not isinstance(signed_items, list):
        return result

    for item in signed_items:
        if not isinstance(item, dict) or item.get("error"):
            continue

        path = item.get("path") or item.get("Path")
        signed_url = _extract_signed_url(item)
        if not path or not signed_url:
            continue

        if signed_url.startswith("/"):
            signed_url = f"{settings.SUPABASE_URL}{signed_url}"

        result[path] = signed_url

    return result


def _sign_paths_with_fallback(
    supabase,
    settings,
    output_paths: list[str],
    thumb_paths: list[str],
    expires_in: int,
) -> dict[str, str]:
    """Sign all given paths, tolerating per-item and whole-batch failures.

    Tries one batch create_signed_urls call first. If that call itself
    raises (e.g. the storage backend errors out because some of the
    paths - typically legacy thumbnails - don't exist), falls back to
    signing paths one-by-one so a handful of missing objects can never
    blank out every row.
    """
    unique_paths = sorted(set(output_paths) | set(thumb_paths))
    if not unique_paths:
        return {}

    try:
        signed_items = (
            supabase.storage.from_("generated-outputs")
            .create_signed_urls(unique_paths, expires_in)
        )
        return _build_signed_url_map(signed_items, settings)
    except Exception as exc:
        print(
            "[include_urls] batch create_signed_urls failed, "
            f"falling back to per-path signing: {exc}"
        )

    url_by_path: dict[str, str] = {}
    seen: set[str] = set()
    ordered_paths: list[str] = []
    for path in output_paths + thumb_paths:
        if path not in seen:
            seen.add(path)
            ordered_paths.append(path)

    for path in ordered_paths:
        try:
            signed = (
                supabase.storage.from_("generated-outputs")
                .create_signed_url(path, expires_in)
            )
        except Exception as exc:
            print(f"[include_urls] failed to sign path {path!r}: {exc}")
            continue

        signed_url = _extract_signed_url(signed)
        if not signed_url:
            continue

        if signed_url.startswith("/"):
            signed_url = f"{settings.SUPABASE_URL}{signed_url}"

        url_by_path[path] = signed_url

    return url_by_path


def _attach_download_and_thumb_urls(
    supabase, rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    if not rows:
        return rows

    output_paths: list[str] = []
    thumb_paths: list[str] = []
    for row in rows:
        row_status = str(row.get("status") or "")
        output_path = str(row.get("output_path") or "").strip()
        if row_status == "done" and output_path:
            output_paths.append(output_path)
            thumb_paths.append(derive_thumb_path(output_path))

    if not output_paths:
        for row in rows:
            row["download_url"] = None
            row["thumb_url"] = None
        return rows

    settings = get_settings()
    try:
        url_by_path = _sign_paths_with_fallback(
            supabase, settings, output_paths, thumb_paths, 3600
        )
    except Exception as exc:
        print(f"[include_urls] unexpected failure signing URLs: {exc}")
        url_by_path = {}

    for row in rows:
        row_status = str(row.get("status") or "")
        output_path = str(row.get("output_path") or "").strip()
        if row_status == "done" and output_path:
            download_url = url_by_path.get(output_path)
            thumb_url = url_by_path.get(derive_thumb_path(output_path)) or download_url
            row["download_url"] = download_url
            row["thumb_url"] = thumb_url
        else:
            row["download_url"] = None
            row["thumb_url"] = None

    return rows


class GenerationDownloadUrlsRequest(BaseModel):
    generation_ids: list[str] = Field(default_factory=list)


class GenerationCreateFabricItem(BaseModel):
    fabric_image_id: str = Field(..., min_length=1)
    apply_to: str = Field(..., min_length=1)
    fabric_code: Optional[str] = Field(default=None, min_length=1, max_length=120)
    fabric_color: Optional[str] = Field(default=None, min_length=1, max_length=120)
    fabric_scale: Optional[str] = Field(
        default=None,
        description="Pattern scale hint: 'fine', 'medium', or 'bold'",
    )


class GenerationCreateRequest(BaseModel):
    hero_image_id: str = Field(..., min_length=1)
    # Backward-compatible single-fabric field (older clients).
    fabric_image_id: Optional[str] = Field(default=None, min_length=1)
    fabrics: list[GenerationCreateFabricItem] = Field(default_factory=list)


class GenerationMatchColorEdit(BaseModel):
    selected_hex: str = Field(..., min_length=4, max_length=9)
    hue_shift_degrees: float = Field(default=0, ge=-180, le=180)
    saturation_delta_percent: float = Field(default=0, ge=-100, le=100)
    lightness_delta_percent: float = Field(default=0, ge=-100, le=100)


class GenerationMatchColorRequest(BaseModel):
    edits: list[GenerationMatchColorEdit] = Field(default_factory=list)
    # Backward-compatible single-edit fields (older clients).
    selected_hex: Optional[str] = Field(default=None, min_length=4, max_length=9)
    hue_shift_degrees: float = Field(default=0, ge=-180, le=180)
    saturation_delta_percent: float = Field(default=0, ge=-100, le=100)
    lightness_delta_percent: float = Field(default=0, ge=-100, le=100)


def _clean_id(value: str, field_name: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{field_name} cannot be blank",
        )
    return cleaned



def _clean_optional_text(value: Optional[str], max_length: int = 120) -> Optional[str]:
    if value is None:
        return None

    cleaned = value.strip()
    if not cleaned:
        return None

    return cleaned[:max_length]


def _format_apply_to_label(value: str) -> str:
    mapping = {
        "shirt": "Shirt",
        "pant": "Pant",
        "suit_full_body": "Suit-Full Body",
        "suit_upper": "Suit-Upper",
        "koti": "Koti",
    }
    key = (value or "").strip().lower()
    return mapping.get(key, value or "Garment")


def _build_fabric_summary_label(fabrics: list[dict[str, Any]]) -> Optional[str]:
    if not fabrics:
        return None

    parts: list[str] = []
    ordered = sorted(fabrics, key=lambda item: int(item.get("sort_order") or 0))

    for item in ordered:
        apply_to_label = _format_apply_to_label(str(item.get("apply_to") or ""))
        fabric_code = _clean_optional_text(item.get("fabric_code"))

        if fabric_code:
            parts.append(f"{apply_to_label}: {fabric_code}")
        elif apply_to_label:
            parts.append(apply_to_label)

    if not parts:
        return None

    return " | ".join(parts)


def _attach_generation_fabric_metadata(
    *,
    supabase,
    shop_id: str,
    generation_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not generation_rows:
        return generation_rows

    generation_ids = [str(row.get("id") or "").strip() for row in generation_rows]
    generation_ids = [value for value in generation_ids if value]
    if not generation_ids:
        return generation_rows

    try:
        result = (
            supabase.table("generation_fabrics")
            .select("generation_id, apply_to, sort_order, fabric_code, fabric_color")
            .eq("shop_id", shop_id)
            .in_("generation_id", generation_ids)
            .order("sort_order", desc=False)
            .execute()
        )
        fabric_rows = getattr(result, "data", None) or []
    except Exception as exc:
        # Backward-compatible fallback if DB columns are not migrated yet.
        message = str(exc).lower()
        if "fabric_code" in message or "fabric_color" in message:
            try:
                fallback_result = (
                    supabase.table("generation_fabrics")
                    .select("generation_id, apply_to, sort_order")
                    .eq("shop_id", shop_id)
                    .in_("generation_id", generation_ids)
                    .order("sort_order", desc=False)
                    .execute()
                )
                fallback_rows = getattr(fallback_result, "data", None) or []
                fabric_rows = [
                    {
                        **row,
                        "fabric_code": None,
                        "fabric_color": None,
                    }
                    for row in fallback_rows
                ]
            except Exception:
                return generation_rows
        else:
            return generation_rows

    by_generation: dict[str, list[dict[str, Any]]] = {}
    for item in fabric_rows:
        generation_id = str(item.get("generation_id") or "").strip()
        if not generation_id:
            continue
        by_generation.setdefault(generation_id, []).append(item)

    enriched: list[dict[str, Any]] = []
    for row in generation_rows:
        generation_id = str(row.get("id") or "").strip()
        fabrics = by_generation.get(generation_id, [])

        next_row = dict(row)
        if fabrics:
            next_row["generation_fabrics"] = fabrics
            summary = _build_fabric_summary_label(fabrics)
            if summary:
                next_row["fabric_summary_label"] = summary

        enriched.append(next_row)

    return enriched


def _persist_generation_fabric_metadata(
    *,
    supabase,
    shop_id: str,
    generation_id: str,
    normalized_fabrics: list[dict[str, Any]],
) -> Optional[str]:
    if not normalized_fabrics:
        return None

    try:
        for item in normalized_fabrics:
            payload = {
                "fabric_code": _clean_optional_text(item.get("fabric_code")),
                "fabric_color": _clean_optional_text(item.get("fabric_color")),
            }

            (
                supabase.table("generation_fabrics")
                .update(payload)
                .eq("generation_id", generation_id)
                .eq("shop_id", shop_id)
                .eq("fabric_image_id", item["fabric_image_id"])
                .eq("apply_to", item["apply_to"])
                .execute()
            )
    except Exception as exc:
        return f"Generation queued, but fabric metadata could not be saved: {exc}"

    return None


def _find_generation_ids_by_fabric_filters(
    *,
    supabase,
    shop_id: str,
    fabric_code: Optional[str],
    fabric_color: Optional[str],
) -> Optional[list[str]]:
    code_filter = _clean_optional_text(fabric_code)
    color_filter = _clean_optional_text(fabric_color)

    if not code_filter and not color_filter:
        return None

    query = (
        supabase.table("generation_fabrics")
        .select("generation_id")
        .eq("shop_id", shop_id)
    )

    if code_filter:
        query = query.ilike("fabric_code", f"%{code_filter}%")

    if color_filter:
        query = query.ilike("fabric_color", f"%{color_filter}%")

    query = query.limit(5000)

    try:
        result = query.execute()
    except Exception as exc:
        message = str(exc).lower()
        if "fabric_code" in message or "fabric_color" in message:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=(
                    "Fabric filter columns are missing in database. "
                    "Run migration for generation_fabrics.fabric_code/fabric_color."
                ),
            ) from exc

        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to filter generations by fabric metadata",
        ) from exc

    rows = getattr(result, "data", None) or []
    seen: set[str] = set()
    generation_ids: list[str] = []

    for row in rows:
        generation_id = str(row.get("generation_id") or "").strip()
        if not generation_id or generation_id in seen:
            continue

        seen.add(generation_id)
        generation_ids.append(generation_id)

    return generation_ids

def _load_folder_prompt_context_for_hero_image(
    *,
    supabase,
    shop_id: str,
    hero_image_id: str,
) -> dict:
    try:
        hero_result = (
            supabase.table("hero_images")
            .select("id, folder_id")
            .eq("id", hero_image_id)
            .eq("shop_id", shop_id)
            .limit(1)
            .execute()
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch hero image metadata",
        ) from exc

    hero_rows = getattr(hero_result, "data", None) or []
    if not hero_rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Hero image not found for this shop",
        )

    folder_id = str(hero_rows[0]["folder_id"])

    try:
        folder_result = (
            supabase.table("garment_types")
            .select("id, name, prompt_template")
            .eq("id", folder_id)
            .eq("shop_id", shop_id)
            .limit(1)
            .execute()
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch folder prompt template",
        ) from exc

    folder_rows = getattr(folder_result, "data", None) or []
    if not folder_rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Folder not found for the selected hero image",
        )

    return folder_rows[0]


def _normalize_generation_fabrics(body: GenerationCreateRequest) -> list[dict[str, Any]]:
    raw_fabrics = list(body.fabrics or [])
    is_legacy_single = False

    if not raw_fabrics and body.fabric_image_id:
        # Backward-compatible path: treat single legacy fabric as full-body garment material.
        is_legacy_single = True
        raw_fabrics = [
            GenerationCreateFabricItem(
                fabric_image_id=body.fabric_image_id,
                apply_to="suit_full_body",
                fabric_code=None,
                fabric_color=None,
            )
        ]

    if not raw_fabrics:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="At least one fabric is required",
        )

    if len(raw_fabrics) > 4:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="At most 4 fabrics are allowed per generation",
        )

    normalized: list[dict[str, Any]] = []
    seen_apply_to: set[str] = set()

    for item in raw_fabrics:
        fabric_image_id = _clean_id(item.fabric_image_id, "fabric_image_id")
        apply_to = (item.apply_to or "").strip().lower()
        if apply_to not in _ALLOWED_APPLY_TO:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "Invalid apply_to value. Allowed values: "
                    "shirt, pant, suit_full_body, suit_upper, koti"
                ),
            )

        if apply_to in seen_apply_to:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Duplicate apply_to value: {apply_to}",
            )
        seen_apply_to.add(apply_to)

        fabric_code = _clean_optional_text(item.fabric_code) or "unknown"

        normalized.append(
            {
                "fabric_image_id": fabric_image_id,
                "apply_to": apply_to,
                "fabric_code": fabric_code,
                "fabric_color": _clean_optional_text(item.fabric_color),
                "fabric_scale": _clean_optional_text(item.fabric_scale),
            }
        )

    if len(normalized) > 1 and any(item["apply_to"] == "suit_full_body" for item in normalized):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="suit_full_body cannot be combined with other apply_to values",
        )

    return normalized


def _normalize_hex_color(value: str) -> str:
    raw = (value or "").strip()
    if not raw.startswith("#"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="selected_hex must be a hex color (e.g. #AABBCC)",
        )

    hex_part = raw[1:]
    if len(hex_part) == 3:
        if not all(ch in "0123456789abcdefABCDEF" for ch in hex_part):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="selected_hex is invalid",
            )
        return "#" + "".join(ch * 2 for ch in hex_part).upper()

    if len(hex_part) == 6 and all(ch in "0123456789abcdefABCDEF" for ch in hex_part):
        return f"#{hex_part.upper()}"

    if len(hex_part) == 8 and all(ch in "0123456789abcdefABCDEF" for ch in hex_part):
        # Accept #AARRGGBB and drop alpha for matching.
        return f"#{hex_part[2:].upper()}"

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="selected_hex is invalid",
    )


def _hex_to_rgb_tuple(hex_color: str) -> tuple[int, int, int]:
    normalized = _normalize_hex_color(hex_color)
    value = normalized[1:]
    return (
        int(value[0:2], 16),
        int(value[2:4], 16),
        int(value[4:6], 16),
    )


def _normalize_match_color_edits(body: GenerationMatchColorRequest) -> list[dict]:
    raw_edits = list(body.edits or [])

    if not raw_edits and body.selected_hex:
        raw_edits = [
            GenerationMatchColorEdit(
                selected_hex=body.selected_hex,
                hue_shift_degrees=body.hue_shift_degrees,
                saturation_delta_percent=body.saturation_delta_percent,
                lightness_delta_percent=body.lightness_delta_percent,
            )
        ]

    if not raw_edits:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No match-color edits provided",
        )

    if len(raw_edits) > 24:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Too many match-color edits in one request",
        )

    normalized: list[dict] = []
    for item in raw_edits:
        selected_hex = _normalize_hex_color(item.selected_hex)
        hue_shift_degrees = float(item.hue_shift_degrees)
        saturation_delta_percent = float(item.saturation_delta_percent)
        lightness_delta_percent = float(item.lightness_delta_percent)

        is_noop = (
            abs(hue_shift_degrees) < 1e-6
            and abs(saturation_delta_percent) < 1e-6
            and abs(lightness_delta_percent) < 1e-6
        )
        if is_noop:
            continue

        normalized.append(
            {
                "selected_hex": selected_hex,
                "hue_shift_degrees": hue_shift_degrees,
                "saturation_delta_percent": saturation_delta_percent,
                "lightness_delta_percent": lightness_delta_percent,
            }
        )

    if not normalized:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="All match-color edits were empty (no changes)",
        )

    return normalized


def _import_match_color_dependencies():
    try:
        import numpy as np  # type: ignore
        from PIL import Image  # type: ignore
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                "Match Color backend dependencies are missing. "
                "Install pillow and numpy in the backend environment."
            ),
        ) from exc

    return np, Image


def _rgb_to_hsl_numpy(rgb, np):
    # rgb shape: (..., 3), float32 in [0, 1]
    r = rgb[..., 0]
    g = rgb[..., 1]
    b = rgb[..., 2]

    max_c = np.maximum(np.maximum(r, g), b)
    min_c = np.minimum(np.minimum(r, g), b)
    delta = max_c - min_c

    l = (max_c + min_c) / 2.0
    s = np.zeros_like(l)
    h = np.zeros_like(l)

    non_gray = delta > 1e-8
    denom = 1.0 - np.abs(2.0 * l - 1.0)
    valid_sat = non_gray & (denom > 1e-8)
    s[valid_sat] = delta[valid_sat] / denom[valid_sat]

    r_is_max = non_gray & (max_c == r)
    g_is_max = non_gray & (max_c == g)
    b_is_max = non_gray & (max_c == b)

    h[r_is_max] = np.mod(((g[r_is_max] - b[r_is_max]) / delta[r_is_max]), 6.0)
    h[g_is_max] = ((b[g_is_max] - r[g_is_max]) / delta[g_is_max]) + 2.0
    h[b_is_max] = ((r[b_is_max] - g[b_is_max]) / delta[b_is_max]) + 4.0
    h = np.mod(h / 6.0, 1.0)

    return h, np.clip(s, 0.0, 1.0), np.clip(l, 0.0, 1.0)


def _hsl_to_rgb_numpy(h, s, l, np):
    s = np.clip(s, 0.0, 1.0)
    l = np.clip(l, 0.0, 1.0)
    h = np.mod(h, 1.0)

    c = (1.0 - np.abs(2.0 * l - 1.0)) * s
    h6 = h * 6.0
    x = c * (1.0 - np.abs(np.mod(h6, 2.0) - 1.0))
    m = l - c / 2.0

    zeros = np.zeros_like(h)

    r1 = np.select(
        [
            (h6 >= 0) & (h6 < 1),
            (h6 >= 1) & (h6 < 2),
            (h6 >= 2) & (h6 < 3),
            (h6 >= 3) & (h6 < 4),
            (h6 >= 4) & (h6 < 5),
            (h6 >= 5) & (h6 < 6),
        ],
        [c, x, zeros, zeros, x, c],
        default=zeros,
    )
    g1 = np.select(
        [
            (h6 >= 0) & (h6 < 1),
            (h6 >= 1) & (h6 < 2),
            (h6 >= 2) & (h6 < 3),
            (h6 >= 3) & (h6 < 4),
            (h6 >= 4) & (h6 < 5),
            (h6 >= 5) & (h6 < 6),
        ],
        [x, c, c, x, zeros, zeros],
        default=zeros,
    )
    b1 = np.select(
        [
            (h6 >= 0) & (h6 < 1),
            (h6 >= 1) & (h6 < 2),
            (h6 >= 2) & (h6 < 3),
            (h6 >= 3) & (h6 < 4),
            (h6 >= 4) & (h6 < 5),
            (h6 >= 5) & (h6 < 6),
        ],
        [zeros, zeros, x, c, c, x],
        default=zeros,
    )

    r = np.clip(r1 + m, 0.0, 1.0)
    g = np.clip(g1 + m, 0.0, 1.0)
    b = np.clip(b1 + m, 0.0, 1.0)

    return np.stack([r, g, b], axis=-1)


def _smoothstep_numpy(edge0, edge1, x, np):
    denom = max(edge1 - edge0, 1e-8)
    t = np.clip((x - edge0) / denom, 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def _detect_output_format(output_path: str) -> tuple[str, str]:
    path = (output_path or "").strip().lower()
    if path.endswith(".jpg") or path.endswith(".jpeg"):
        return "JPEG", "image/jpeg"
    if path.endswith(".webp"):
        return "WEBP", "image/webp"
    return "PNG", "image/png"


def _apply_match_color_full_res(
    image_bytes: bytes,
    *,
    output_path: str,
    edits: list[dict],
) -> tuple[bytes, str]:
    np, Image = _import_match_color_dependencies()

    image = Image.open(io.BytesIO(image_bytes))
    image.load()

    has_alpha = "A" in image.getbands()
    rgba = image.convert("RGBA")

    rgba_array = np.asarray(rgba).astype(np.float32)
    rgb = rgba_array[..., :3] / 255.0
    alpha = rgba_array[..., 3:4]

    hue_tol = 18.0 / 360.0
    sat_tol = 0.28
    light_tol = 0.28
    feather = 0.08

    rgb_work = rgb

    for edit in edits:
        selected_hex = str(edit["selected_hex"])
        hue_shift_degrees = float(edit["hue_shift_degrees"])
        saturation_delta_percent = float(edit["saturation_delta_percent"])
        lightness_delta_percent = float(edit["lightness_delta_percent"])

        target_rgb = np.array([_hex_to_rgb_tuple(selected_hex)], dtype=np.float32) / 255.0
        target_h, target_s, target_l = _rgb_to_hsl_numpy(target_rgb, np)
        target_h = float(target_h.reshape(-1)[0])
        target_s = float(target_s.reshape(-1)[0])
        target_l = float(target_l.reshape(-1)[0])

        h, s, l = _rgb_to_hsl_numpy(rgb_work, np)

        hue_diff = np.abs(((h - target_h + 0.5) % 1.0) - 0.5)
        sat_diff = np.abs(s - target_s)
        light_diff = np.abs(l - target_l)

        h_mask = 1.0 - _smoothstep_numpy(hue_tol, hue_tol + feather, hue_diff, np)
        s_mask = 1.0 - _smoothstep_numpy(sat_tol, sat_tol + feather, sat_diff, np)
        l_mask = 1.0 - _smoothstep_numpy(light_tol, light_tol + feather, light_diff, np)
        mask = np.clip(h_mask * s_mask * l_mask, 0.0, 1.0)

        hue_shift = hue_shift_degrees / 360.0
        sat_delta = saturation_delta_percent / 100.0
        light_delta = lightness_delta_percent / 100.0

        h2 = np.mod(h + hue_shift * mask, 1.0)
        s2 = np.clip(s + sat_delta * mask, 0.0, 1.0)
        l2 = np.clip(l + light_delta * mask, 0.0, 1.0)

        rgb_shifted = _hsl_to_rgb_numpy(h2, s2, l2, np)
        mask_3 = mask[..., None]
        rgb_work = np.clip(rgb_work * (1.0 - mask_3) + rgb_shifted * mask_3, 0.0, 1.0)

    out_rgba = np.concatenate([rgb_work * 255.0, alpha], axis=-1).astype(np.uint8)
    out_image_rgba = Image.fromarray(out_rgba, mode="RGBA")

    save_format, mime_type = _detect_output_format(output_path)

    if save_format == "JPEG":
        out_image = out_image_rgba.convert("RGB")
    elif save_format == "WEBP" and not has_alpha:
        out_image = out_image_rgba.convert("RGB")
    else:
        out_image = out_image_rgba

    buf = io.BytesIO()
    save_kwargs = {}
    if save_format in {"JPEG", "WEBP"}:
        save_kwargs["quality"] = 95
    out_image.save(buf, format=save_format, **save_kwargs)

    return buf.getvalue(), mime_type


@router.post("")
def create_generation(
    body: GenerationCreateRequest,
    current: CurrentShopContext = Depends(get_current_shop_context),
):
    supabase = get_supabase_admin_client()
    settings = get_settings()

    hero_image_id = _clean_id(body.hero_image_id, "hero_image_id")
    normalized_fabrics = _normalize_generation_fabrics(body)
    primary_fabric_image_id = normalized_fabrics[0]["fabric_image_id"]

    # Build prompt before charging credits so prompt lookup failures do not debit the user.
    folder_context = _load_folder_prompt_context_for_hero_image(
        supabase=supabase,
        shop_id=current.shop_id,
        hero_image_id=hero_image_id,
    )
    fabric_scale = None
    if normalized_fabrics:
        fabric_scale = normalized_fabrics[0].get("fabric_scale")

    prompt_used = build_generation_prompt(
        folder_name=folder_context.get("name"),
        folder_prompt_template=folder_context.get("prompt_template"),
        fabric_assignments=normalized_fabrics,
        fabric_scale=fabric_scale,
    )

    try:
        result = (
            supabase.rpc(
                "create_generation_with_credit_debit_v2",
                {
                    "p_shop_id": current.shop_id,
                    "p_hero_image_id": hero_image_id,
                    "p_fabrics": normalized_fabrics,
                    "p_credits_cost": settings.CREDITS_PER_IMAGE,
                },
            )
            .execute()
        )
    except Exception as exc:
        message = str(exc).lower()

        if "insufficient credits" in message:
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail="Insufficient credits",
            ) from exc

        if "hero image not found" in message:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Hero image not found for this shop",
            ) from exc

        if "fabric image not found" in message:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Fabric image not found for this shop",
            ) from exc

        if "one or more fabric images not found" in message:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="One or more fabric images not found for this shop",
            ) from exc

        if (
            "invalid apply_to value" in message
            or "duplicate apply_to value" in message
            or "suit_full_body cannot be combined" in message
            or "count must be between 1 and 3" in message
            or "must include fabric_image_id and apply_to" in message
            or "fabric_code is required" in message
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc

        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create generation",
        ) from exc

    rows = getattr(result, "data", None) or []
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Generation created but no response returned",
        )

    row = rows[0]
    generation_id = row["generation_id"]

    prompt_saved = True
    prompt_save_warning = None

    # Best-effort save: avoid failing the request after credits were already debited.
    try:
        (
            supabase.table("generations")
            .update(
                {
                    "prompt_used": prompt_used,
                    # Keep legacy column populated for backward compatibility.
                    "fabric_image_id": primary_fabric_image_id,
                }
            )
            .eq("id", generation_id)
            .eq("shop_id", current.shop_id)
            .execute()
        )
    except Exception as exc:
        prompt_saved = False
        prompt_save_warning = f"Generation queued, but prompt could not be saved: {exc}"

    fabric_metadata_warning = _persist_generation_fabric_metadata(
        supabase=supabase,
        shop_id=current.shop_id,
        generation_id=generation_id,
        normalized_fabrics=normalized_fabrics,
    )

    response = {
        "id": generation_id,
        "status": row["status"],
        "credits_used": row["credits_used"],
        "balance_before": row["balance_before"],
        "balance_after": row["balance_after"],
        "prompt_saved": prompt_saved,
    }

    warnings: list[str] = []
    if prompt_save_warning:
        warnings.append(prompt_save_warning)
    if fabric_metadata_warning:
        warnings.append(fabric_metadata_warning)

    if warnings:
        response["warning"] = " | ".join(warnings)

    return response


@router.get("")
def list_generations(
    status_filter: Optional[str] = Query(default=None, alias="status"),
    folder_id: Optional[str] = Query(default=None),
    fabric_code: Optional[str] = Query(default=None),
    fabric_color: Optional[str] = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    include_urls: bool = Query(default=False),
    current: CurrentShopContext = Depends(get_current_shop_context),
):
    supabase = get_supabase_admin_client()

    if status_filter is not None:
        status_filter = status_filter.strip().lower()
        if status_filter not in _ALLOWED_STATUSES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid status filter",
            )

    filtered_generation_ids = _find_generation_ids_by_fabric_filters(
        supabase=supabase,
        shop_id=current.shop_id,
        fabric_code=fabric_code,
        fabric_color=fabric_color,
    )

    query = (
        supabase.table("generations")
        .select(
            "id, shop_id, hero_image_id, fabric_image_id, folder_id, status, "
            "output_path, credits_used, created_at, started_at, completed_at"
        )
        .eq("shop_id", current.shop_id)
        .order("created_at", desc=True)
    )

    if status_filter:
        query = query.eq("status", status_filter)

    if folder_id and folder_id.strip():
        query = query.eq("folder_id", folder_id.strip())

    if filtered_generation_ids is not None:
        if not filtered_generation_ids:
            return []
        query = query.in_("id", filtered_generation_ids)

    query = query.range(offset, offset + limit - 1)

    try:
        result = query.execute()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch generations",
        ) from exc

    rows = getattr(result, "data", None) or []
    enriched_rows = _attach_generation_fabric_metadata(
        supabase=supabase,
        shop_id=current.shop_id,
        generation_rows=rows,
    )

    if include_urls:
        enriched_rows = _attach_download_and_thumb_urls(supabase, enriched_rows)

    return enriched_rows


@router.post("/download-urls")
def get_generation_download_urls_batch(
    body: GenerationDownloadUrlsRequest,
    current: CurrentShopContext = Depends(get_current_shop_context),
):
    supabase = get_supabase_admin_client()
    settings = get_settings()

    ids = [str(gid).strip() for gid in body.generation_ids if str(gid).strip()]
    if not ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="generation_ids is required",
        )
    if len(ids) > 100:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="At most 100 generation_ids are allowed",
        )

    try:
        result = (
            supabase.table("generations")
            .select("id, status, output_path")
            .eq("shop_id", current.shop_id)
            .in_("id", ids)
            .execute()
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch generations",
        ) from exc

    rows = getattr(result, "data", None) or []
    done_rows = [
        row
        for row in rows
        if str(row.get("status") or "") == "done"
        and str(row.get("output_path") or "").strip()
    ]

    urls: dict[str, dict[str, Optional[str]]] = {}

    if done_rows:
        output_paths: list[str] = []
        thumb_paths: list[str] = []
        for row in done_rows:
            output_path = str(row["output_path"]).strip()
            output_paths.append(output_path)
            thumb_paths.append(derive_thumb_path(output_path))

        try:
            url_by_path = _sign_paths_with_fallback(
                supabase, settings, output_paths, thumb_paths, 3600
            )
        except Exception as exc:
            print(f"[include_urls] unexpected failure signing URLs: {exc}")
            url_by_path = {}

        for row in done_rows:
            generation_id = str(row["id"])
            output_path = str(row["output_path"]).strip()
            download_url = url_by_path.get(output_path)
            thumb_url = url_by_path.get(derive_thumb_path(output_path)) or download_url
            urls[generation_id] = {
                "download_url": download_url,
                "thumb_url": thumb_url,
            }

    return {
        "urls": urls,
        "expires_in_seconds": 3600,
    }


@router.get("/{generation_id}")
def get_generation(
    generation_id: str,
    current: CurrentShopContext = Depends(get_current_shop_context),
):
    supabase = get_supabase_admin_client()

    generation_id = _clean_id(generation_id, "generation_id")

    try:
        result = (
            supabase.table("generations")
            .select("*")
            .eq("id", generation_id)
            .eq("shop_id", current.shop_id)
            .limit(1)
            .execute()
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch generation",
        ) from exc

    rows = getattr(result, "data", None) or []
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Generation not found",
        )

    enriched = _attach_generation_fabric_metadata(
        supabase=supabase,
        shop_id=current.shop_id,
        generation_rows=rows,
    )
    return enriched[0]


@router.get("/{generation_id}/download-url")
def get_generation_download_url(
    generation_id: str,
    expires_in_seconds: int = Query(default=300, ge=60, le=3600),
    current: CurrentShopContext = Depends(get_current_shop_context),
):
    supabase = get_supabase_admin_client()
    settings = get_settings()

    generation_id = _clean_id(generation_id, "generation_id")

    try:
        result = (
            supabase.table("generations")
            .select("id, status, output_path")
            .eq("id", generation_id)
            .eq("shop_id", current.shop_id)
            .limit(1)
            .execute()
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch generation",
        ) from exc

    rows = getattr(result, "data", None) or []
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Generation not found",
        )

    row = rows[0]
    generation_status = str(row.get("status") or "")
    output_path = row.get("output_path")

    if generation_status != "done" or not output_path:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Generation output is not ready yet",
        )

    try:
        signed = (
            supabase.storage.from_("generated-outputs")
            .create_signed_url(output_path, expires_in_seconds)
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create signed download URL",
        ) from exc

    signed_url = None
    if isinstance(signed, dict):
        signed_url = (
            signed.get("signedURL")
            or signed.get("signedUrl")
            or signed.get("signed_url")
            or signed.get("data", {}).get("signedURL")
        )

    if not signed_url:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Storage signed URL response was empty",
        )

    if signed_url.startswith("/"):
        signed_url = f"{settings.SUPABASE_URL}{signed_url}"

    return {
        "generation_id": row["id"],
        "download_url": signed_url,
        "expires_in_seconds": expires_in_seconds,
    }


@router.post("/{generation_id}/match-color")
def match_color_on_generation_output(
    generation_id: str,
    body: GenerationMatchColorRequest,
    current: CurrentShopContext = Depends(get_current_shop_context),
):
    supabase = get_supabase_admin_client()

    generation_id = _clean_id(generation_id, "generation_id")
    normalized_edits = _normalize_match_color_edits(body)

    try:
        result = (
            supabase.table("generations")
            .select("id, status, output_path")
            .eq("id", generation_id)
            .eq("shop_id", current.shop_id)
            .limit(1)
            .execute()
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch generation",
        ) from exc

    rows = getattr(result, "data", None) or []
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Generation not found",
        )

    row = rows[0]
    generation_status = str(row.get("status") or "")
    output_path = str(row.get("output_path") or "").strip()

    if generation_status != "done" or not output_path:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Generation output is not ready yet",
        )

    try:
        original_bytes = supabase.storage.from_("generated-outputs").download(output_path)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to download generation output from storage",
        ) from exc

    try:
        edited_bytes, edited_mime_type = _apply_match_color_full_res(
            original_bytes,
            output_path=output_path,
            edits=normalized_edits,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to apply Match Color: {exc}",
        ) from exc

    storage_bucket = supabase.storage.from_("generated-outputs")
    old_output_path = output_path
    new_ext = _guess_extension_from_mime(edited_mime_type)
    new_output_path = f"{current.shop_id}/{generation_id}/output_v{int(time.time())}.{new_ext}"

    try:
        storage_bucket.upload(
            new_output_path,
            edited_bytes,
            {
                "content-type": edited_mime_type,
                "upsert": "false",
            },
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to upload new generation output to storage",
        ) from exc

    new_thumb_path = derive_thumb_path(new_output_path)
    try:
        np, Image = _import_match_color_dependencies()
        thumb_image = Image.open(io.BytesIO(edited_bytes))
        thumb_image.load()
        thumb_image = thumb_image.convert("RGB")
        if thumb_image.width > 400:
            new_height = max(1, round(thumb_image.height * (400 / thumb_image.width)))
            thumb_image = thumb_image.resize((400, new_height), Image.Resampling.LANCZOS)
        thumb_buf = io.BytesIO()
        thumb_image.save(thumb_buf, format="JPEG", quality=70)

        storage_bucket.upload(
            new_thumb_path,
            thumb_buf.getvalue(),
            {
                "content-type": "image/jpeg",
                "upsert": "false",
            },
        )
    except Exception as exc:
        print(f"WARNING: failed to generate/upload thumbnail for {new_output_path}: {exc}")

    try:
        (
            supabase.table("generations")
            .update({"output_path": new_output_path})
            .eq("id", generation_id)
            .eq("shop_id", current.shop_id)
            .execute()
        )
    except Exception as exc:
        try:
            storage_bucket.remove([new_output_path, new_thumb_path])
        except Exception:
            pass
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update generation output path",
        ) from exc

    try:
        storage_bucket.remove([old_output_path, derive_thumb_path(old_output_path)])
    except Exception:
        pass

    return {
        "generation_id": generation_id,
        "output_path": new_output_path,
        "edited": True,
        "applied_edits": len(normalized_edits),
    }


@router.delete("/{generation_id}")
def delete_generation_output(
    generation_id: str,
    current: CurrentShopContext = Depends(get_current_shop_context),
):
    supabase = get_supabase_admin_client()
    generation_id = _clean_id(generation_id, "generation_id")

    try:
        result = (
            supabase.table("generations")
            .select("id, status, output_path")
            .eq("id", generation_id)
            .eq("shop_id", current.shop_id)
            .limit(1)
            .execute()
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch generation",
        ) from exc

    rows = getattr(result, "data", None) or []
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Generation not found",
        )

    row = rows[0]
    generation_status = str(row.get("status") or "").strip().lower()
    output_path = str(row.get("output_path") or "").strip()

    # V1: delete is only for saved outputs.
    if generation_status != "done" or not output_path:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only completed outputs can be deleted",
        )

    try:
        (
            supabase.table("generations")
            .delete()
            .eq("id", generation_id)
            .eq("shop_id", current.shop_id)
            .execute()
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete generation",
        ) from exc

    storage_warning = None
    try:
        supabase.storage.from_("generated-outputs").remove([output_path])
    except Exception as exc:
        # Keep delete successful even if storage cleanup fails.
        storage_warning = f"Generation deleted, but storage cleanup failed: {exc}"

    response = {
        "deleted": True,
        "generation_id": generation_id,
        "output_path": output_path,
    }
    if storage_warning:
        response["warning"] = storage_warning

    return response

