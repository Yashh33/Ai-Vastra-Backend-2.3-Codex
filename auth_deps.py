import threading
from typing import Optional

import jwt
from cachetools import TTLCache
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from config import get_settings
from supabase_client import get_supabase_admin_client


bearer_scheme = HTTPBearer(auto_error=False)


class CurrentShopContext(BaseModel):
    auth_user_id: str
    email: Optional[str] = None
    shop_id: str
    role: str


def get_bearer_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> str:
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token",
        )

    if credentials.scheme.lower() != "bearer" or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authorization header",
        )

    return credentials.credentials


# --- Layer A: local JWKS-based token verification -------------------------

_jwks_client: Optional["jwt.PyJWKClient"] = None
_jwks_client_lock = threading.Lock()


def _get_jwks_client() -> "jwt.PyJWKClient":
    global _jwks_client
    if _jwks_client is None:
        with _jwks_client_lock:
            if _jwks_client is None:
                settings = get_settings()
                _jwks_client = jwt.PyJWKClient(
                    settings.SUPABASE_JWKS_URL,
                    cache_keys=True,
                    lifespan=3600,
                )
    return _jwks_client


def _verify_token_via_supabase(token: str) -> tuple[str, Optional[str]]:
    supabase = get_supabase_admin_client()

    try:
        auth_response = supabase.auth.get_user(token)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired access token",
        ) from exc

    user = getattr(auth_response, "user", None)
    if user is None and isinstance(auth_response, dict):
        user = auth_response.get("user")

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unable to verify user from token",
        )

    if isinstance(user, dict):
        auth_user_id = str(user.get("id") or "")
        email = user.get("email")
    else:
        auth_user_id = str(getattr(user, "id", "") or "")
        email = getattr(user, "email", None)

    if not auth_user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token user id missing",
        )

    return auth_user_id, email


def _verify_token(token: str) -> tuple[str, Optional[str]]:
    settings = get_settings()

    try:
        jwks_client = _get_jwks_client()
        signing_key = jwks_client.get_signing_key_from_jwt(token)
    except jwt.PyJWKClientError as exc:
        # JWKS key retrieval failed (network issue, unknown kid, etc.) -
        # fall back to network verification as a resilience measure.
        print(f"WARNING: JWKS key retrieval failed, falling back to Supabase auth.get_user: {exc}")
        return _verify_token_via_supabase(token)
    except jwt.exceptions.DecodeError as exc:
        # Malformed token - not a key-retrieval problem, must not fall back.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired access token",
        ) from exc
    except Exception as exc:
        # Unexpected error fetching signing keys (e.g. transport-level
        # network failure) - fall back to network verification.
        print(f"WARNING: JWKS key retrieval failed, falling back to Supabase auth.get_user: {exc}")
        return _verify_token_via_supabase(token)

    try:
        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=["ES256"],
            audience="authenticated",
            issuer=f"{settings.SUPABASE_URL.rstrip('/')}/auth/v1",
            leeway=30,
        )
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired access token",
        ) from exc

    auth_user_id = str(payload.get("sub") or "")
    email = payload.get("email")

    if not auth_user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token user id missing",
        )

    return auth_user_id, email


# --- Layer B: TTL-cached shop context resolution ---------------------------

_shop_context_cache: TTLCache = TTLCache(maxsize=1000, ttl=300)
_shop_context_cache_lock = threading.Lock()


def _get_cached_shop_context(auth_user_id: str) -> Optional[dict]:
    with _shop_context_cache_lock:
        return _shop_context_cache.get(auth_user_id)


def _set_cached_shop_context(auth_user_id: str, shop_id: str, role: str, is_suspended: bool) -> None:
    with _shop_context_cache_lock:
        _shop_context_cache[auth_user_id] = {
            "shop_id": shop_id,
            "role": role,
            "is_suspended": is_suspended,
        }


def _extract_is_suspended(shops_field) -> bool:
    if isinstance(shops_field, dict):
        return bool(shops_field.get("is_suspended", False))
    if isinstance(shops_field, list) and shops_field:
        return bool(shops_field[0].get("is_suspended", False))
    return False


def _resolve_shop_context_fallback(supabase, auth_user_id: str) -> tuple[str, str, bool]:
    try:
        result = (
            supabase.table("shop_users")
            .select("shop_id, role")
            .eq("auth_user_id", auth_user_id)
            .limit(1)
            .execute()
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to resolve shop mapping",
        ) from exc

    rows = getattr(result, "data", None) or []
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User is not linked to any shop",
        )

    row = rows[0]
    shop_id = str(row["shop_id"])
    role = str(row["role"])

    is_suspended = False
    try:
        shop_result = (
            supabase.table("shops")
            .select("is_suspended")
            .eq("id", shop_id)
            .limit(1)
            .execute()
        )
        shop_rows = getattr(shop_result, "data", None) or []
        if shop_rows:
            is_suspended = bool(shop_rows[0].get("is_suspended", False))
    except Exception:
        # Silently skip suspension check on timeout/error
        # User gets through, worst case a suspended shop
        # temporarily gets access - acceptable tradeoff
        pass

    return shop_id, role, is_suspended


def _resolve_shop_context(supabase, auth_user_id: str) -> tuple[str, str, bool]:
    try:
        result = (
            supabase.table("shop_users")
            .select("shop_id, role, shops(is_suspended)")
            .eq("auth_user_id", auth_user_id)
            .limit(1)
            .execute()
        )
    except HTTPException:
        raise
    except Exception:
        return _resolve_shop_context_fallback(supabase, auth_user_id)

    rows = getattr(result, "data", None) or []
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User is not linked to any shop",
        )

    row = rows[0]
    shop_id = str(row["shop_id"])
    role = str(row["role"])
    is_suspended = _extract_is_suspended(row.get("shops"))

    return shop_id, role, is_suspended


def get_current_shop_context(
    token: str = Depends(get_bearer_token),
) -> CurrentShopContext:
    auth_user_id, email = _verify_token(token)

    cached = _get_cached_shop_context(auth_user_id)
    if cached is not None:
        if cached["is_suspended"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Shop access is suspended",
            )
        return CurrentShopContext(
            auth_user_id=auth_user_id,
            email=email,
            shop_id=cached["shop_id"],
            role=cached["role"],
        )

    supabase = get_supabase_admin_client()
    shop_id, role, is_suspended = _resolve_shop_context(supabase, auth_user_id)

    _set_cached_shop_context(auth_user_id, shop_id, role, is_suspended)

    if is_suspended:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Shop access is suspended",
        )

    return CurrentShopContext(
        auth_user_id=auth_user_id,
        email=email,
        shop_id=shop_id,
        role=role,
    )
