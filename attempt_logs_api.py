"""POST /attempt-logs/client-report: the web app reports a Generate / Try-on click that never
got a response (network error, timeout, CORS). It can only fill an attempt the backend
never finished: a final backend status always wins."""
import threading
import time
from collections import deque
from typing import Literal, Optional

from cachetools import TTLCache
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

import attempt_log
from auth_deps import CurrentShopContext, get_current_shop_context

router = APIRouter(prefix="/attempt-logs", tags=["Attempt logs"])

_RATE_LIMIT_PER_MINUTE = 30
_rate_hits: TTLCache = TTLCache(maxsize=5000, ttl=60)
_rate_lock = threading.Lock()


class ClientReportRequest(BaseModel):
    request_id: str = Field(..., max_length=64)
    attempt_type: Literal["look", "tryon", "look_tryon"]
    garment_type_id: Optional[str] = Field(default=None, max_length=64)
    user_message: Optional[str] = Field(default=None, max_length=1000)
    client_error: Optional[str] = Field(default=None, max_length=1000)
    device_label: Optional[str] = Field(default=None, max_length=80)


def _enforce_rate_limit(shop_id: str) -> None:
    now = time.monotonic()
    with _rate_lock:
        hits: deque = _rate_hits.get(shop_id) or deque()
        while hits and now - hits[0] >= 60:
            hits.popleft()
        if len(hits) >= _RATE_LIMIT_PER_MINUTE:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many reports. Please wait a minute.",
            )
        hits.append(now)
        _rate_hits[shop_id] = hits


def _find_existing(client, request_id: str) -> Optional[dict]:
    for column in ("request_id", "client_request_id"):
        result = (
            client.table(attempt_log.TABLE)
            .select("id, status, shop_id")
            .eq(column, request_id)
            .limit(1)
            .execute()
        )
        rows = getattr(result, "data", None) or []
        if rows:
            return rows[0]
    return None


@router.post("/client-report")
def client_report(
    body: ClientReportRequest,
    request: Request,
    current: CurrentShopContext = Depends(get_current_shop_context),
):
    request_id = attempt_log.valid_uuid(body.request_id)
    if not request_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="request_id must be a UUID")
    _enforce_rate_limit(current.shop_id)

    user_agent = request.headers.get("user-agent")
    now = attempt_log.utc_now()
    report = {
        "status": "failed",
        "failed_step": "client_network",
        "user_message": attempt_log.scrub(body.user_message, attempt_log.USER_MESSAGE_MAX),
        "error_detail": attempt_log.scrub(f"Reported by the app: {body.client_error or 'no response received'}"),
        "finished_at": attempt_log.iso(now),
    }

    client = attempt_log._client()
    existing = _find_existing(client, request_id)

    if existing:
        if existing.get("shop_id") and str(existing["shop_id"]) != str(current.shop_id):
            return {"request_id": request_id, "recorded": False, "reason": "not this shop's attempt"}
        if existing.get("status") != "started":
            # running / succeeded / failed / timed_out: the backend's record stands.
            return {"request_id": request_id, "recorded": False, "reason": "backend status already recorded"}
        result = (
            client.table(attempt_log.TABLE)
            .update(report)
            .eq("id", existing["id"])
            .eq("status", "started")
            .execute()
        )
        return {"request_id": request_id, "recorded": bool(getattr(result, "data", None))}

    payload = dict(
        report,
        request_id=request_id,
        client_request_id=request_id,
        shop_id=current.shop_id,
        user_id=current.auth_user_id,
        user_email=current.email,
        attempt_type=body.attempt_type,
        garment_type_id=attempt_log.valid_uuid(body.garment_type_id),
        device_label=body.device_label or attempt_log.device_label(user_agent),
        user_agent=(user_agent or "")[: attempt_log.USER_AGENT_MAX] or None,
        endpoint="client_report",
        started_at=attempt_log.iso(now),
    )
    try:
        client.table(attempt_log.TABLE).insert(payload).execute()
    except Exception as exc:
        if attempt_log._is_unique_violation(exc):
            # The backend created the row in the meantime; its record stands.
            return {"request_id": request_id, "recorded": False, "reason": "backend status already recorded"}
        raise
    return {"request_id": request_id, "recorded": True}
