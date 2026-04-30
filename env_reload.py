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
    _review.MAX_DIFF_CHARS = _review._read_int_env(
        "REVIEW_MAX_DIFF_CHARS", _review.DEFAULT_MAX_DIFF_CHARS
    )
    _review.REVIEW_BATCH_MAX_CHARS = _review._resolve_batch_max_chars()
    _review.REVIEW_LLM_READ_TIMEOUT = _review._read_float_env(
        "REVIEW_LLM_READ_TIMEOUT", _review.DEFAULT_REVIEW_LLM_READ_TIMEOUT
    )
