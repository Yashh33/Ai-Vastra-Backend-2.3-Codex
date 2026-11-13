from functools import lru_cache
from typing import NamedTuple

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    APP_ENV: str = "development"

    SUPABASE_URL: str
    SUPABASE_SERVICE_ROLE_KEY: str

    GEMINI_API_KEY: str
    GEMINI_IMAGE_MODEL_ID: str = "gemini-3.1-flash-image-preview"
    # Output size of every generated image (looks and try-ons), for every model.
    GEMINI_IMAGE_SIZE: str = "1K"
    # Time limit for one Gemini call (looks and try-ons). Optional; the default is fine.
    GEMINI_TIMEOUT_SECONDS: float = Field(default=150, gt=0)
    # Days a small copy of each web try-on result is kept for the admin (then deleted by the worker).
    TRYON_RESULT_RETENTION_DAYS: int = Field(default=7, ge=1)
    # Hours every finished web try-on is kept for the shop (GET /tryon/recent), then deleted by the worker.
    TRYON_SHOP_RESULT_HOURS: int = Field(default=24, ge=1)

    # Single source of truth for the credit/image ratio. A future ratio
    # change (e.g. perception pricing) is a one-line edit here.
    CREDITS_PER_IMAGE: int = Field(default=50, gt=0)
    # WhatsApp shadow-shop opening trial, expressed in images (not credits).
    WHATSAPP_FREE_IMAGES: int = Field(default=3, ge=0)
    WORKER_POLL_INTERVAL_SECONDS: float = Field(default=3, gt=0)
    # Worker only: "false" switches off the background fill-in of thumbnails / view copies for
    # existing pictures. Unset or anything else = on.
    PICTURE_BACKFILL_ENABLED: str = Field(default="true")
    # Worker only: the backfill does no work while the worker uses more memory than this (MB).
    # Blank or unreadable = 320.
    PICTURE_BACKFILL_MAX_MEMORY_MB: str = Field(default="320")

    # Used by /admin endpoints. Keep this long and private.
    ADMIN_PANEL_SECRET: str = Field(default="")

    # WhatsApp Cloud API (Meta Graph API)
    WHATSAPP_ACCESS_TOKEN: str = Field(default="")
    WHATSAPP_PHONE_NUMBER_ID: str = Field(default="")
    WHATSAPP_VERIFY_TOKEN: str = Field(default="")
    WHATSAPP_GRAPH_VERSION: str = Field(default="v25.0")
    # "false" turns the WhatsApp completion watcher off (always do this locally:
    # the local database is the live one). Unset or anything else = on.
    WHATSAPP_WATCHER_ENABLED: str = Field(default="true")

    # Shop that holds the fixed-garment-menu master templates (one
    # hero_folders row + one hero_images row per garment type).
    MASTER_SHOP_ID: str = Field(default="")

    # Razorpay (shared by React checkout and WhatsApp payment links)
    RAZORPAY_KEY_ID: str = Field(default="")
    RAZORPAY_KEY_SECRET: str = Field(default="")
    RAZORPAY_WEBHOOK_SECRET: str = Field(default="")

    # fal.ai (SAM 3 segmentation). Never log or return this.
    FAL_KEY: str = Field(default="")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def SUPABASE_JWKS_URL(self) -> str:
        return self.SUPABASE_URL.rstrip("/") + "/auth/v1/.well-known/jwks.json"


# The only models a shop can be set to: id -> (label, short label).
# The admin panel keeps the same labels in src/lib/imageModels.ts.
IMAGE_MODELS: dict[str, tuple[str, str]] = {
    "gemini-nano-banana-2.1": ("Nano Banana 2.1", "NB 2.1"),
    "gemini-3.1-flash-image-preview": ("Nano Banana 2", "NB 2"),
}


class GeminiPrice(NamedTuple):
    """USD per 1M tokens."""

    input: float
    image_output: float
    text_output: float  # text and "thinking" output


# AI cost for the Operations log: USD per 1M tokens, from Google's Gemini API pricing page
# (standard paid tier, Nov 2026). A model not listed here gets ai_cost_inr = null; never a guess.
# Update these numbers when Google changes prices; the logic does not need to change.
GEMINI_PRICES_USD_PER_1M: dict[str, GeminiPrice] = {
    "gemini-nano-banana-2.1": GeminiPrice(input=1.50, image_output=30.00, text_output=7.50),
    "gemini-3.1-flash-image-preview": GeminiPrice(input=0.50, image_output=60.00, text_output=3.00),
    "gemini-3.1-flash-image": GeminiPrice(input=0.50, image_output=60.00, text_output=3.00),
}
USD_TO_INR = 84.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
