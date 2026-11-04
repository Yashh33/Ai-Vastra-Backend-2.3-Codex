import threading
import time
from typing import Any, Callable, Optional, TypeVar

import httpcore
import httpx
from fastapi import HTTPException, status
from supabase import create_client

from config import get_settings

T = TypeVar("T")

CONNECTION_PROBLEM_DETAIL = "Temporary connection problem. Please try again."
RETRY_DELAY_SECONDS = 0.3

_local = threading.local()


def _thread_client():
    client = getattr(_local, "client", None)
    if client is None:
        settings = get_settings()
        client = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY)
        _local.client = client
    return client


def reset_thread_client() -> None:
    """Drop this thread's client: the next call opens fresh connections."""
    _local.client = None


class _ThreadLocalClient:
    """Stands in for a supabase Client. Every attribute (table, rpc, storage, auth) is looked up
    on the CALLING thread's own client, so this object can be passed between threads safely.

    The library's HTTP/2 connections are not safe to share between threads: a shared client
    fails at random with httpx.ReadError ([Errno 11] Resource temporarily unavailable). The
    worker already uses one client per thread for the same reason."""

    def __getattr__(self, name: str) -> Any:
        return getattr(_thread_client(), name)


_CLIENT = _ThreadLocalClient()


def get_supabase_admin_client():
    return _CLIENT


# ---------------------------------------------------------------- dropped connections

# Raised by httpx/httpcore when a connection drops or times out: the request may simply
# be sent again. Anything else (a 4xx, a database error) is a real answer and never retried.
TRANSIENT_ERROR_TYPES: tuple[type[BaseException], ...] = (
    httpx.TimeoutException,
    httpx.NetworkError,
    httpx.RemoteProtocolError,
    httpcore.TimeoutException,
    httpcore.NetworkError,
    httpcore.RemoteProtocolError,
)
_GATEWAY_STATUSES = {502, 503, 504}


class SupabaseUnavailable(HTTPException):
    """Still no connection after the one retry. Shown to the user as a 503."""

    def __init__(self) -> None:
        super().__init__(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=CONNECTION_PROBLEM_DETAIL)


def is_transient_error(exc: BaseException) -> bool:
    if isinstance(exc, TRANSIENT_ERROR_TYPES):
        return True
    # A gateway in front of the database / storage answered 502/503/504. Only the Supabase
    # libraries' own errors: an HTTPException or a Gemini error with that code is not this.
    if not type(exc).__module__.startswith(("postgrest", "storage3")):
        return False
    for attr in ("code", "status"):
        value = getattr(exc, attr, None)
        try:
            if value is not None and int(value) in _GATEWAY_STATUSES:
                return True
        except (TypeError, ValueError):
            continue
    return False


def caused_by_connection_problem(exc: BaseException) -> bool:
    """True if exc, or an error it was raised from, is a dropped connection."""
    seen = 0
    current: Optional[BaseException] = exc
    while current is not None and seen < 6:
        if isinstance(current, SupabaseUnavailable) or is_transient_error(current):
            return True
        current = current.__cause__ or current.__context__
        seen += 1
    return False


def _is_duplicate(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "23505" in text or "duplicate key" in text


def retry_once(fn: Callable[[], T], *, duplicate_ok: bool = False) -> T:
    """Run one database or storage call; on a dropped connection, wait briefly and run it once
    more on a fresh client. Only for calls that are safe to repeat (reads, updates, upserts,
    signing, inserts with their own id when duplicate_ok, idempotent RPCs).

    duplicate_ok: an insert whose retry hits a unique key means the first try did land.
    Still failing after the retry: SupabaseUnavailable (503)."""
    try:
        return fn()
    except Exception as exc:
        if not is_transient_error(exc):
            raise
        print(f"[supabase] connection problem ({type(exc).__name__}), retrying once")

    reset_thread_client()
    time.sleep(RETRY_DELAY_SECONDS)
    try:
        return fn()
    except Exception as exc:
        if duplicate_ok and _is_duplicate(exc):
            return None  # type: ignore[return-value]
        if is_transient_error(exc):
            raise SupabaseUnavailable() from exc
        raise
