import os
import threading

from cachetools import TTLCache
from dotenv import dotenv_values
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse

from admin_api import router as admin_router
from attempt_log import AttemptLogMiddleware
from attempt_log import flush as flush_attempt_log
from attempt_logs_api import router as attempt_logs_router
from auth_deps import CurrentShopContext, get_current_shop_context
from config import get_settings
from folders_api import router as folders_router
from garment_types_api import router as garment_types_router
from generations_api import router as generations_router
from images_api import router as images_router
from payments_api import router as payments_router
from segment_api import router as segment_router
from supabase_client import get_supabase_admin_client
from tryon_api import router as tryon_router
from whatsapp_api import router as whatsapp_router
from whatsapp_watcher import start_completion_watcher

app = FastAPI(title="Ai Vastra Backend")

# Added first so it sits innermost (inside GZip): it must see the uncompressed JSON.
app.add_middleware(AttemptLogMiddleware)
app.add_middleware(GZipMiddleware, minimum_size=1000)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://192.168.1.9:5173",
        "http://192.168.1.7:5173",
        "https://ai-vastra-reactjs-demo-2-2-codex.onrender.com",
        "https://screen.mytryonai.in",
        "http://localhost:5174",
        "http://127.0.0.1:5174",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-Id"],
)


app.include_router(folders_router)
app.include_router(garment_types_router)
app.include_router(images_router)
app.include_router(generations_router)
app.include_router(tryon_router)
app.include_router(admin_router)
app.include_router(whatsapp_router)
app.include_router(payments_router)
app.include_router(segment_router)
app.include_router(attempt_logs_router)


@app.on_event("shutdown")
def _flush_attempt_log() -> None:
    # Don't lose queued attempt-log writes on a deploy/restart.
    flush_attempt_log(timeout=5.0)


@app.on_event("startup")
async def _start_whatsapp_completion_watcher() -> None:
    start_completion_watcher()


@app.get("/health")
def health():
    settings = get_settings()

    return {
        "status": "ok",
        "app_env": settings.APP_ENV,
        "credits_per_generation": settings.CREDITS_PER_IMAGE,
        "supabase_configured": bool(
            settings.SUPABASE_URL and settings.SUPABASE_SERVICE_ROLE_KEY
        ),
        "gemini_api_configured": bool(settings.GEMINI_API_KEY),
        "gemini_image_model_id": settings.GEMINI_IMAGE_MODEL_ID,
        "whatsapp_configured": bool(
            settings.WHATSAPP_ACCESS_TOKEN and settings.WHATSAPP_PHONE_NUMBER_ID
        ),
    }


# Environment variables the live service needs. Variables with safe defaults
# (APP_ENV, GEMINI_IMAGE_MODEL_ID, CREDITS_PER_IMAGE, WHATSAPP_FREE_IMAGES,
# WORKER_POLL_INTERVAL_SECONDS, WHATSAPP_GRAPH_VERSION) are deliberately not listed.
_HEALTHZ_REQUIRED_ENV = (
    "SUPABASE_URL",
    "SUPABASE_SERVICE_ROLE_KEY",
    "GEMINI_API_KEY",
    "ADMIN_PANEL_SECRET",
    "WHATSAPP_ACCESS_TOKEN",
    "WHATSAPP_PHONE_NUMBER_ID",
    "WHATSAPP_VERIFY_TOKEN",
    "MASTER_SHOP_ID",
    "RAZORPAY_KEY_ID",
    "RAZORPAY_KEY_SECRET",
    "RAZORPAY_WEBHOOK_SECRET",
    "FAL_KEY",
)


@app.get("/healthz", include_in_schema=False)
def healthz():
    """Render health check: no auth, no database access. Reports only the NAMES
    of missing environment variables, never their values."""
    # Same sources Settings uses: the process environment, then a local .env file.
    dotenv_file = dotenv_values(".env") if os.path.exists(".env") else {}
    missing = [
        name
        for name in _HEALTHZ_REQUIRED_ENV
        if not (os.environ.get(name) or dotenv_file.get(name) or "").strip()
    ]
    if missing:
        return JSONResponse(status_code=500, content={"ok": False, "missing": missing})
    return {"ok": True}


_me_shop_summary_cache: TTLCache = TTLCache(maxsize=1000, ttl=60)
_me_shop_summary_lock = threading.Lock()


def _get_shop_summary(shop_id: str) -> dict:
    with _me_shop_summary_lock:
        cached = _me_shop_summary_cache.get(shop_id)
        if cached is not None:
            return cached

    shop_name = None
    header_display_text = None
    credits_balance = 0

    try:
        supabase = get_supabase_admin_client()

        shop_result = (
            supabase.table("shops")
            .select("name, header_display_text")
            .eq("id", shop_id)
            .execute()
        )
        shop_rows = getattr(shop_result, "data", None) or []
        if shop_rows:
            shop_name = shop_rows[0].get("name")
            header_display_text = shop_rows[0].get("header_display_text")

        ledger_result = (
            supabase.table("credit_ledger")
            .select("balance_after")
            .eq("shop_id", shop_id)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        ledger_rows = getattr(ledger_result, "data", None) or []
        if ledger_rows:
            credits_balance = int(ledger_rows[0].get("balance_after") or 0)
    except Exception:
        shop_name = None
        header_display_text = None
        credits_balance = 0

    summary = {
        "shop_name": shop_name,
        "header_display_text": header_display_text,
        "credits_balance": credits_balance,
    }

    with _me_shop_summary_lock:
        _me_shop_summary_cache[shop_id] = summary

    return summary


@app.get("/me")
def me(current: CurrentShopContext = Depends(get_current_shop_context)):
    summary = _get_shop_summary(current.shop_id)

    return {
        "auth_user_id": current.auth_user_id,
        "email": current.email,
        "shop_id": current.shop_id,
        "role": current.role,
        "shop_name": summary["shop_name"],
        "header_display_text": summary["header_display_text"],
        "credits_balance": summary["credits_balance"],
    }
