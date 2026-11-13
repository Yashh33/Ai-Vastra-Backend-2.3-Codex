"""Every finished web try-on / look try-on, kept for the shop for TRYON_SHOP_RESULT_HOURS (24).

- Private bucket "tryon-recent" (service role only, no storage policies).
  "<UTC date and hour YYYY-MM-DDTHH>/<attempt id>.<jpg|png|webp>": the AI result as it is,
  plus "<same name>_thumb.jpg": a small copy (longest edge 400 px).
- The attempt row (generation_attempts) links it: shop_result_path, shop_result_saved_at.
- Only the finished try-on picture is kept. The customer's own photo is never stored or logged.

delete_expired (worker, hourly) only ever touches this bucket, and only paths with the exact
shape above (the same care as tryon_results.delete_expired).

Until sql/2026-11-13_round2a_tickets_and_recent_tryons.sql runs (no columns, no bucket),
try-ons work as before and one warning is logged."""
from __future__ import annotations

import io
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import ai_call
import attempt_log
import image_thumbs
from config import get_settings
from supabase_client import retry_once

BUCKET = "tryon-recent"
SQL_FILE = "sql/2026-11-13_round2a_tickets_and_recent_tryons.sql"
SIGNED_URL_SECONDS = 15 * 60
THUMB_EDGE = 400
THUMB_QUALITY = 75
STILL_RUNNING_FOR = attempt_log.STUCK_AFTER  # 5 minutes: older open attempts count as timed out
RECENT_LIMIT = 30
TRYON_TYPES = ["tryon", "look_tryon"]
STILL_RUNNING_DETAIL = "This try-on is still being made."
STILL_RUNNING_CODE = "still_running"

_EXT_BY_MIME = {"image/jpeg": "jpg", "image/jpg": "jpg", "image/png": "png", "image/webp": "webp"}
_MIME_BY_EXT = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_HOUR = r"\d{4}-\d{2}-\d{2}T\d{2}"
_HOUR_RE = re.compile(rf"^{_HOUR}$")
_RESULT_RE = re.compile(rf"^{_HOUR}/{_UUID}\.(?:jpg|png|webp)$")
_THUMB_RE = re.compile(rf"^{_HOUR}/{_UUID}_thumb\.jpg$")

_BATCH = 200
_MAX_BATCHES = 25

_BASE_COLUMNS = (
    "id, request_id, client_request_id, attempt_type, status, started_at, created_at, "
    "garment_type_id, garment_type_name, failed_step, user_message, http_status, "
    "credits_charged, credits_refunded"
)
_RESULT_COLUMNS = "shop_result_path, shop_result_saved_at"

_warned: set[str] = set()


# ---------------------------------------------------------------- small helpers

def _warn_once(key: str, message: str) -> None:
    if key not in _warned:
        _warned.add(key)
        print(message, flush=True)


def _short(exc: BaseException) -> str:
    return attempt_log.scrub(f"{type(exc).__name__}: {exc}", 300) or type(exc).__name__


def _is_missing_column(exc: BaseException) -> bool:
    return attempt_log._is_missing_column(exc)


def _is_missing_bucket(exc: BaseException) -> bool:
    return "bucket not found" in str(exc).lower()


def _warn_missing_schema(what: str) -> None:
    _warn_once(
        f"missing:{what}",
        f"[tryon_recent] WARNING: {what} missing (run {SQL_FILE}); "
        "try-ons work as before but results are not kept for the shop",
    )


def _parse_time(value: Any) -> Optional[datetime]:
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def keep_hours() -> int:
    return int(get_settings().TRYON_SHOP_RESULT_HOURS)


def is_result_path(path: Optional[str]) -> bool:
    return bool(path) and bool(_RESULT_RE.match(str(path)))


def is_thumb_path(path: Optional[str]) -> bool:
    return bool(path) and bool(_THUMB_RE.match(str(path)))


def thumb_path(result_path: str) -> str:
    return f"{result_path.rsplit('.', 1)[0]}_thumb.jpg"


def mime_for(result_path: str) -> str:
    return _MIME_BY_EXT.get(result_path.rsplit(".", 1)[-1], "image/jpeg")


def result_path(attempt_id: str, mime_type: Optional[str], when: datetime) -> Optional[str]:
    """None when the picture's type is not one the bucket accepts."""
    ext = _EXT_BY_MIME.get(str(mime_type or "").split(";")[0].strip().lower())
    if not ext:
        return None
    return f"{when.astimezone(timezone.utc):%Y-%m-%dT%H}/{attempt_id}.{ext}"


def small_jpeg(image_bytes: bytes) -> bytes:
    """JPEG, longest edge at most THUMB_EDGE (never upscaled), transparency onto white."""
    from PIL import Image

    image = Image.open(io.BytesIO(image_bytes))
    if image.format == "JPEG":
        image.draft("RGB", (THUMB_EDGE, THUMB_EDGE))
    image.load()
    if image.mode in ("RGBA", "LA", "P"):
        rgba = image.convert("RGBA")
        flattened = Image.new("RGB", rgba.size, (255, 255, 255))
        flattened.paste(rgba, mask=rgba.getchannel("A"))
        image = flattened
    else:
        image = image.convert("RGB")
    image.thumbnail((THUMB_EDGE, THUMB_EDGE), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=THUMB_QUALITY, optimize=True)
    return buf.getvalue()


# ---------------------------------------------------------------- store (API, before the response)

def _link_row(supabase: Any, attempt_id: str, data: dict) -> bool:
    result = retry_once(
        lambda: supabase.table(attempt_log.TABLE).update(data).eq("id", attempt_id).execute()
    )
    return bool(getattr(result, "data", None))


def store_result(supabase: Any, attempt: Any, image_bytes: bytes, mime_type: Optional[str]) -> Optional[str]:
    """Keep the finished try-on for the shop. Returns the stored path, or None.
    Never raises and never fails the try-on: a failure logs one line."""
    attempt_id = attempt_log.valid_uuid(getattr(attempt, "id", None))
    if not attempt_id or not image_bytes:
        return None
    try:
        saved_at = attempt_log.utc_now()
        path = result_path(attempt_id, mime_type, saved_at)
        if path is None:
            print(f"[tryon_recent] WARNING: try-on result not kept for attempt {attempt_id}: type {mime_type!r} not accepted")
            return None

        try:
            thumb = small_jpeg(image_bytes)
        except Exception as exc:
            thumb = None
            print(f"[tryon_recent] WARNING: small copy not made for attempt {attempt_id}: {_short(exc)}")

        image_thumbs.upload_new_file(supabase, BUCKET, path, image_bytes, _MIME_BY_EXT[path.rsplit(".", 1)[-1]])
        if thumb is not None:
            image_thumbs.upload_new_file(supabase, BUCKET, thumb_path(path), thumb, "image/jpeg")

        # A direct write (not the background log writer), so the link exists before the answer.
        data = {"shop_result_path": path, "shop_result_saved_at": attempt_log.iso(saved_at)}
        if not _link_row(supabase, attempt_id, data):
            # The row's insert is still queued on the log writer: let it land, then try again.
            attempt_log.flush(3.0)
            if not _link_row(supabase, attempt_id, data):
                print(f"[tryon_recent] WARNING: try-on result kept but not linked for attempt {attempt_id}: row not found")
                return None
        return path
    except Exception as exc:
        if _is_missing_column(exc):
            _warn_missing_schema("generation_attempts.shop_result_path")
        elif _is_missing_bucket(exc) or _is_missing_bucket(exc.__cause__ or exc):
            _warn_missing_schema(f"storage bucket {BUCKET}")
        else:
            print(f"[tryon_recent] WARNING: try-on result not kept for attempt {attempt_id}: {_short(exc)}")
        return None


# ---------------------------------------------------------------- lookups

def _select_attempts(supabase: Any, shop_id: str, column: str, value: str) -> list[dict]:
    """This shop's try-on attempts where column = value, newest first. Without the new columns
    (SQL not run yet) the same rows come back without them, after one warning."""

    def run(columns: str) -> list[dict]:
        return (
            retry_once(
                lambda: supabase.table(attempt_log.TABLE)
                .select(columns)
                .eq("shop_id", shop_id)
                .eq(column, value)
                .in_("attempt_type", TRYON_TYPES)
                .order("started_at", desc=True)
                .limit(20)
                .execute()
            ).data
            or []
        )

    try:
        return run(f"{_BASE_COLUMNS}, {_RESULT_COLUMNS}")
    except Exception as exc:
        if not _is_missing_column(exc):
            raise
        _warn_missing_schema("generation_attempts.shop_result_path")
        return run(_BASE_COLUMNS)


def _fresh_result(row: dict, now: datetime) -> bool:
    saved_at = _parse_time(row.get("shop_result_saved_at"))
    return (
        is_result_path(row.get("shop_result_path"))
        and saved_at is not None
        and saved_at >= now - timedelta(hours=keep_hours())
    )


def _still_running(row: dict, now: datetime) -> bool:
    started = _parse_time(row.get("started_at"))
    return (
        row.get("status") in attempt_log.OPEN_STATUSES
        and started is not None
        and now - started <= STILL_RUNNING_FOR
    )


@dataclass
class EarlierTicket:
    kind: str  # "replay" | "running"
    row: dict
    image_bytes: bytes = b""
    mime_type: str = ""


def check_ticket(
    supabase: Any, shop_id: str, ticket: Optional[str], *, exclude_id: Optional[str], now: Optional[datetime] = None
) -> Optional[EarlierTicket]:
    """Same X-Client-Request-Id again on a try-on POST (called before any charge):
    "replay" with the stored picture, "running" while an earlier one is still being made,
    or None to carry on as a normal new try-on. Never raises."""
    ticket = attempt_log.valid_uuid(ticket)
    if not ticket or not shop_id:
        return None
    now = now or attempt_log.utc_now()
    try:
        rows = [
            row
            for row in _select_attempts(supabase, shop_id, "client_request_id", ticket)
            if str(row.get("id")) != str(exclude_id)
        ]
    except Exception as exc:
        print(f"[tryon_recent] WARNING: same-ticket check skipped: {_short(exc)}")
        return None

    for row in rows:
        if not _fresh_result(row, now):
            continue
        path = row["shop_result_path"]
        try:
            data = retry_once(lambda: supabase.storage.from_(BUCKET).download(path))
        except Exception as exc:
            print(f"[tryon_recent] WARNING: stored result of attempt {row.get('id')} not readable: {_short(exc)}")
            continue
        if data:
            return EarlierTicket("replay", row, bytes(data), mime_for(path))

    for row in rows:
        if _still_running(row, now):
            return EarlierTicket("running", row)
    return None


def _sign(paths: list[str]) -> dict[str, Optional[str]]:
    return image_thumbs.sign_existing_paths(BUCKET, paths, expires_in=SIGNED_URL_SECONDS)


def _error_code(row: dict, effective: str) -> Optional[str]:
    if effective == "timed_out":
        return "timed_out"
    step = row.get("failed_step")
    if step == "ai_call" and row.get("http_status") == 503:
        return ai_call.AI_BUSY
    return step


def result_status(supabase: Any, shop_id: str, request_id: str, now: Optional[datetime] = None) -> dict:
    """GET /tryon/result/{request_id}: ready | running | failed | unknown, for this shop only."""
    request_id = attempt_log.valid_uuid(request_id)
    if not request_id:
        return {"status": "unknown"}
    now = now or attempt_log.utc_now()

    rows: dict[str, dict] = {}
    for column in ("request_id", "client_request_id"):
        for row in _select_attempts(supabase, shop_id, column, request_id):
            rows.setdefault(str(row.get("id")), row)
    ordered = sorted(rows.values(), key=lambda row: str(row.get("started_at") or ""), reverse=True)

    for row in ordered:
        if not _fresh_result(row, now):
            continue
        path = row["shop_result_path"]
        signed = _sign([path, thumb_path(path)])
        if signed.get(path):
            return {"status": "ready", "image_url": signed[path], "thumb_url": signed.get(thumb_path(path))}

    for row in ordered:
        if _still_running(row, now):
            return {"status": "running"}

    for row in ordered:
        if row.get("http_status") == 409:
            continue  # a repeat answered "still being made", not the try-on itself
        effective = attempt_log.effective_status(row, now)
        if effective in ("failed", "timed_out"):
            return {
                "status": "failed",
                "error_code": _error_code(row, effective),
                "message": row.get("user_message"),
                "credits_refunded": row.get("credits_refunded"),
            }
    return {"status": "unknown"}


def recent(supabase: Any, shop_id: str, now: Optional[datetime] = None) -> list[dict]:
    """GET /tryon/recent: this shop's kept results, newest first, at most RECENT_LIMIT."""
    now = now or attempt_log.utc_now()
    hours = keep_hours()
    try:
        rows = (
            retry_once(
                lambda: supabase.table(attempt_log.TABLE)
                .select(f"{_BASE_COLUMNS}, {_RESULT_COLUMNS}")
                .eq("shop_id", shop_id)
                .in_("attempt_type", TRYON_TYPES)
                .gte("shop_result_saved_at", attempt_log.iso(now - timedelta(hours=hours)))
                .order("shop_result_saved_at", desc=True)
                .limit(RECENT_LIMIT)
                .execute()
            ).data
            or []
        )
    except Exception as exc:
        if not _is_missing_column(exc):
            raise
        _warn_missing_schema("generation_attempts.shop_result_path")
        return []

    rows = [row for row in rows if is_result_path(row.get("shop_result_path"))]
    paths = [path for row in rows for path in (row["shop_result_path"], thumb_path(row["shop_result_path"]))]
    signed = _sign(paths) if paths else {}

    items = []
    for row in rows:
        path = row["shop_result_path"]
        if not signed.get(path):
            continue  # the file is gone
        saved_at = _parse_time(row.get("shop_result_saved_at"))
        items.append(
            {
                "request_id": row.get("request_id"),
                "client_request_id": row.get("client_request_id"),
                "attempt_type": row.get("attempt_type"),
                "garment_type_id": row.get("garment_type_id"),
                "garment_type_name": row.get("garment_type_name"),
                "created_at": row.get("created_at"),
                "expires_at": attempt_log.iso(saved_at + timedelta(hours=hours)) if saved_at else None,
                "image_url": signed[path],
                "thumb_url": signed.get(thumb_path(path)),
            }
        )
    return items


# ---------------------------------------------------------------- clean-up (worker, hourly)

def _remove(client: Any, paths: list[str]) -> int:
    safe = [path for path in paths if is_result_path(path) or is_thumb_path(path)]
    if safe:
        client.storage.from_(BUCKET).remove(safe)
        print(f"[tryon_recent] deleted {len(safe)} expired shop try-on file(s) from {BUCKET}: {safe}")
    return len(safe)


def delete_expired(client: Any, now: Optional[datetime] = None) -> int:
    """Delete results older than TRYON_SHOP_RESULT_HOURS and clear their two columns; then
    whole hour-folders older than that plus one hour (files whose row was never linked).
    Only this bucket, only well-formed paths. Returns how many files were deleted."""
    now = now or attempt_log.utc_now()
    hours = keep_hours()
    cutoff = now - timedelta(hours=hours)
    deleted = 0

    try:
        for _ in range(_MAX_BATCHES):
            rows = (
                client.table(attempt_log.TABLE)
                .select("id, shop_result_path")
                .lt("shop_result_saved_at", attempt_log.iso(cutoff))
                .limit(_BATCH)
                .execute()
            ).data or []
            if not rows:
                break
            paths: list[str] = []
            for row in rows:
                path = row.get("shop_result_path")
                if is_result_path(path):
                    paths += [path, thumb_path(path)]
            deleted += _remove(client, paths)
            # Files first, then the links: a failed delete is tried again next hour.
            client.table(attempt_log.TABLE).update({"shop_result_path": None, "shop_result_saved_at": None}).in_(
                "id", [row["id"] for row in rows]
            ).execute()
            if len(rows) < _BATCH:
                break
    except Exception as exc:
        if not _is_missing_column(exc):
            raise
        _warn_missing_schema("generation_attempts.shop_result_path")

    # Every file in an hour-folder was saved in that UTC hour: a folder that started more than
    # hours + 1 ago holds only expired files.
    folder_cutoff = now - timedelta(hours=hours + 1)
    bucket = client.storage.from_(BUCKET)
    try:
        folders = bucket.list("", {"limit": 1000}) or []
    except Exception as exc:
        if _is_missing_bucket(exc):
            _warn_missing_schema(f"storage bucket {BUCKET}")
            return deleted
        raise
    for folder in folders:
        name = str(folder.get("name") or "") if isinstance(folder, dict) else ""
        if not _HOUR_RE.match(name):
            continue
        try:
            started = datetime.strptime(name, "%Y-%m-%dT%H").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if started >= folder_cutoff:
            continue
        files = bucket.list(name, {"limit": 1000}) or []
        deleted += _remove(client, [f"{name}/{item.get('name')}" for item in files if isinstance(item, dict)])
    return deleted
