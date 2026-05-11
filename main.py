import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

BASE_DIR = Path(__file__).parent
DOTENV_PATH = BASE_DIR / ".env"

try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=DOTENV_PATH)
except ImportError:
    pass

from env_reload import reload_dotenv

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from db import (
    init_db,
    seed_default_rule,
    seed_global_settings,
    seed_report_settings,
    seed_review_settings,
)
from services.gitlab_client import close_client
from services.poller import start_polling
from services.reports_scheduler import start_reports_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    seed_default_rule()
    seed_report_settings()
    seed_review_settings()
    seed_global_settings()
    poll_task = asyncio.create_task(start_polling())
    reports_task = asyncio.create_task(start_reports_scheduler())
    try:
        yield
    finally:
        poll_task.cancel()
        reports_task.cancel()
        await asyncio.gather(poll_task, reports_task, return_exceptions=True)
        await close_client()


app = FastAPI(title="Project Manager", lifespan=lifespan)


@app.get("/.well-known/appspecific/com.chrome.devtools.json")
async def _chrome_devtools_probe():
    return {}

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

from routers import rules, pages, queue, compare, reports, review, schema, database, presets, sonar  # noqa: E402


@app.post("/api/reload-env")
async def api_reload_env():
    reload_dotenv()
    return {"status": "ok"}

app.include_router(rules.router)
app.include_router(pages.router)
app.include_router(queue.router)
app.include_router(compare.router)
app.include_router(reports.router)
app.include_router(review.router)
app.include_router(schema.router)
app.include_router(database.router)
app.include_router(presets.router)
app.include_router(sonar.router)

if __name__ == "__main__":
    import uvicorn

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8055"))
    print(f"\n  Web-интерфейс: http://localhost:{port}/rules")
    print(f"  Дашборд:       http://localhost:{port}/")
    print(f"  Swagger API:   http://localhost:{port}/docs\n")
    uvicorn.run("main:app", host=host, port=port, reload=True)
