"""Guardrails: prompt-injection detection and PII log redaction.

Re-exports `get_injection_type` (the guard called inside the `reading`
node) and `redact_pii` (the structlog processor wired in by
`core/logging.py`). Note: importing anything from this package pulls in
everything both submodules need -- see `injection.py`'s lazy
`create_logger()` for why that matters (a real circular import this
project hit).
"""

from app.guardrails.injection import get_injection_type
from app.guardrails.pii import redact_pii

__all__ = ["get_injection_type", "redact_pii"]