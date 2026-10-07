"""PII processor: email addresses never reach the log in plaintext."""

import hashlib
import hmac

from app.core import settings
from app.guardrails.pii import redact_pii


def _hmac(addr: str) -> str:
    key = settings.PII_HMAC_SECRET.get_secret_value().encode()
    return hmac.new(key, addr.encode(), hashlib.sha256).hexdigest()


def test_addresses_in_kwargs_are_pseudonymized():
    out = redact_pii(None, "info", {"event": "x", "sender": "alice@example.com"})
    assert out["sender"] == _hmac("alice@example.com")


def test_addresses_embedded_in_free_text_are_pseudonymized():
    out = redact_pii(None, "info", {"event": "Sending from a@x.com to b@y.org now"})
    assert "a@x.com" not in out["event"] and "b@y.org" not in out["event"]
    assert _hmac("a@x.com") in out["event"]


def test_same_address_same_pseudonym():
    first = redact_pii(None, "info", {"event": "alice@example.com"})["event"]
    second = redact_pii(None, "info", {"event": "alice@example.com"})["event"]
    assert first == second


def test_non_string_values_untouched():
    out = redact_pii(None, "info", {"event": "ok", "count": 3, "ids": ["a@x.com"]})
    assert out["count"] == 3
