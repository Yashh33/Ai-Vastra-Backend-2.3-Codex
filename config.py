from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    APP_ENV: str = "development"

    SUPABASE_URL: str
    SUPABASE_SERVICE_ROLE_KEY: str

    GEMINI_API_KEY: str
    GEMINI_IMAGE_MODEL_ID: str = "gemini-3-pro-image-preview"

    CREDITS_PER_GENERATION: int = Field(default=1, gt=0)
    WORKER_POLL_INTERVAL_SECONDS: float = Field(default=3, gt=0)

    # Used by /admin endpoints. Keep this long and private.
    ADMIN_PANEL_SECRET: str = Field(default="")

    # WhatsApp Cloud API (Meta Graph API)
    WHATSAPP_ACCESS_TOKEN: str = Field(default="")
    WHATSAPP_PHONE_NUMBER_ID: str = Field(default="")
    WHATSAPP_VERIFY_TOKEN: str = Field(default="")
    WHATSAPP_GRAPH_VERSION: str = Field(default="v25.0")

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
