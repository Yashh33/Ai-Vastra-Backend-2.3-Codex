"""Which Gemini image model a shop uses. Read per job/request, never cached, so an admin
change applies to the next generation. Choosing a model never fails a generation."""
from typing import Any, Optional

import attempt_log
from config import IMAGE_MODELS, get_settings


def read_shop_image_model(supabase: Any, shop_id: str) -> Optional[str]:
    """The stored value as is. Raises if the read fails (e.g. the SQL has not been run yet)."""
    result = supabase.table("shops").select("image_model_id").eq("id", shop_id).limit(1).execute()
    rows = getattr(result, "data", None) or []
    return rows[0].get("image_model_id") if rows else None


def pick_image_model(stored: Optional[str], shop_id: str) -> str:
    default_model = get_settings().GEMINI_IMAGE_MODEL_ID
    if not stored:
        return default_model
    if stored in IMAGE_MODELS:
        return stored
    print(f"[image_models] WARNING: shop {shop_id} has unknown image model {stored!r}; using {default_model}")
    return default_model


def warn_read_failed(shop_id: str, exc: Exception) -> None:
    print(
        f"[image_models] WARNING: could not read the image model of shop {shop_id}; "
        f"using the default: {attempt_log.scrub(str(exc), 300)}"
    )


def resolve_image_model(supabase: Any, shop_id: str) -> str:
    """The shop's model if it is in the allow-list, else the env default."""
    try:
        stored = read_shop_image_model(supabase, shop_id)
    except Exception as exc:
        warn_read_failed(shop_id, exc)
        stored = None
    return pick_image_model(stored, shop_id)
