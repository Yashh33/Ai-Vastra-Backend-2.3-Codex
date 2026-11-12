"""One true credit balance: the existing RPC public.get_shop_credit_balance(uuid), the ledger
total that the look and try-on charges themselves use. Used by GET /me and the admin API.

rpc_balance() never raises: None means "the RPC could not answer", and the caller then falls
back to its previous method (the newest credit_ledger row's balance_after)."""
from __future__ import annotations

import re
import threading
from typing import Any, Optional

RPC_NAME = "get_shop_credit_balance"

# The repo only knows the function as get_shop_credit_balance(uuid). PostgREST calls functions
# by argument NAME, so the usual name is tried first; if PostgREST answers "no such function
# with these arguments" its hint names the real argument, which is then used and remembered.
_DEFAULT_ARG = "p_shop_id"
_arg_name = _DEFAULT_ARG
_arg_lock = threading.Lock()
_warned = False
_HINT_RE = re.compile(RPC_NAME + r"\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)")


def _signature_not_found(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "pgrst202" in text or "could not find the function" in text


def _hinted_arg(exc: BaseException, tried: str) -> Optional[str]:
    """The argument name PostgREST suggests ("Perhaps you meant ...(<name>)"), if not the one tried."""
    return next((name for name in _HINT_RE.findall(str(exc)) if name != tried), None)


def _as_int(data: Any) -> int:
    """A scalar function comes back as a number; be lenient about list / dict wrappers."""
    if isinstance(data, list):
        data = data[0] if data else None
    if isinstance(data, dict):
        data = next(iter(data.values()), None) if data else None
    if data is None:
        return 0  # no ledger rows: same as coalesce(..., 0) in the SQL
    if isinstance(data, bool):
        raise TypeError("unexpected boolean balance")
    return int(float(data))


def _call(supabase: Any, shop_id: str, arg: str) -> int:
    return _as_int(supabase.rpc(RPC_NAME, {arg: shop_id}).execute().data)


def _warn(shop_id: str, exc: BaseException) -> None:
    global _warned
    if _warned:
        return
    _warned = True
    print(
        f"[credit_balance] WARNING: {RPC_NAME} failed, using the newest ledger row instead "
        f"(shop={shop_id}): {type(exc).__name__}: {str(exc)[:200]}",
        flush=True,
    )


def rpc_balance(supabase: Any, shop_id: str) -> Optional[int]:
    global _arg_name
    if not shop_id:
        return None
    arg = _arg_name
    try:
        return _call(supabase, shop_id, arg)
    except Exception as exc:
        hinted = _hinted_arg(exc, arg) if _signature_not_found(exc) else None
        if not hinted:
            _warn(shop_id, exc)
            return None
        try:
            value = _call(supabase, shop_id, hinted)
        except Exception as retry_exc:
            _warn(shop_id, retry_exc)
            return None
        with _arg_lock:
            _arg_name = hinted
        return value


def latest_ledger_balance(supabase: Any, shop_id: str) -> int:
    """The previous method: balance_after of the shop's newest ledger row (raises on failure)."""
    result = (
        supabase.table("credit_ledger")
        .select("balance_after")
        .eq("shop_id", shop_id)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    rows = getattr(result, "data", None) or []
    return int(rows[0].get("balance_after") or 0) if rows else 0


def shop_balance(supabase: Any, shop_id: str) -> int:
    """RPC first; on failure the newest ledger row (which may raise, like before)."""
    value = rpc_balance(supabase, shop_id)
    return value if value is not None else latest_ledger_balance(supabase, shop_id)
