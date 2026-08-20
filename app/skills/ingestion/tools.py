"""Tool registration for the ingestion skill."""

from app.tools.ingestion_tools import get_ingestion_tools


def get_tools() -> list:
    return list(get_ingestion_tools())


__all__ = ["get_tools"]
