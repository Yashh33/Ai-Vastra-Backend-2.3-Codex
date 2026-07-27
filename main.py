import threading

from cachetools import TTLCache
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from admin_api import router as admin_router
from auth_deps import CurrentShopContext, get_current_shop_context
from config import get_settings
from folders_api import router as folders_router
from garment_types_api import router as garment_types_router
from generations_api import router as generations_router
from images_api import router as images_router
from payments_api import router as payments_router
from supabase_client import get_supabase_admin_client
from tryon_api import router as tryon_router
from whatsapp_api import router as whatsapp_router
from whatsapp_watcher import start_completion_watcher

app = FastAPI(title="Ai Vastra Backend")

app.add_middleware(GZipMiddleware, minimum_size=1000)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://192.168.1.9:5173",
        "http://192.168.1.7:5173",
        "https://ai-vastra-reactjs-demo-2-2-codex.onrender.com",
        "http://localhost:5174",
        "http://127.0.0.1:5174",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


app.include_router(folders_router)
app.include_router(garment_types_router)
app.include_router(images_router)
app.include_router(generations_router)
app.include_router(tryon_router)
app.include_router(admin_router)
app.include_router(whatsapp_router)
app.include_router(payments_router)


@app.on_event("startup")
async def _start_whatsapp_completion_watcher() -> None:
    start_completion_watcher()


@app.get("/health")
def health():
    settings = get_settings()

    return {
        "status": "ok",
        "app_env": settings.APP_ENV,
        "credits_per_generation": settings.CREDITS_PER_GENERATION,
        "supabase_configured": bool(
            settings.SUPABASE_URL and settings.SUPABASE_SERVICE_ROLE_KEY
        ),
        "gemini_api_configured": bool(settings.GEMINI_API_KEY),
        "gemini_image_model_id": settings.GEMINI_IMAGE_MODEL_ID,
        "whatsapp_configured": bool(
            settings.WHATSAPP_ACCESS_TOKEN and settings.WHATSAPP_PHONE_NUMBER_ID
        ),
    }


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
