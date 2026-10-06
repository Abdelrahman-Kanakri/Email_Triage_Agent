"""Application settings loaded from ``.env`` via pydantic-settings.

Import the module-level ``settings`` singleton — never instantiate ``Settings``
directly elsewhere, as that would bypass the singleton and re-read the file.
"""
import os

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed application settings, loaded from ``.env`` at import time.

    Fields without a default (the API keys and model names) are *required* —
    pydantic raises a ``ValidationError`` on startup if any is missing from the
    environment, so the app fails loudly rather than running half-configured.
    Fields with a default (thresholds, chunk sizes, collection name) may be
    overridden via ``.env`` but are safe to omit. The module-level ``settings``
    singleton below is what the rest of the app imports.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        
    )
    # ── LangSmith configurations ─────────────────────────────────────────────────────────────
    LANGSMITH_API_KEY: SecretStr  
    LANGSMITH_ENDPOINT: str
    LANGSMITH_TRACING: str
    LANGSMITH_PROJECT: str 

    # ── OpenAI API & Models Names ─────────────────────────────────────────────────────────────

    OPENAI_API_KEY: SecretStr
    OPENAI_REGURAL_MODEL: str
    OPENAI_MINI_MODEL: str
    
    # ── Emails Data Path ─────────────────────────────────────────────────────────────
    EMAILS_DATA_PATH: str
    
    # ── PII HMAC Secret ─────────────────────────────────────────────────────────────
    PII_HMAC_SECRET: SecretStr

# MAIN
settings = Settings()

# Settings the observability 
os.environ["LANGSMITH_API_KEY"] = settings.LANGSMITH_API_KEY.get_secret_value()
os.environ["LANGSMITH_ENDPOINT"] = settings.LANGSMITH_ENDPOINT
os.environ["LANGSMITH_TRACING"] =  settings.LANGSMITH_TRACING
os.environ["LANGSMITH_PROJECT"] = settings.LANGSMITH_PROJECT