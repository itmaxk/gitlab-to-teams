import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)
DOTENV_PATH = Path(__file__).resolve().parent / ".env"

try:
    from dotenv import load_dotenv as _load_dotenv
except ImportError:
    _load_dotenv = None


def reload_dotenv():
    """Перечитывает .env файл и обновляет модульные константы."""
    if _load_dotenv is not None:
        _load_dotenv(dotenv_path=DOTENV_PATH, override=True)
        logger.info("Reloaded .env file from %s", DOTENV_PATH)

    # Обновляем модульные константы, которые читаются при импорте
    import services.review_service as _review
    _review.MAX_DIFF_CHARS = int(os.getenv("REVIEW_MAX_DIFF_CHARS", "60000"))
