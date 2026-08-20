"""Tool registration for the calculate skill."""

from app.tools.calculate_tool import get_calculate_tools


def get_tools() -> list:
    return list(get_calculate_tools())


__all__ = ["get_tools"]
