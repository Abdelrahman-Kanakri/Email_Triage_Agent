from app.core.logging import get_logger

logger = get_logger(__name__)
logger.info("test_log", sender="alice@example.com", to="bob@example.com")
