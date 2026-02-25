from functools import lru_cache

from supabase import create_client

from config import get_settings


@lru_cache
def get_supabase_admin_client():
    settings = get_settings()
    return create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY)
