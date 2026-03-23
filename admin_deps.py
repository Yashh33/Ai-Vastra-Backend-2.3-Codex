from fastapi import Header, HTTPException, status

from config import get_settings


def verify_admin_secret(x_admin_secret: str | None = Header(default=None)) -> None:
    settings = get_settings()
    configured = (settings.ADMIN_PANEL_SECRET or "").strip()

    if not configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Admin panel secret is not configured on backend",
        )

    provided = (x_admin_secret or "").strip()
    if not provided or provided != configured:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid admin secret",
        )
