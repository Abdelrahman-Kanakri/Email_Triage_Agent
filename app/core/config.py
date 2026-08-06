"""Application settings loaded from ``.env`` via pydantic-settings.

Import the module-level ``settings`` singleton — never instantiate ``Settings``
directly elsewhere, as that would bypass the singleton and re-read the file.
"""

from pydantic import Field
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
    )
    # ── LangSmith configurations ─────────────────────────────────────────────────────────────
    LANGSMITH_API_KEY: str = Field(..., env="LANGSMITH_API_KEY")
    LANGSMITH_ENDPOINT: str = Field(..., env="LANGSMITH_ENDPOINT")
    LANGSMITH_TRACING: bool = Field(..., env="LANGSMITH_TRACING")
    LANGSMITH_PROJECT: str = Field(..., env="LANGSMITH_PROJECT")

    # ── Mistral API & Models Names ─────────────────────────────────────────────────────────────

    MISTRAL_API_KEY: str = Field(..., env="MISTRAL_API_KEY")
    LARGE_MODEL_NAME: str = Field(..., env="LARGE_MODEL_NAME")
    MEDIUM_MODEL_NAME: str = Field(..., env="MEDIUM_MODEL_NAME")
    SMALL_MODEL_NAME: str = Field(..., env="SMALL_MODEL_NAME")
    MISTRAL_EMBEDDING_MODEL_NAME: str = Field(..., env="MISTRAL_EMBEDDING_MODEL_NAME")
    
    # ── Emails Data Path ─────────────────────────────────────────────────────────────
    EMAILS_DATA_PATH: str = Field(..., env="EMAILS_DATA_PATH")
    
    # ── PII HMAC Secret ─────────────────────────────────────────────────────────────
    PII_HMAC_SECRET: str = Field(..., env="PII_HMAC_SECRET")


# MAIN
settings = Settings()
