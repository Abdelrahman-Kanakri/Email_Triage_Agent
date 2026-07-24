"""Core application utilities: settings singleton and (future) logging.
The `settings` singleton is imported from here to avoid circular imports. 
The `get_logger` function is also imported from here to avoid circular imports.

"""

from app.core.config import settings
from app.core.logging import get_logger

__all__ = ["settings", "get_logger"]