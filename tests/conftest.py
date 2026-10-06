"""Test env defaults so `app.core.config.Settings()` validates without a real .env.

Only variables already in the shell win (`setdefault`). `.env` does not:
pydantic-settings ranks real env vars above it, so tests always run on
these dummy values, with LangSmith tracing off. Nothing here touches the
network: `ChatOpenAI` only builds a client at import time.
"""
import os

_DEFAULTS = {
    "LANGSMITH_API_KEY": "test",
    "LANGSMITH_ENDPOINT": "https://example.invalid",
    "LANGSMITH_TRACING": "false",
    "LANGSMITH_PROJECT": "test",
    "OPENAI_API_KEY": "test",
    "OPENAI_REGURAL_MODEL": "test-REGURAL",
    "OPENAI_MINI_MODEL": "test-mini",
    "EMAILS_DATA_PATH": ".",
    "PII_HMAC_SECRET": "test-secret",
}

for key, value in _DEFAULTS.items():
    os.environ.setdefault(key, value)
