"""Credits for web try-ons and look try-ons: charged before the AI call, refunded once if it
fails. Both RPCs (sql/2026-11-04_tryon_credits_and_results.sql) are keyed by the attempt id,
so a call repeated after a dropped connection never charges or refunds twice."""
from typing import Any, Optional

from fastapi import HTTPException, status

import attempt_log
from supabase_client import retry_once

# Same status and text as POST /generations.
INSUFFICIENT_CREDITS_DETAIL = "Insufficient credits"


def _rows(result: Any) -> list[dict]:
    data = getattr(result, "data", None)
    return data if isinstance(data, list) else ([data] if isinstance(data, dict) else [])


def _function_missing(exc: Exception) -> bool:
    text = str(exc).lower()
    return "pgrst202" in text or "could not find the function" in text or "42883" in text


def charge(supabase: Any, *, shop_id: str, charge_key: str, amount: int) -> int:
    """Credits charged. 402 if the shop has too few. 0 (with a warning) only while the SQL
    file has not been run yet, so try-ons keep working as before."""
    try:
        result = retry_once(
            lambda: supabase.rpc(
                "debit_credits_for_tryon",
                {"p_shop_id": shop_id, "p_attempt_id": charge_key, "p_amount": amount},
            ).execute()
        )
    except HTTPException:
        raise
    except Exception as exc:
        if "insufficient credits" in str(exc).lower():
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail=INSUFFICIENT_CREDITS_DETAIL,
            ) from exc
        if _function_missing(exc):
            print(
                "[tryon_credits] WARNING: debit_credits_for_tryon is missing "
                "(run sql/2026-11-04_tryon_credits_and_results.sql); try-on not charged"
            )
            return 0
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to charge credits",
        ) from exc

    rows = _rows(result)
    return int(rows[0].get("credits_used") or 0) if rows else 0


def refund(supabase: Any, *, shop_id: str, charge_key: str) -> Optional[int]:
    """Credits refunded (0 if nothing was charged). None if the refund call itself failed,
    so the log cannot say. Never raises."""
    try:
        result = retry_once(
            lambda: supabase.rpc(
                "refund_credits_for_tryon",
                {"p_shop_id": shop_id, "p_attempt_id": charge_key},
            ).execute()
        )
    except Exception as exc:
        print(f"[tryon_credits] ERROR: refund failed for attempt {charge_key}: {attempt_log.scrub(str(exc), 300)}")
        return None
    rows = _rows(result)
    if not rows or not rows[0].get("refunded"):
        return 0
    return int(rows[0].get("refund_amount") or 0)
