import os


def is_review_llm_configured() -> bool:
    return bool(os.getenv("REVIEW_API_URL", "").strip())
