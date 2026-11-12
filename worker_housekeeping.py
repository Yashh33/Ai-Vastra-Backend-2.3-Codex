"""Worker-only housekeeping: called from worker.run_attempt_log_housekeeping, never from the API.

- sweep_stuck_looks (every 2 min): looks left 'processing' for 15+ minutes (the worker died or
  was redeployed mid-job) are failed and refunded through the existing RPC
  mark_generation_failed_with_refund, which refunds at most once and refuses 'done' rows.
- sweep_missed_tryon_refunds (every 2 min): charged web try-ons whose refund did not happen
  (the request died after the charge, or the refund call failed) are refunded through the
  existing RPC refund_credits_for_tryon, which refunds at most once per attempt.
- delete_expired_pushed_tryons (hourly): customer try-ons pushed to the TV from the web app
  (POST /tryon/push-to-screen) are deleted after TRYON_RESULT_RETENTION_DAYS.

Every function handles one row at a time: one failing row never stops the others, and none
of them raises into the worker's main loop."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

import attempt_log
import tryon_credits
import tryon_results
from config import get_settings
from image_thumbs import look_thumbnail_path

GENERATED_OUTPUTS_BUCKET = "generated-outputs"

STUCK_LOOK_AFTER = timedelta(minutes=15)
STUCK_LOOK_ERROR = "Timed out: the worker stopped before finishing"
STUCK_LOOK_MAX = 20

TRYON_REFUND_MIN_AGE = timedelta(minutes=10)  # a younger attempt may still be running
TRYON_REFUND_WINDOW = timedelta(hours=24)
TRYON_REFUND_MAX = 20
_TRYON_TYPES = ["tryon", "look_tryon"]

PUSHED_BATCH = 100
PUSHED_MAX_BATCHES = 5

_warned: set[str] = set()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _short(exc: BaseException) -> str:
    return attempt_log.scrub(f"{type(exc).__name__}: {exc}", 300) or type(exc).__name__


def _warn_once(key: str, message: str) -> None:
    if key not in _warned:
        _warned.add(key)
        print(message, flush=True)


def _rows(result: Any) -> list[dict]:
    data = getattr(result, "data", None)
    return data if isinstance(data, list) else ([data] if isinstance(data, dict) else [])


# ---------------------------------------------------------------- 2a. stuck looks

def _completed_refusal(exc: BaseException) -> bool:
    return "cannot refund a completed generation" in str(exc).lower()


def sweep_stuck_looks(client: Any, active_ids: Iterable[str] = (), now: Optional[datetime] = None) -> int:
    """Fail + refund looks stuck in 'processing' for 15+ minutes. Returns how many were failed."""
    now = now or _utc_now()
    skip = {str(gid) for gid in active_ids}
    try:
        rows = (
            client.table("generations")
            .select("id, shop_id")
            .eq("status", "processing")
            .lt("started_at", (now - STUCK_LOOK_AFTER).isoformat())
            .order("started_at")
            .limit(STUCK_LOOK_MAX + len(skip))
            .execute()
        ).data or []
    except Exception as exc:
        print(f"[worker] stuck-look sweep: read failed: {_short(exc)}", flush=True)
        return 0

    failed = 0
    for row in [row for row in rows if str(row.get("id")) not in skip][:STUCK_LOOK_MAX]:
        generation_id, shop_id = str(row.get("id")), str(row.get("shop_id"))
        try:
            result = client.rpc(
                "mark_generation_failed_with_refund",
                {"p_generation_id": generation_id, "p_shop_id": shop_id, "p_error": STUCK_LOOK_ERROR},
            ).execute()
        except Exception as exc:
            if _completed_refusal(exc):
                continue  # it finished after all: nothing to do
            print(f"[worker] stuck-look sweep: generation={generation_id} not failed: {_short(exc)}", flush=True)
            continue

        info = (_rows(result) or [{}])[0]
        refunded = int(info.get("refund_amount") or 0) if info.get("refunded") else 0
        failed += 1
        print(
            f"[worker] stuck-look sweep: failed generation={generation_id} shop={shop_id} refunded={refunded}",
            flush=True,
        )
        try:
            attempt = attempt_log.WorkerAttempt(generation_id)
            attempt.LOOKUP_RETRIES = 0  # the row exists by now, or never will: don't wait for it
            attempt.fail("timeout", error=STUCK_LOOK_ERROR, credits_refunded=refunded)
        except Exception as exc:  # WorkerAttempt never raises; belt and braces
            print(f"[worker] stuck-look sweep: attempt log not updated for {generation_id}: {_short(exc)}", flush=True)
    return failed


# ---------------------------------------------------------------- 2b. try-on refunds that did not happen

def _tryon_refund_query(client: Any, now: datetime, status: str, limit: int):
    return (
        client.table(attempt_log.TABLE)
        .select("id, shop_id, credits_charged, status")
        .in_("attempt_type", _TRYON_TYPES)
        .not_.is_("shop_id", "null")
        .gt("credits_charged", 0)
        .is_("credits_refunded", "null")
        .eq("status", status)
        .gte("created_at", (now - TRYON_REFUND_WINDOW).isoformat())
        .lt("started_at", (now - TRYON_REFUND_MIN_AGE).isoformat())
        .order("started_at")
        .limit(limit)
    )


def find_missed_tryon_refunds(client: Any, now: Optional[datetime] = None) -> list[dict]:
    """Charged try-on attempts, 10 min to 24 h old, never refunded, that failed, or timed out
    without any result having been kept (no admin copy, not pushed to the TV)."""
    now = now or _utc_now()
    rows = _tryon_refund_query(client, now, "failed", TRYON_REFUND_MAX).execute().data or []
    room = TRYON_REFUND_MAX - len(rows)
    if room > 0:
        try:
            rows += (
                _tryon_refund_query(client, now, "timed_out", room)
                .is_("result_copy_path", "null")
                .is_("tryon_result_path", "null")
                .execute()
            ).data or []
        except Exception as exc:
            if not attempt_log._is_missing_column(exc):
                raise
            # Without result_copy_path we cannot tell whether the customer got the result.
            _warn_once(
                "result_copy_path",
                "[worker] try-on refund sweep: WARNING: result_copy_path missing "
                "(run sql/2026-11-04_tryon_credits_and_results.sql); timed-out try-ons skipped",
            )
    return rows


def sweep_missed_tryon_refunds(client: Any, now: Optional[datetime] = None) -> int:
    """Refund each one once and record the amount in credits_refunded. Returns how many were done."""
    try:
        rows = find_missed_tryon_refunds(client, now)
    except Exception as exc:
        print(f"[worker] try-on refund sweep: read failed: {_short(exc)}", flush=True)
        return 0

    done = 0
    for row in rows:
        attempt_id, shop_id = str(row.get("id")), str(row.get("shop_id"))
        try:
            amount = tryon_credits.refund(client, shop_id=shop_id, charge_key=attempt_id)
            if amount is None:
                continue  # the refund call failed (already logged): try again next sweep
            (
                client.table(attempt_log.TABLE)
                .update({"credits_refunded": amount})
                .eq("id", attempt_id)
                .is_("credits_refunded", "null")
                .execute()
            )
            done += 1
            print(f"[worker] try-on refund sweep: attempt={attempt_id} shop={shop_id} refunded={amount}", flush=True)
        except Exception as exc:
            print(f"[worker] try-on refund sweep: attempt={attempt_id} failed: {_short(exc)}", flush=True)
    return done


# ---------------------------------------------------------------- 6. pushed customer try-ons

def pushed_output_path_ok(path: Optional[str], shop_id: str, generation_id: str) -> bool:
    """Only "<shop_id>/<generation_id>/output..." (no further folders) is ever deleted."""
    if not path or not shop_id or not generation_id:
        return False
    pattern = rf"^{re.escape(str(shop_id))}/{re.escape(str(generation_id))}/output[^/\\]*$"
    return bool(re.match(pattern, str(path))) and ".." not in str(path)


def _is_pushed_tryon(row: dict) -> bool:
    try:
        credits = int(row.get("credits_used") or 0)
    except (TypeError, ValueError):
        return False
    return (
        row.get("generation_type") == "tryon"
        and row.get("prompt_used") == tryon_results.PUSHED_TRYON_PROMPT
        and credits == 0
    )


def _delete_one_pushed(client: Any, row: dict, now_iso: str) -> bool:
    """True if the row was deleted, False if it was skipped. Raises on a failing call."""
    generation_id, shop_id = str(row.get("id")), str(row.get("shop_id"))
    output_path = row.get("output_path")
    if not _is_pushed_tryon(row):
        return False  # belt and braces: the query already selects only these
    if not pushed_output_path_ok(output_path, shop_id, generation_id):
        _warn_once(
            f"path:{generation_id}",
            f"[worker] pushed try-on cleanup: skipped generation={generation_id} (unexpected path shape)",
        )
        return False

    ledger = (
        client.table("credit_ledger").select("id").eq("generation_id", generation_id).limit(1).execute()
    ).data or []
    if ledger:
        return False  # money points at it: never delete

    (
        client.table("shop_screen_state")
        .update({"live_generation_id": None, "updated_at": now_iso})
        .eq("shop_id", shop_id)
        .eq("live_generation_id", generation_id)
        .execute()
    )
    client.storage.from_(GENERATED_OUTPUTS_BUCKET).remove([output_path, look_thumbnail_path(output_path)])
    (
        client.table("generations")
        .delete()
        .eq("id", generation_id)
        .eq("shop_id", shop_id)
        .eq("generation_type", "tryon")
        .eq("prompt_used", tryon_results.PUSHED_TRYON_PROMPT)
        .eq("credits_used", 0)
        .execute()
    )
    print(f"[worker] pushed try-on cleanup: deleted generation={generation_id} shop={shop_id}", flush=True)
    return True


def delete_expired_pushed_tryons(client: Any, now: Optional[datetime] = None) -> int:
    """Hourly. Only rows made by POST /tryon/push-to-screen: generation_type 'tryon', the exact
    marker prompt, credits_used 0, older than TRYON_RESULT_RETENTION_DAYS. Never looks, never
    WhatsApp rows, never pushed look copies. Batches of 100, at most 5 per run."""
    now = now or _utc_now()
    cutoff = now - timedelta(days=get_settings().TRYON_RESULT_RETENTION_DAYS)
    now_iso = now.isoformat()
    deleted = 0
    skipped = 0  # skipped rows stay in the result set, so the next page starts after them

    for _ in range(PUSHED_MAX_BATCHES):
        try:
            rows = (
                client.table("generations")
                .select("id, shop_id, output_path, generation_type, prompt_used, credits_used")
                .eq("generation_type", "tryon")
                .eq("prompt_used", tryon_results.PUSHED_TRYON_PROMPT)
                .eq("credits_used", 0)
                .lt("created_at", cutoff.isoformat())
                .order("created_at")
                .order("id")
                .range(skipped, skipped + PUSHED_BATCH - 1)
                .execute()
            ).data or []
        except Exception as exc:
            print(f"[worker] pushed try-on cleanup: read failed: {_short(exc)}", flush=True)
            break

        for row in rows:
            try:
                if _delete_one_pushed(client, row, now_iso):
                    deleted += 1
                else:
                    skipped += 1
            except Exception as exc:
                skipped += 1
                print(f"[worker] pushed try-on cleanup: generation={row.get('id')} skipped: {_short(exc)}", flush=True)

        if len(rows) < PUSHED_BATCH:
            break
    return deleted
