import logging
import sys

import agentops

from app.config.settings import settings


def configure_utf8_logging() -> None:
    """Keep AgentOps emoji logs from crashing Windows cp1252 streams."""
    logging.raiseExceptions = False

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

    for logger_name in ("agentops", "uvicorn", "uvicorn.error"):
        for handler in logging.getLogger(logger_name).handlers:
            stream = getattr(handler, "stream", None)
            if hasattr(stream, "reconfigure"):
                try:
                    stream.reconfigure(encoding="utf-8", errors="replace")
                except Exception:
                    pass


def init_agentops() -> None:
    """Initialize AgentOps from the app entrypoint only."""
    configure_utf8_logging()
    if settings.AGENTOPS_API_KEY:
        agentops.init(settings.AGENTOPS_API_KEY, auto_start_session=False)
