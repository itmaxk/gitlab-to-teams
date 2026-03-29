import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from db import init_db, seed_default_rule, seed_report_settings
from services.poller import start_polling
from services.reports_scheduler import start_reports_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

BASE_DIR = Path(__file__).parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    seed_default_rule()
    seed_report_settings()
    poll_task = asyncio.create_task(start_polling())
    reports_task = asyncio.create_task(start_reports_scheduler())
    yield
    poll_task.cancel()
    reports_task.cancel()


app = FastAPI(title="GitLab Manager", lifespan=lifespan)

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

from routers import rules, pages, queue, compare, reports  # noqa: E402

app.include_router(rules.router)
app.include_router(pages.router)
app.include_router(queue.router)
app.include_router(compare.router)
app.include_router(reports.router)

if __name__ == "__main__":
    import uvicorn

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8055"))
    print(f"\n  Web-интерфейс: http://localhost:{port}/rules")
    print(f"  Дашборд:       http://localhost:{port}/")
    print(f"  Swagger API:   http://localhost:{port}/docs\n")
    uvicorn.run("main:app", host=host, port=port, reload=True)
