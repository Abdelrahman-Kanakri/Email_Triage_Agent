"""
...
"""
import hashlib
import hmac
import re

from app.core import settings

secret_key = settings.PII_HMAC_SECRET.encode()
def redact_pii(logger, method_name: str, event_dict: dict):
    """
    Redacts personally identifiable information (PII) from the event dictionary.
    """

    pattern = r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9.-]+"
    for key, value in event_dict.items():
        if isinstance(value, str):
            msg = re.sub(pattern, lambda m: hmac.new(
                secret_key,
                m.group(0).encode(),
                hashlib.sha256).hexdigest(), value)
            event_dict[key] = msg
    return event_dict