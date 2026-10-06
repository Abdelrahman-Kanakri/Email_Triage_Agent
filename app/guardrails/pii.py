"""Structlog processor that pseudonymizes email addresses in log output.

Wired into `core/logging.py`'s processor chain, right before
`JSONRenderer()` -- runs on every log call automatically (opt-out, not
opt-in), so redaction can't be forgotten the way a hand-called helper
could be: put the security control at the layer everyone passes
through, not the layer everyone has to remember to opt into.

Scans log values by content (a regex address pattern), not by key --
`send_email`'s log line embeds addresses inside an f-string, not as
`sender=`/`to=` kwargs, so a key-based lookup would miss real PII.
Regex, not presidio: the field and the address shape are already known
here, so there's no "unknown location" for presidio's NER to discover --
its whole value proposition doesn't apply to this data.

Each matched address is replaced by its HMAC-SHA256 digest, keyed by
`PII_HMAC_SECRET` -- not a plain hash, because email addresses are
low-entropy/guessable and a plain hash could be reversed by dictionary
attack. HMAC is one-way even with the key: there is no "decrypt", only
recompute-and-compare, which is exactly what makes it safe to log. The
same address always produces the same digest, preserving per-sender
traceability (needed for Phase 4) without ever logging plaintext.

This pseudonymization only touches the *logged* copy. `send_email`
keeps reading the real `Email.sender`/`Email.to` from `State`, never
the hash -- the hash isn't reversible and would break sending.
"""
import hashlib
import hmac
import re

from app.core import settings


def redact_pii(logger, method_name: str, event_dict: dict):
    """Replace every email address found in `event_dict`'s string values
    with its HMAC-SHA256 pseudonym. See module docstring for the full
    reasoning -- this is the mechanism, not the "why".
    """

    pattern = r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9.-]+"
    for key, value in event_dict.items():
        if isinstance(value, str):
            msg = re.sub(pattern, lambda m: hmac.new(
                settings.PII_HMAC_SECRET.get_secret_value().encode(),
                m.group(0).encode(),
                hashlib.sha256).hexdigest(), value)
            event_dict[key] = msg
    return event_dict