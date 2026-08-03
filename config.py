from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    APP_ENV: str = "development"

    SUPABASE_URL: str
    SUPABASE_SERVICE_ROLE_KEY: str

    GEMINI_API_KEY: str
    GEMINI_IMAGE_MODEL_ID: str = "gemini-3.1-flash-image-preview"

    # Single source of truth for the credit/image ratio. A future ratio
    # change (e.g. perception pricing) is a one-line edit here.
    CREDITS_PER_IMAGE: int = Field(default=50, gt=0)
    # WhatsApp shadow-shop opening trial, expressed in images (not credits).
    WHATSAPP_FREE_IMAGES: int = Field(default=3, ge=0)
    WORKER_POLL_INTERVAL_SECONDS: float = Field(default=3, gt=0)

    # Used by /admin endpoints. Keep this long and private.
    ADMIN_PANEL_SECRET: str = Field(default="")

    # WhatsApp Cloud API (Meta Graph API)
    WHATSAPP_ACCESS_TOKEN: str = Field(default="")
    WHATSAPP_PHONE_NUMBER_ID: str = Field(default="")
    WHATSAPP_VERIFY_TOKEN: str = Field(default="")
    WHATSAPP_GRAPH_VERSION: str = Field(default="v25.0")

    # Shop that holds the fixed-garment-menu master templates (one
    # hero_folders row + one hero_images row per garment type).
    MASTER_SHOP_ID: str = Field(default="")

    # Razorpay (shared by React checkout and WhatsApp payment links)
    RAZORPAY_KEY_ID: str = Field(default="")
    RAZORPAY_KEY_SECRET: str = Field(default="")
    RAZORPAY_WEBHOOK_SECRET: str = Field(default="")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def SUPABASE_JWKS_URL(self) -> str:
        return self.SUPABASE_URL.rstrip("/") + "/auth/v1/.well-known/jwks.json"


@lru_cache
def get_settings() -> Settings:
    return Settings()
