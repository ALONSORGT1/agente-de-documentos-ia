"""Un cliente HTTP reutilizable con reintentos acotados del SDK oficial."""
from functools import lru_cache

from openai import OpenAI

from app.core.config import settings
from app.core.errors import ConfigurationError


@lru_cache(maxsize=1)
def get_openai_client():
    if not settings.openai_api_key:
        raise ConfigurationError("Falta OPENAI_API_KEY en .env. Reinicia el servidor tras configurarla.")
    return OpenAI(api_key=settings.openai_api_key, timeout=settings.api_timeout_seconds,
                  max_retries=settings.openai_max_retries)
