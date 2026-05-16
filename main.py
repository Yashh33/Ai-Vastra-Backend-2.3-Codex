from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from admin_api import router as admin_router
from auth_deps import CurrentShopContext, get_current_shop_context
from config import get_settings
from folders_api import router as folders_router
from garment_types_api import router as garment_types_router
from generations_api import router as generations_router
from images_api import router as images_router

app = FastAPI(title="Ai Vastra Backend")

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
app.include_router(admin_router)


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
