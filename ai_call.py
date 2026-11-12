"""Gemini calls for looks (worker) and try-ons (API): time limit, one retry, failure reason codes.

- Time limit: GEMINI_TIMEOUT_SECONDS (default 150). google-genai waits forever unless
  HttpOptions.timeout is set, and that value is in MILLISECONDS (checked against
  google-genai 1.64.0: _api_client.get_timeout_in_seconds divides it by 1000).
- One retry of the AI call only (never of a credit charge), and only for "the AI service is
  busy": a timeout, a dropped connection, or a Google 429/500/502/503/504. A model that
  answered but refused or returned no image is never retried: that is a response, not an error.
- Reason codes: ai_busy | ai_blocked | ai_no_image.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable, Optional, TypeVar

import httpcore
import httpx
from google.genai import errors as genai_errors
from google.genai import types

from config import get_settings

T = TypeVar("T")

AI_BUSY = "ai_busy"
AI_BLOCKED = "ai_blocked"
AI_NO_IMAGE = "ai_no_image"
ERROR_CODES = frozenset({AI_BUSY, AI_BLOCKED, AI_NO_IMAGE})

AI_BUSY_DETAIL = "The AI service is busy. Please try again in a few minutes."
RETRY_DELAY_SECONDS = 5.0
_RETRY_GOOGLE_CODES = frozenset({429, 500, 502, 503, 504})

try:  # the async client uses aiohttp when it is installed
    import aiohttp

    _AIOHTTP_CONNECTION_ERRORS: tuple[type[BaseException], ...] = (aiohttp.ClientConnectionError,)
    _AIOHTTP_TIMEOUT_ERRORS: tuple[type[BaseException], ...] = (aiohttp.ServerTimeoutError,)
except Exception:  # pragma: no cover - aiohttp is optional
    _AIOHTTP_CONNECTION_ERRORS = ()
    _AIOHTTP_TIMEOUT_ERRORS = ()

_TIMEOUT_ERRORS: tuple[type[BaseException], ...] = (
    httpx.TimeoutException,
    httpcore.TimeoutException,
    asyncio.TimeoutError,
    TimeoutError,
    *_AIOHTTP_TIMEOUT_ERRORS,
)
_CONNECTION_ERRORS: tuple[type[BaseException], ...] = (
    httpx.TransportError,
    httpcore.NetworkError,
    httpcore.RemoteProtocolError,
    ConnectionError,
    *_AIOHTTP_CONNECTION_ERRORS,
)


def http_options() -> types.HttpOptions:
    """For genai.Client(http_options=...): the time limit, in milliseconds."""
    seconds = float(get_settings().GEMINI_TIMEOUT_SECONDS)
    return types.HttpOptions(timeout=max(1, int(seconds * 1000)))


def busy_kind(exc: BaseException) -> Optional[str]:
    """'timeout' | 'connection' | 'google' when the AI service was busy or unreachable, else None.
    Only a few causes deep, so a wrapped transport error is still recognised."""
    current: Optional[BaseException] = exc
    for _ in range(4):
        if current is None:
            break
        if isinstance(current, _TIMEOUT_ERRORS):
            return "timeout"
        if isinstance(current, _CONNECTION_ERRORS):
            return "connection"
        if isinstance(current, genai_errors.APIError):
            try:
                return "google" if int(current.code) in _RETRY_GOOGLE_CODES else None
            except (TypeError, ValueError):
                return None
        current = current.__cause__ or current.__context__
    return None


def is_busy_error(exc: BaseException) -> bool:
    return busy_kind(exc) is not None


def _should_retry(exc: BaseException, retry_on_timeout: bool) -> bool:
    kind = busy_kind(exc)
    if kind is None:
        return False
    return retry_on_timeout or kind != "timeout"


def _log_retry(label: str, exc: BaseException, delay: float) -> None:
    kind = busy_kind(exc)
    print(f"[{label}] AI call failed ({kind}: {type(exc).__name__}), retrying once in {delay:g}s", flush=True)


def call_with_retry(
    fn: Callable[[], T],
    *,
    retry_on_timeout: bool,
    label: str = "ai",
    sleep: Optional[Callable[[float], Any]] = None,
    delay: Optional[float] = None,
) -> T:
    """Run one AI call; at most ONE more try, and only for a busy/unreachable AI service."""
    delay = RETRY_DELAY_SECONDS if delay is None else delay
    try:
        return fn()
    except Exception as exc:
        if not _should_retry(exc, retry_on_timeout):
            raise
        _log_retry(label, exc, delay)
    (sleep or time.sleep)(delay)
    return fn()


async def call_with_retry_async(
    fn: Callable[[], Awaitable[T]],
    *,
    retry_on_timeout: bool,
    label: str = "ai",
    sleep: Optional[Callable[[float], Awaitable[Any]]] = None,
    delay: Optional[float] = None,
) -> T:
    delay = RETRY_DELAY_SECONDS if delay is None else delay
    try:
        return await fn()
    except Exception as exc:
        if not _should_retry(exc, retry_on_timeout):
            raise
        _log_retry(label, exc, delay)
    await (sleep or asyncio.sleep)(delay)
    return await fn()
