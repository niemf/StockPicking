"""FastAPI application entry point."""

import asyncio
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from loguru import logger

from app.config import settings
from app.db.base import init_db
from app.services.monitor import monitor_loop
from app.web.routes import router

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "web" / "static"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Create tables, then start the optional background monitor."""
    try:
        init_db()
    except Exception as exc:  # noqa: BLE001 - the UI should still start read-only
        logger.error("database init failed: {}", exc)

    stop_event = asyncio.Event()
    task = asyncio.create_task(monitor_loop(stop_event)) if settings.monitor_enabled else None
    if task is None:
        logger.info("monitor disabled (set MONITOR_ENABLED=true to run the polling loop)")

    try:
        yield
    finally:
        if task is not None:
            stop_event.set()
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    debug=settings.debug,
    description="A 股主板价格监控与 AI 复盘系统",
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.include_router(router)
