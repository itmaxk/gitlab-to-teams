import logging
import os

logger = logging.getLogger(__name__)

try:
    from dotenv import load_dotenv as _load_dotenv
except ImportError:
    _load_dotenv = None


def reload_dotenv():
    """Перечитывает .env файл и обновляет модульные константы."""
    if _load_dotenv is not None:
        _load_dotenv(override=True)
        logger.info("Reloaded .env file")

    # Обновляем модульные константы, которые читаются при импорте
    import services.review_service as _review
    _review.MAX_DIFF_CHARS = int(os.getenv("REVIEW_MAX_DIFF_CHARS", "60000"))
