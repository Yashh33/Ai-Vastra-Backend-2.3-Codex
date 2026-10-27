"""Generation attempt log ("Operations"): one row per Generate / Try-on attempt.

Rules this module guarantees for every caller:
- Logging never breaks or slows a generation. In the API process every write goes through
  one background thread (ordered, so the insert always lands before its updates); in the
  worker writes are synchronous. Any failure prints one line
  "attempt_log write failed: <request_id> <reason>" and the caller carries on.
- Never stores secrets, auth tokens, base64 image data or the customer photo.
- In-memory AttemptRecord is the source of truth for "has this attempt finished?"; a final
  status is written exactly once.
"""
from __future__ import annotations

import json
import queue
import re
import threading
import time
import traceback
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from config import GEMINI_PRICES_USD_PER_1M, USD_TO_INR, get_settings

TABLE = "generation_attempts"

ATTEMPT_TYPES = frozenset({"look", "tryon", "look_tryon"})
OPEN_STATUSES = ("started", "running")
FINAL_STATUSES = frozenset({"succeeded", "failed", "timed_out"})
FAILED_STEPS = frozenset(
    {
        "validation", "auth", "credits", "upload", "prompt_build", "ai_call", "ai_no_image",
        "ai_blocked", "storage_save", "db_save", "timeout", "client_network", "unknown",
    }
)
STUCK_AFTER = timedelta(minutes=5)
RETENTION = timedelta(days=90)
ERROR_DETAIL_MAX = 4000
USER_AGENT_MAX = 300
USER_MESSAGE_MAX = 1000

# Gemini finish / block reasons that mean "the model refused", as opposed to "no image".
_BLOCK_FINISH_REASONS = frozenset(
    {
        "SAFETY", "PROHIBITED_CONTENT", "IMAGE_SAFETY", "IMAGE_PROHIBITED_CONTENT",
        "BLOCKLIST", "SPII", "RECITATION", "IMAGE_RECITATION",
    }
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    return value.isoformat()


def log_write_failed(request_id: Any, reason: Any) -> None:
    print(f"attempt_log write failed: {request_id} {_short(reason)}", flush=True)


def _short(value: Any, limit: int = 300) -> str:
    text = value if isinstance(value, str) else f"{type(value).__name__}: {value}"
    return scrub(text, limit) or ""


def valid_uuid(value: Optional[str]) -> Optional[str]:
    try:
        return str(uuid.UUID(str(value).strip())) if value else None
    except (ValueError, AttributeError, TypeError):
        return None


# ---------------------------------------------------------------- scrubbing

_SECRET_SETTING_NAMES = (
    "GEMINI_API_KEY", "SUPABASE_SERVICE_ROLE_KEY", "FAL_KEY", "WHATSAPP_ACCESS_TOKEN",
    "WHATSAPP_VERIFY_TOKEN", "RAZORPAY_KEY_SECRET", "RAZORPAY_WEBHOOK_SECRET",
    "ADMIN_PANEL_SECRET",
)
_SCRUB_PATTERNS = [
    (re.compile(r"(?i)(authorization\s*[:=]\s*)(bearer|key|basic)?\s*[^\s,;\"']+"), r"\1[redacted]"),
    # Real tokens are long; short words ("Missing bearer token") stay readable.
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/\-]{16,}=*"), "Bearer [redacted]"),
    (re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), "[redacted-jwt]"),
    (re.compile(r"AIza[0-9A-Za-z_\-]{20,}"), "[redacted-key]"),
    (re.compile(r"\b(?:sk|rzp)_?-?(?:live|test)?_?[A-Za-z0-9]{16,}"), "[redacted-key]"),
    (
        re.compile(r"(?i)\b((?:api[_-]?key|apikey|access[_-]?token|token|secret|password|signature)\s*[=:]\s*)[^\s&\"',;]+"),
        r"\1[redacted]",
    ),
    (re.compile(r"(?i)([?&](?:token|key|apikey|signature)=)[^&\s\"']+"), r"\1[redacted]"),
    (re.compile(r"[A-Za-z0-9+/]{200,}={0,2}"), "[base64 removed]"),
]


def scrub(text: Any, limit: int = ERROR_DETAIL_MAX) -> Optional[str]:
    """Remove secrets, tokens and base64 blobs; truncate."""
    if text is None:
        return None
    cleaned = str(text)
    try:
        settings = get_settings()
        for name in _SECRET_SETTING_NAMES:
            secret = str(getattr(settings, name, "") or "")
            if len(secret) >= 6:
                cleaned = cleaned.replace(secret, "[redacted]")
    except Exception:
        pass
    for pattern, replacement in _SCRUB_PATTERNS:
        cleaned = pattern.sub(replacement, cleaned)
    return cleaned[:limit]


def describe_exception(exc: BaseException) -> str:
    """Raw technical error: type + message, then the traceback tail. Scrubbed, <= 4000 chars."""
    detail = getattr(exc, "detail", None)
    head = f"{type(exc).__name__}: {detail if detail is not None else exc}"
    try:
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    except Exception:
        tb = ""
    budget = ERROR_DETAIL_MAX - len(head) - 40
    tail = tb[-budget:] if budget > 0 else ""
    return scrub(f"{head}\n--- traceback (tail) ---\n{tail}" if tail else head) or head[:ERROR_DETAIL_MAX]


# ---------------------------------------------------------------- device label

def device_label(user_agent: Optional[str]) -> Optional[str]:
    if not user_agent:
        return None
    ua = user_agent.lower()
    if "iphone" in ua:
        os_name = "iPhone"
    elif "ipad" in ua:
        os_name = "iPad"
    elif "android" in ua:
        os_name = "Android"
    elif "windows" in ua:
        os_name = "Windows"
    elif "macintosh" in ua or "mac os" in ua:
        os_name = "Mac"
    elif "linux" in ua:
        os_name = "Linux"
    else:
        os_name = None

    if "edg/" in ua or "edga/" in ua or "edgios/" in ua:
        browser = "Edge"
    elif "opr/" in ua or "opera" in ua:
        browser = "Opera"
    elif "samsungbrowser" in ua:
        browser = "Samsung Internet"
    elif "fxios" in ua or "firefox" in ua:
        browser = "Firefox"
    elif "crios" in ua or "chrome/" in ua:
        browser = "Chrome"
    elif "safari" in ua:
        browser = "Safari"
    else:
        browser = None

    label = " · ".join(part for part in (os_name, browser) if part)
    return label or "Other"


# ---------------------------------------------------------------- AI usage and cost

def ai_cost_inr(model_id: Optional[str], input_tokens: Optional[int], output_tokens: Optional[int]) -> Optional[float]:
    prices = GEMINI_PRICES_USD_PER_1M.get(model_id or "")
    if not prices or input_tokens is None or output_tokens is None:
        return None
    usd = (input_tokens * prices[0] + output_tokens * prices[1]) / 1_000_000
    return round(usd * USD_TO_INR, 4)


def _enum_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    return str(getattr(value, "value", None) or getattr(value, "name", None) or value)


def _safety_list(ratings: Any) -> list[dict]:
    out = []
    for rating in ratings or []:
        out.append(
            {
                "category": _enum_text(getattr(rating, "category", None)),
                "probability": _enum_text(getattr(rating, "probability", None)),
                "blocked": bool(getattr(rating, "blocked", False)),
            }
        )
    return out


def ai_response_fields(response: Any, model_id: Optional[str]) -> dict:
    """Token counts, cost, finish reason and safety from a Gemini response. Never raises."""
    fields: dict[str, Any] = {"model_id": model_id}
    try:
        usage = getattr(response, "usage_metadata", None)
        input_tokens = getattr(usage, "prompt_token_count", None)
        candidates_tokens = getattr(usage, "candidates_token_count", None)
        thoughts_tokens = getattr(usage, "thoughts_token_count", None)
        output_tokens = (
            None
            if candidates_tokens is None and thoughts_tokens is None
            else (candidates_tokens or 0) + (thoughts_tokens or 0)
        )
        fields["ai_input_tokens"] = input_tokens
        fields["ai_output_tokens"] = output_tokens
        fields["ai_cost_inr"] = ai_cost_inr(model_id, input_tokens, output_tokens)

        candidates = getattr(response, "candidates", None) or []
        first = candidates[0] if candidates else None
        finish = _enum_text(getattr(first, "finish_reason", None)) if first is not None else None
        feedback = getattr(response, "prompt_feedback", None)
        block_reason = _enum_text(getattr(feedback, "block_reason", None))
        fields["ai_finish_reason"] = finish or (f"PROMPT_BLOCKED:{block_reason}" if block_reason else None)

        safety: dict[str, Any] = {}
        candidate_ratings = _safety_list(getattr(first, "safety_ratings", None)) if first is not None else []
        prompt_ratings = _safety_list(getattr(feedback, "safety_ratings", None))
        if candidate_ratings:
            safety["candidate"] = candidate_ratings
        if prompt_ratings:
            safety["prompt"] = prompt_ratings
        if block_reason:
            safety["prompt_block_reason"] = block_reason
        fields["ai_safety"] = safety or None
    except Exception as exc:
        log_write_failed("ai_response_fields", exc)
    return fields


def no_image_step(response: Any) -> str:
    """'ai_blocked' when the model refused, else 'ai_no_image'."""
    try:
        feedback = getattr(response, "prompt_feedback", None)
        if _enum_text(getattr(feedback, "block_reason", None)):
            return "ai_blocked"
        candidates = getattr(response, "candidates", None) or []
        finish = _enum_text(getattr(candidates[0], "finish_reason", None)) if candidates else None
        if finish in _BLOCK_FINISH_REASONS:
            return "ai_blocked"
    except Exception:
        pass
    return "ai_no_image"


# ---------------------------------------------------------------- writer

_client_factory: Optional[Callable[[], Any]] = None
_thread_clients = threading.local()


def _client() -> Any:
    if _client_factory is not None:
        return _client_factory()
    # One client per thread (clients are not shared across threads), reused between writes.
    client = getattr(_thread_clients, "client", None)
    if client is None:
        from supabase_client import get_supabase_admin_client

        client = get_supabase_admin_client()
        _thread_clients.client = client
    return client


def _reset_client() -> None:
    _thread_clients.client = None


def _is_transport_error(exc: BaseException) -> bool:
    try:
        import httpcore
        import httpx

        return isinstance(exc, (httpx.TransportError, httpcore.NetworkError, httpcore.RemoteProtocolError))
    except Exception:
        return False


class _Writer:
    def __init__(self) -> None:
        self.sync = False
        self._queue: "queue.Queue[Optional[tuple[str, Callable[[Any], None]]]]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def submit(self, request_id: str, fn: Callable[[Any], None]) -> None:
        if self.sync:
            self._run(request_id, fn)
            return
        try:
            self._ensure_thread()
            self._queue.put((request_id, fn))
        except Exception as exc:
            log_write_failed(request_id, exc)

    def _ensure_thread(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._loop, name="attempt-log-writer", daemon=True)
            self._thread.start()

    @staticmethod
    def _run(request_id: str, fn: Callable[[Any], None]) -> None:
        try:
            fn(_client())
        except Exception as exc:
            if _is_transport_error(exc):
                # Dropped keep-alive connection: one retry on a fresh client.
                _reset_client()
                try:
                    fn(_client())
                    return
                except Exception as retry_exc:
                    exc = retry_exc
            log_write_failed(request_id, exc)

    def _loop(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is not None:
                    self._run(*item)
            finally:
                self._queue.task_done()

    def flush(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while self._queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.02)
        return not self._queue.unfinished_tasks


_writer = _Writer()


def configure(*, client_factory: Optional[Callable[[], Any]] = None, sync: Optional[bool] = None) -> None:
    """Worker: synchronous writes with its own client. Tests: a fake client."""
    global _client_factory
    if client_factory is not None:
        _client_factory = client_factory
    if sync is not None:
        _writer.sync = sync


def flush(timeout: float = 5.0) -> bool:
    return _writer.flush(timeout)


def _clean(fields: dict) -> dict:
    out: dict[str, Any] = {}
    for key, value in fields.items():
        if key == "error_detail" and value is not None:
            value = scrub(value)
        elif key == "user_message" and value is not None:
            value = scrub(value, USER_MESSAGE_MAX)
        elif key == "user_agent" and value is not None:
            value = str(value)[:USER_AGENT_MAX]
        elif isinstance(value, datetime):
            value = iso(value)
        out[key] = value
    return out


def _is_unique_violation(exc: Exception) -> bool:
    text = str(exc).lower()
    return "23505" in text or "duplicate key" in text or "already exists" in text


# ---------------------------------------------------------------- attempt record (API process)

class AttemptRecord:
    def __init__(
        self,
        *,
        attempt_type: str,
        endpoint: str,
        client_request_id: Optional[str] = None,
        user_agent: Optional[str] = None,
        **fields: Any,
    ) -> None:
        self.id = str(uuid.uuid4())
        client_id = valid_uuid(client_request_id)
        self.request_id = client_id or self.id
        self.attempt_type = attempt_type
        self.status = "started"
        self.step: Optional[str] = None
        self.finalized = False
        self.started_at = utc_now()
        # The cause behind an HTTPException the middleware will only see as a response.
        self.error: Optional[str] = None
        self.failed_step: Optional[str] = None

        payload = _clean(
            {
                "id": self.id,
                "request_id": self.request_id,
                "client_request_id": client_id,
                "attempt_type": attempt_type,
                "endpoint": endpoint,
                "status": "started",
                "started_at": self.started_at,
                "device_label": fields.pop("device_label", None) or device_label(user_agent),
                "user_agent": user_agent,
                **fields,
            }
        )
        _writer.submit(self.request_id, lambda client: self._insert(client, payload))

    @staticmethod
    def _insert(client: Any, payload: dict) -> None:
        try:
            client.table(TABLE).insert(payload).execute()
        except Exception as exc:
            if not _is_unique_violation(exc):
                raise
            existing = client.table(TABLE).select("id").eq("id", payload["id"]).limit(1).execute()
            if getattr(existing, "data", None) or payload["request_id"] == payload["id"]:
                return  # our own row already landed (a retried insert)
            # A client reused an id: keep its id in client_request_id, use our own row id.
            retry = dict(payload, request_id=payload["id"])
            client.table(TABLE).insert(retry).execute()

    def set_step(self, step: str) -> None:
        self.step = step

    def note_error(self, exc: BaseException) -> None:
        try:
            self.error = describe_exception(exc)
        except Exception:
            pass

    def update(self, **fields: Any) -> None:
        data = _clean(fields)
        if not data:
            return
        row_id = self.id
        _writer.submit(self.request_id, lambda client: client.table(TABLE).update(data).eq("id", row_id).execute())

    def enrich(self, build: Callable[[Any], dict]) -> None:
        """Fields that need a DB lookup are resolved on the writer thread, off the request path."""
        row_id = self.id

        def _apply(client: Any) -> None:
            data = _clean(build(client) or {})
            if data:
                client.table(TABLE).update(data).eq("id", row_id).execute()

        _writer.submit(self.request_id, _apply)

    def running(self, **fields: Any) -> None:
        if self.finalized:
            return
        self.status = "running"
        self.update(status="running", **fields)

    def _final(self, status: str, fields: dict) -> None:
        if self.finalized:
            return
        self.finalized = True
        self.status = status
        finished = utc_now()
        duration_ms = int((finished - self.started_at).total_seconds() * 1000)
        self.update(status=status, finished_at=finished, duration_ms=duration_ms, **fields)

    def succeed(self, **fields: Any) -> None:
        self._final("succeeded", dict(fields, failed_step=None))

    def fail(
        self,
        step: Optional[str],
        *,
        user_message: Optional[str] = None,
        error: Any = None,
        http_status: Optional[int] = None,
        **fields: Any,
    ) -> None:
        resolved = step if step in FAILED_STEPS else "unknown"
        if not self.finalized:
            self.failed_step = resolved
        if isinstance(error, BaseException):
            error = describe_exception(error)
        self._final(
            "failed",
            dict(fields, failed_step=resolved, user_message=user_message, error_detail=error, http_status=http_status),
        )


class _NullAttempt:
    """Stand-in when no attempt exists (keeps call sites free of None checks)."""

    id = None
    request_id = None
    status = "started"
    step = None
    failed_step = None
    error = None
    finalized = True
    attempt_type = None

    def __getattr__(self, _name: str) -> Callable[..., None]:
        return lambda *args, **kwargs: None


NULL_ATTEMPT = _NullAttempt()


def begin(attempt_type: str, endpoint: str, **fields: Any) -> AttemptRecord | _NullAttempt:
    try:
        return AttemptRecord(attempt_type=attempt_type, endpoint=endpoint, **fields)
    except Exception as exc:
        log_write_failed(endpoint, exc)
        return NULL_ATTEMPT


def from_request(request: Any) -> AttemptRecord | _NullAttempt:
    try:
        return getattr(request.state, "attempt", None) or NULL_ATTEMPT
    except Exception:
        return NULL_ATTEMPT


def record_user_message_for_generation(generation_id: Optional[str], message: str) -> None:
    """WhatsApp watcher: the exact failure text it sent for a worker look."""
    if not generation_id:
        return
    data = _clean({"user_message": message})
    _writer.submit(
        f"generation:{generation_id}",
        lambda client: client.table(TABLE).update(data).eq("generation_id", generation_id).execute(),
    )


def link_tryon_result(
    request_id: Optional[str], shop_id: str, result_path: str, generation_id: Optional[str] = None
) -> None:
    """push-to-screen: attach the stored try-on result to the attempt that made it."""
    client_id = valid_uuid(request_id)
    if not client_id:
        return
    data = {"tryon_result_path": result_path}
    if generation_id:
        data["generation_id"] = generation_id

    def _apply(client: Any) -> None:
        for column in ("request_id", "client_request_id"):
            result = (
                client.table(TABLE)
                .update(data)
                .eq(column, client_id)
                .eq("shop_id", shop_id)
                .in_("attempt_type", ["tryon", "look_tryon"])
                .execute()
            )
            if getattr(result, "data", None):
                return

    _writer.submit(client_id, _apply)


# ---------------------------------------------------------------- enrichment builders
# Run on the writer thread (off the request path). Each returns the fields to store.

_IST = timezone(timedelta(hours=5, minutes=30))


def _ist_label(value: Any) -> str:
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(_IST)
        return moment.strftime("%d %b %Y, %I:%M %p IST")
    except Exception:
        return str(value)


def inputs_builder(
    shop_id: str,
    *,
    fabrics: Optional[list[dict]] = None,
    hero_image_id: Optional[str] = None,
    hero_from_garment_type_id: Optional[str] = None,
    source_generation_id: Optional[str] = None,
    slot_labels_garment_type_id: Optional[str] = None,
) -> Callable[[Any], dict]:
    """fabrics: [{fabric_image_id, apply_to, slot_label?, sort_order}] -> fabric_inputs with storage paths.
    A built-in default slot is {default_fabric: "white"|"black", ...} with no fabric_image_id:
    it is recorded as "Default white" / "Default black" with no storage path.
    Missing slot labels are looked up from slot_labels_garment_type_id's fabric slots.
    Hero: by hero image id, or a garment type's default hero, or (look try-on) the source look.
    The customer photo is never part of this."""

    def build(client: Any) -> dict:
        out: dict[str, Any] = {}
        if fabrics is not None:
            labels: dict[str, Any] = {}
            if slot_labels_garment_type_id and any(not f.get("slot_label") for f in fabrics):
                rows = (
                    client.table("garment_fabric_slots")
                    .select("label, apply_to")
                    .eq("folder_id", slot_labels_garment_type_id)
                    .eq("shop_id", shop_id)
                    .execute()
                ).data or []
                labels = {str(r.get("apply_to") or "").strip().casefold(): r.get("label") for r in rows}
            ids = [str(f["fabric_image_id"]) for f in fabrics if f.get("fabric_image_id")]
            paths: dict[str, str] = {}
            if ids:
                rows = (
                    client.table("fabric_images").select("id, storage_path").in_("id", ids).eq("shop_id", shop_id).execute()
                ).data or []
                paths = {str(r["id"]): r.get("storage_path") for r in rows}
            out["fabric_inputs"] = [
                {
                    "slot_label": f.get("slot_label") or labels.get(str(f.get("apply_to") or "").strip().casefold()),
                    "apply_to": f.get("apply_to"),
                    "sort_order": f.get("sort_order"),
                    "storage_path": None if f.get("default_fabric") else paths.get(str(f.get("fabric_image_id"))),
                    # There is no colour-fix feature for fabric images.
                    "colour_corrected": False,
                    **(
                        {"default_fabric": f["default_fabric"], "fabric_label": f"Default {f['default_fabric']}"}
                        if f.get("default_fabric")
                        else {}
                    ),
                }
                for f in fabrics
            ]
            out["fabric_mode"] = "multi" if len(fabrics) > 1 else "single"

        hero_id = hero_image_id
        if not hero_id and hero_from_garment_type_id:
            rows = (
                client.table("garment_types").select("default_hero_image_id").eq("id", hero_from_garment_type_id).limit(1).execute()
            ).data or []
            hero_id = rows[0].get("default_hero_image_id") if rows else None
        if hero_id:
            rows = (
                client.table("hero_images").select("storage_path").eq("id", hero_id).eq("shop_id", shop_id).limit(1).execute()
            ).data or []
            if rows:
                out["hero_path"] = rows[0].get("storage_path")
                out["hero_colour_corrected"] = False

        if source_generation_id:
            rows = (
                client.table("generations")
                .select("output_path, original_output_path, folder_id")
                .eq("id", source_generation_id)
                .eq("shop_id", shop_id)
                .limit(1)
                .execute()
            ).data or []
            if rows:
                out["hero_path"] = rows[0].get("output_path")
                out["hero_colour_corrected"] = bool(rows[0].get("original_output_path"))
                if rows[0].get("folder_id"):
                    out["garment_type_id"] = rows[0]["folder_id"]
        return out

    return build


def prompt_version_builder(
    garment_type_id: Optional[str], prompt_field: str, template: Optional[str], is_fallback: bool
) -> Callable[[Any], dict]:
    """Label the prompt: the saved version it matches, or say plainly that it matches none."""

    def build(client: Any) -> dict:
        if is_fallback:
            return {"prompt_version_label": "built-in fallback (garment type has no saved prompt)"}
        if not garment_type_id:
            return {}
        rows = (
            client.table("garment_prompt_versions")
            .select("id, created_at, note, look_prompt, tryon_prompt, look_tryon_prompt")
            .eq("garment_type_id", garment_type_id)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        ).data or []
        if rows and (rows[0].get(prompt_field) or "").strip() == (template or "").strip():
            note = (rows[0].get("note") or "").strip()
            label = f"saved {_ist_label(rows[0].get('created_at'))}" + (f" · {note}" if note else "")
            return {"prompt_version_id": rows[0]["id"], "prompt_version_label": label}
        return {"prompt_version_label": "current garment prompt (not saved as a version)"}

    return build


# ---------------------------------------------------------------- worker side (synchronous)

class WorkerAttempt:
    """The worker finishes look attempts the API started, found by generation_id."""

    LOOKUP_RETRIES = 3
    LOOKUP_DELAY_SECONDS = 1.0

    def __init__(self, generation_id: str) -> None:
        self.generation_id = generation_id
        self.step = "upload"
        self._row: Optional[dict] = None

    def _lookup(self, retries: int) -> Optional[dict]:
        if self._row:
            return self._row
        for attempt_no in range(retries + 1):
            try:
                result = (
                    _client()
                    .table(TABLE)
                    .select("id, request_id, started_at, status")
                    .eq("generation_id", self.generation_id)
                    .order("started_at", desc=True)
                    .limit(1)
                    .execute()
                )
                rows = getattr(result, "data", None) or []
            except Exception as exc:
                log_write_failed(f"generation:{self.generation_id}", exc)
                return None
            if rows:
                self._row = rows[0]
                return self._row
            if attempt_no < retries:
                time.sleep(self.LOOKUP_DELAY_SECONDS)
        if retries:
            log_write_failed(f"generation:{self.generation_id}", "no attempt row for this generation")
        return None

    def update(self, **fields: Any) -> None:
        try:
            row = self._lookup(retries=0)
            if not row:
                return
            (
                _client()
                .table(TABLE)
                .update(_clean(fields))
                .eq("id", row["id"])
                .in_("status", list(OPEN_STATUSES))
                .execute()
            )
        except Exception as exc:
            log_write_failed(f"generation:{self.generation_id}", exc)

    def _finish(self, status: str, fields: dict) -> None:
        try:
            row = self._lookup(retries=self.LOOKUP_RETRIES)
            if not row:
                return
            finished = utc_now()
            duration_ms = None
            try:
                started = datetime.fromisoformat(str(row.get("started_at")).replace("Z", "+00:00"))
                duration_ms = int((finished - started).total_seconds() * 1000)
            except Exception:
                pass
            payload = _clean(dict(fields, status=status, finished_at=finished, duration_ms=duration_ms))
            (
                _client()
                .table(TABLE)
                .update(payload)
                .eq("id", row["id"])
                # A slow queue can outlast the 5-minute sweep: the real outcome corrects
                # 'timed_out', but never overwrites another final status.
                .in_("status", [*OPEN_STATUSES, "timed_out"])
                .execute()
            )
        except Exception as exc:
            log_write_failed(f"generation:{self.generation_id}", exc)

    def succeed(self, **fields: Any) -> None:
        self._finish("succeeded", dict(fields, failed_step=None, error_detail=None))

    def fail(self, step: Optional[str], *, error: Any = None, user_message: Optional[str] = None, **fields: Any) -> None:
        resolved = step if step in FAILED_STEPS else "unknown"
        if isinstance(error, BaseException):
            error = describe_exception(error)
        data = dict(fields, failed_step=resolved, error_detail=error)
        if user_message is not None:
            # Otherwise left alone: the WhatsApp watcher may already have recorded the text it sent.
            data["user_message"] = user_message
        self._finish("failed", data)


# ---------------------------------------------------------------- housekeeping (worker)

def sweep_timed_out(now: Optional[datetime] = None) -> int:
    """Rows still started/running 5 minutes after they began become timed_out."""
    now = now or utc_now()
    try:
        result = (
            _client()
            .table(TABLE)
            .update(
                {
                    "status": "timed_out",
                    "failed_step": "timeout",
                    "finished_at": iso(now),
                    "error_detail": "No final status was recorded within 5 minutes of the start.",
                }
            )
            .in_("status", list(OPEN_STATUSES))
            .lt("started_at", iso(now - STUCK_AFTER))
            .execute()
        )
        return len(getattr(result, "data", None) or [])
    except Exception as exc:
        log_write_failed("sweep", exc)
        return 0


def delete_older_than_retention(now: Optional[datetime] = None) -> int:
    """Retention: delete log ROWS older than 90 days. Never touches images."""
    now = now or utc_now()
    try:
        result = _client().table(TABLE).delete().lt("created_at", iso(now - RETENTION)).execute()
        return len(getattr(result, "data", None) or [])
    except Exception as exc:
        log_write_failed("retention", exc)
        return 0


# ---------------------------------------------------------------- HTTP middleware (API process)

# POST paths that start an image generation -> (attempt_type, internal endpoint name)
ATTEMPT_ROUTES: dict[str, tuple[str, str]] = {
    "/generations": ("look", "web_look"),
    "/tryon/": ("look_tryon", "web_tryon_from_look_json"),
    "/tryon/v2": ("look_tryon", "web_tryon_from_look"),
    "/tryon/quick": ("tryon", "web_tryon_quick_json"),
    "/tryon/quick/v2": ("tryon", "web_tryon_quick"),
    "/tryon/multi/v2": ("tryon", "web_tryon_multi"),
}


def _step_for_status(status_code: int, current_step: Optional[str]) -> str:
    if status_code in (401, 403):
        return "auth"
    if status_code == 402:
        return "credits"
    if status_code == 422:
        return "validation"
    if status_code >= 500 and current_step in (None, "validation"):
        # A server error while checking inputs is not the request's fault.
        return "unknown"
    if current_step:
        return current_step
    return "validation" if 400 <= status_code < 500 else "unknown"


def _message_from_body(body: bytes, payload: Any) -> Optional[str]:
    if isinstance(payload, dict) and "detail" in payload:
        detail = payload["detail"]
        if isinstance(detail, str):
            return detail
        return json.dumps(detail, ensure_ascii=False)[:USER_MESSAGE_MAX]
    text = body.decode("utf-8", errors="replace").strip()
    return text[:USER_MESSAGE_MAX] or None


def _with_request_id(body: bytes, request_id: Optional[str]) -> bytes:
    """Add "request_id" as the first key of a JSON object body, without parsing it."""
    if not request_id or b'"request_id"' in body:
        return body
    stripped = body.lstrip()
    if not stripped.startswith(b"{"):
        return body  # not an object (list, string...): the header still carries the id
    field = b'"request_id":' + json.dumps(request_id).encode("utf-8")
    rest = stripped[1:].lstrip()
    return b"{" + field + (b"}" if rest.startswith(b"}") else b"," + rest)


class AttemptLogMiddleware:
    """Pure ASGI. For generation POSTs only:
    - opens the attempt row at arrival, before auth / validation / body parsing;
    - adds X-Request-Id to the response and "request_id" to JSON object bodies;
    - if the handler did not finalize (auth/422 before it ran, an HTTPException, or a crash),
      writes 'failed' with the step, the exact text returned and the HTTP status.
    It never reads the request body, so the customer photo is never seen here."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        route = ATTEMPT_ROUTES.get(scope.get("path", "")) if scope.get("type") == "http" else None
        if not route or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or []}
        record = begin(
            route[0],
            route[1],
            client_request_id=headers.get("x-client-request-id"),
            user_agent=headers.get("user-agent"),
        )
        scope.setdefault("state", {})["attempt"] = record
        request_id = (record.request_id or "").encode("latin-1")

        response: dict[str, Any] = {"status": None, "body": b"", "payload": None}
        pending_start: dict[str, Any] = {}

        async def send_wrapper(message: dict) -> None:
            if message["type"] == "http.response.start":
                response["status"] = message["status"]
                out_headers = [h for h in message.get("headers", []) if h[0].lower() != b"x-request-id"]
                if request_id:
                    out_headers.append((b"x-request-id", request_id))
                content_type = next(
                    (v.decode("latin-1") for k, v in out_headers if k.lower() == b"content-type"), ""
                )
                message = dict(message, headers=out_headers)
                if content_type.startswith("application/json"):
                    pending_start["message"] = message  # held until the body is complete
                    return
                await send(message)
                return

            if message["type"] == "http.response.body" and "message" in pending_start:
                response["body"] += message.get("body", b"")
                if message.get("more_body"):
                    return
                body = response["body"]
                if response["status"] < 400:
                    # Success bodies can be multi-MB base64 images: splice, never re-parse.
                    body = _with_request_id(body, record.request_id)
                else:
                    try:
                        payload = json.loads(body) if body else None
                    except ValueError:
                        payload = None
                    response["payload"] = payload
                    if isinstance(payload, dict) and record.request_id:
                        payload.setdefault("request_id", record.request_id)
                        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                start = pending_start.pop("message")
                start["headers"] = [h for h in start["headers"] if h[0].lower() != b"content-length"] + [
                    (b"content-length", str(len(body)).encode("latin-1"))
                ]
                await send(start)
                await send({"type": "http.response.body", "body": body})
                return

            if message["type"] == "http.response.body" and response["status"] and response["status"] >= 400:
                response["body"] += message.get("body", b"")[:USER_MESSAGE_MAX]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except BaseException as exc:
            if not record.finalized:
                import asyncio

                if isinstance(exc, asyncio.CancelledError):
                    record.fail("client_network", error="Request cancelled before a response (client disconnected).")
                else:
                    record.fail(
                        _step_for_status(500, record.step),
                        user_message="Internal Server Error",
                        error=exc,
                        http_status=500,
                    )
            print(f"[attempt] request_id={record.request_id} endpoint={route[1]} crashed status={record.status}", flush=True)
            raise

        status_code = response["status"] or 500
        try:
            self._finish(record, route, status_code, response)
        except Exception as exc:
            log_write_failed(record.request_id, exc)
        finally:
            # One searchable line per attempt in the server logs ("search this in Render logs").
            print(
                f"[attempt] request_id={record.request_id} endpoint={route[1]} "
                f"http={status_code} status={record.status} step={record.failed_step or record.step or '-'}",
                flush=True,
            )

    @staticmethod
    def _finish(record: Any, route: tuple[str, str], status_code: int, response: dict) -> None:
        if record.finalized:
            return
        if status_code >= 400:
            error = f"HTTP {status_code}: {scrub(response['body'].decode('utf-8', errors='replace'), 1000)}"
            if record.error:
                error = f"{error}\n--- cause ---\n{record.error}"
            record.fail(
                _step_for_status(status_code, record.step),
                user_message=_message_from_body(response["body"], response["payload"]),
                error=error,
                http_status=status_code,
            )
        elif record.status == "started":
            log_write_failed(record.request_id, f"{route[1]} returned {status_code} without recording an outcome")
            record.fail(
                "unknown",
                error=f"Handler returned HTTP {status_code} without recording an outcome (logging bug).",
                http_status=status_code,
            )


def effective_status(row: dict, now: Optional[datetime] = None) -> str:
    """'timed_out' for rows stuck in started/running > 5 min, even before the sweep runs."""
    status = str(row.get("status") or "")
    if status not in OPEN_STATUSES:
        return status
    try:
        started = datetime.fromisoformat(str(row.get("started_at")).replace("Z", "+00:00"))
        if (now or utc_now()) - started > STUCK_AFTER:
            return "timed_out"
    except Exception:
        pass
    return status
