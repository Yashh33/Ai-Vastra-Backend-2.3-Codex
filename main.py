from fastapi import Depends, FastAPI

from auth_deps import CurrentShopContext, get_current_shop_context
from config import get_settings
from folders_api import router as folders_router
from generations_api import router as generations_router
from images_api import router as images_router

app = FastAPI(title="Ai Vastra Backend")

app.include_router(folders_router)
app.include_router(images_router)
app.include_router(generations_router)


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
    }


@app.get("/me")
def me(current: CurrentShopContext = Depends(get_current_shop_context)):
    return {
        "auth_user_id": current.auth_user_id,
        "email": current.email,
        "shop_id": current.shop_id,
        "role": current.role,
    }
