from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

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


def get_current_shop_context(
    token: str = Depends(get_bearer_token),
) -> CurrentShopContext:
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

    # Optional shop suspension gate (requires shops.is_suspended column).
    try:
        shop_result = (
            supabase.table("shops")
            .select("is_suspended")
            .eq("id", shop_id)
            .limit(1)
            .execute()
        )
        shop_rows = getattr(shop_result, "data", None) or []
        if shop_rows and bool(shop_rows[0].get("is_suspended", False)):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Shop access is suspended",
            )
    except HTTPException:
        raise
    except Exception as exc:
        # Backward-compatible fallback if column migration is not applied yet.
        if "is_suspended" not in str(exc).lower():
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to resolve shop status",
            ) from exc

    return CurrentShopContext(
        auth_user_id=auth_user_id,
        email=email,
        shop_id=shop_id,
        role=str(row["role"]),
    )
