"""A small copy of each successful web try-on / look try-on result, for the admin Operations
page only. Kept in the private "tryon-results" bucket (service role only, no public read) and
deleted automatically after TRYON_RESULT_RETENTION_DAYS. The customer's own photo is never stored.

delete_expired is the one automatic storage delete in this codebase: it only ever touches
this bucket, and only files whose path has the exact shape save_copy writes."""
import io
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

import attempt_log
from config import get_settings
from supabase_client import retry_once

BUCKET = "tryon-results"
# generations.prompt_used of a row made by POST /tryon/push-to-screen. The worker's hourly
# clean-up finds pushed customer try-ons by this exact text: never change it.
PUSHED_TRYON_PROMPT = "web-app try-on (pushed to screen)"
MAX_EDGE = 768
JPEG_QUALITY = 80
SIGNED_URL_SECONDS = 600

# "<YYYY-MM-DD>/<attempt uuid>.jpg", the day in UTC.
_PATH_RE = re.compile(r"^\d{4}-\d{2}-\d{2}/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.jpg$")
_BATCH = 200
_MAX_BATCHES = 25


def is_copy_path(path: Optional[str]) -> bool:
    return bool(path) and bool(_PATH_RE.match(str(path)))


def copy_path(attempt_id: str, when: datetime) -> str:
    return f"{when.astimezone(timezone.utc):%Y-%m-%d}/{attempt_id}.jpg"


def small_jpeg(image_bytes: bytes) -> bytes:
    """JPEG, longest side at most MAX_EDGE (never upscaled), transparency flattened onto white."""
    from PIL import Image

    image = Image.open(io.BytesIO(image_bytes))
    image.load()
    if image.mode in ("RGBA", "LA", "P"):
        rgba = image.convert("RGBA")
        flattened = Image.new("RGB", rgba.size, (255, 255, 255))
        flattened.paste(rgba, mask=rgba.getchannel("A"))
        image = flattened
    else:
        image = image.convert("RGB")
    image.thumbnail((MAX_EDGE, MAX_EDGE), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return buf.getvalue()


def save_copy(supabase: Any, attempt: Any, image_bytes: bytes) -> None:
    """Background task, after the result was returned. Never raises: a failure only logs."""
    attempt_id = attempt_log.valid_uuid(getattr(attempt, "id", None))
    if not attempt_id:
        return
    try:
        data = small_jpeg(image_bytes)
        saved_at = attempt_log.utc_now()
        path = copy_path(attempt_id, saved_at)
        retry_once(
            lambda: supabase.storage.from_(BUCKET).upload(
                path, data, {"content-type": "image/jpeg", "upsert": "true"}
            )
        )
        attempt.update(result_copy_path=path, result_copy_saved_at=saved_at)
    except Exception as exc:
        print(f"[tryon_results] WARNING: result copy not saved for attempt {attempt_id}: {attempt_log.scrub(str(exc), 300)}")


def _remove(client: Any, paths: list[str]) -> int:
    safe = [path for path in paths if is_copy_path(path)]
    if safe:
        client.storage.from_(BUCKET).remove(safe)
        print(f"[tryon_results] deleted {len(safe)} expired result copies from {BUCKET}: {safe}")
    return len(safe)


def delete_expired(client: Any, now: Optional[datetime] = None) -> int:
    """Worker, hourly: delete copies older than the retention period and clear their links."""
    now = now or attempt_log.utc_now()
    cutoff = now - timedelta(days=get_settings().TRYON_RESULT_RETENTION_DAYS)
    deleted = 0

    for _ in range(_MAX_BATCHES):
        rows = (
            client.table(attempt_log.TABLE)
            .select("id, result_copy_path")
            .lt("result_copy_saved_at", attempt_log.iso(cutoff))
            .limit(_BATCH)
            .execute()
        ).data or []
        if not rows:
            break
        deleted += _remove(client, [row.get("result_copy_path") for row in rows])
        client.table(attempt_log.TABLE).update({"result_copy_path": None, "result_copy_saved_at": None}).in_(
            "id", [row["id"] for row in rows]
        ).execute()
        if len(rows) < _BATCH:
            break

    # Files whose row was never linked (the link write failed): every file in a day's folder
    # is from that UTC day, so a folder dated before the cutoff day is entirely expired.
    bucket = client.storage.from_(BUCKET)
    for folder in bucket.list("", {"limit": 1000}) or []:
        name = str(folder.get("name") or "") if isinstance(folder, dict) else ""
        try:
            day = date.fromisoformat(name)
        except ValueError:
            continue
        if day >= cutoff.date():
            continue
        files = bucket.list(name, {"limit": 1000}) or []
        deleted += _remove(client, [f"{name}/{item.get('name')}" for item in files if isinstance(item, dict)])

    return deleted
