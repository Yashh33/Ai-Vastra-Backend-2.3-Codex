"""GET /healthz/queue: an alarm link for the uptime monitor (never Render's health check).

Not OK when:
- a look has been 'queued' for more than 3 minutes (the worker is not picking jobs up);
- a look has been 'processing' for more than 20 minutes (the worker stopped mid-job);
- at least 3 attempts were made in the last 15 minutes and every one failed or timed out
  (rows that failed on validation / auth / credits / client_network are not counted);
- the database cannot be read ("database").
Only reason words and counts are returned: never shop names or ids. The result is cached in
memory for 60 seconds, so the monitor cannot load the database."""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

import attempt_log

CACHE_SECONDS = 60.0
QUEUED_TOO_LONG = timedelta(minutes=3)
PROCESSING_TOO_LONG = timedelta(minutes=20)
FAILURE_WINDOW = timedelta(minutes=15)
MIN_ATTEMPTS = 3
IGNORED_FAILED_STEPS = frozenset({"validation", "auth", "credits", "client_network"})
_FAILED_STATUSES = frozenset({"failed", "timed_out"})
_MAX_ATTEMPT_ROWS = 1000

_cache: dict[str, Any] = {}
_cache_lock = threading.Lock()


def _count(query: Any) -> int:
    result = query.execute()
    count = getattr(result, "count", None)
    return int(count) if count is not None else len(getattr(result, "data", None) or [])


def evaluate(supabase: Any, now: Optional[datetime] = None) -> tuple[bool, list[str], dict[str, int]]:
    """(ok, reasons, counts). Never raises: a database error is the reason "database"."""
    now = now or datetime.now(timezone.utc)
    reasons: list[str] = []
    counts: dict[str, int] = {}
    try:
        queued = _count(
            supabase.table("generations")
            .select("id", count="exact")
            .eq("status", "queued")
            .lt("created_at", (now - QUEUED_TOO_LONG).isoformat())
            .limit(1)
        )
        processing = _count(
            supabase.table("generations")
            .select("id", count="exact")
            .eq("status", "processing")
            .lt("started_at", (now - PROCESSING_TOO_LONG).isoformat())
            .limit(1)
        )
        attempts = (
            supabase.table(attempt_log.TABLE)
            .select("status, failed_step, started_at")
            .gte("created_at", (now - FAILURE_WINDOW).isoformat())
            .limit(_MAX_ATTEMPT_ROWS)
            .execute()
        ).data or []
    except Exception as exc:
        print(f"[healthz/queue] database read failed: {type(exc).__name__}", flush=True)
        return False, ["database"], {}

    if queued:
        reasons.append("looks_queued_over_3_min")
        counts["looks_queued_over_3_min"] = queued
    if processing:
        reasons.append("looks_processing_over_20_min")
        counts["looks_processing_over_20_min"] = processing

    counted = [row for row in attempts if str(row.get("failed_step") or "") not in IGNORED_FAILED_STEPS]
    failed = [row for row in counted if attempt_log.effective_status(row, now) in _FAILED_STATUSES]
    if len(counted) >= MIN_ATTEMPTS and len(failed) == len(counted):
        reasons.append("all_recent_attempts_failed")
        counts["recent_attempts_failed"] = len(failed)

    return not reasons, reasons, counts


def cached_result(
    supabase_factory: Callable[[], Any], *, clock: Callable[[], float] = time.monotonic
) -> tuple[int, dict]:
    """(http status, body), at most one database check per CACHE_SECONDS."""
    with _cache_lock:
        if _cache and clock() < _cache["expires"]:
            return _cache["status"], dict(_cache["body"])
        try:
            ok, reasons, counts = evaluate(supabase_factory())
        except Exception as exc:  # e.g. the client could not be built
            print(f"[healthz/queue] check failed: {type(exc).__name__}", flush=True)
            ok, reasons, counts = False, ["database"], {}
        body: dict[str, Any] = {"ok": True} if ok else {"ok": False, "reasons": reasons}
        if not ok and counts:
            body["counts"] = counts
        status_code = 200 if ok else 503
        _cache.update(expires=clock() + CACHE_SECONDS, status=status_code, body=body)
        return status_code, dict(body)


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()
