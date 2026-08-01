"""
Shopping Research Agent API
Main application entry point with FastAPI configuration.
"""

import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager

logging.raiseExceptions = False

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

import agentops
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from agentops.sdk.decorators import trace, agent, operation, tool

from app.config import settings

# Configure logging
logging.basicConfig(
    level=settings.LOG_LEVEL,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)


def _ensure_uvicorn_access_logging() -> None:
    access_logger = logging.getLogger("uvicorn.access")
    access_logger.disabled = False

    if access_logger.level == logging.NOTSET or access_logger.level > logging.INFO:
        access_logger.setLevel(logging.INFO)

    has_stream_handler = any(
        isinstance(handler, logging.StreamHandler) for handler in access_logger.handlers
    )
    if not has_stream_handler:
        try:
            from uvicorn.logging import AccessFormatter

            formatter = AccessFormatter(
                '%(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s'
            )
        except Exception:
            formatter = logging.Formatter("%(levelname)s: %(message)s")

        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(formatter)
        access_logger.addHandler(handler)

    access_logger.propagate = False


_ensure_uvicorn_access_logging()
logger = logging.getLogger(__name__)

if settings.AGENTOPS_API_KEY:
    agentops.init(
        settings.AGENTOPS_API_KEY,
        auto_start_session=False,
        default_tags=[
            "shopping-research-agent",
            "fastapi",
            "google-adk",
            "shopping",
        ],
    )
else:
    logger.warning("AgentOps API key is not configured; tracing is disabled.")

scheduler = BackgroundScheduler()

if settings.GOOGLE_APPLICATION_CREDENTIALS:
    if os.path.exists(settings.GOOGLE_APPLICATION_CREDENTIALS):
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = (
            settings.GOOGLE_APPLICATION_CREDENTIALS
        )
        print(f"✅ Đã cấu hình xác thực từ: {settings.GOOGLE_APPLICATION_CREDENTIALS}")
    else:
        logger.warning(
            f"⚠️ Cảnh báo: File credential không tồn tại tại: {settings.GOOGLE_APPLICATION_CREDENTIALS}"
        )

logger.debug(
    f"Đang sử dụng Credential từ: {os.environ.get('GOOGLE_APPLICATION_CREDENTIALS')}"
)

# Initialize FastAPI application
app = FastAPI(
    title="Shopping Research Agent",
    description="AI-powered shopping research on Vietnamese e-commerce platforms",
    version="1.3.0",
    debug=settings.DEBUG,
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://localhost:5174",
    ],  # Trong production nên giới hạn lại domain frontend
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Add GZIP compression middleware (giúp nén payload trả về, rất tốt cho tốc độ FE)
app.add_middleware(GZipMiddleware, minimum_size=1000)


@app.get("/")
async def root():
    return {"message": "Shopping Research Agent API is running"}


@app.get("/hello/{name}")
async def say_hello(name: str):
    return {"message": f"Hello {name}"}


logger.info("✅ Application initialized")
