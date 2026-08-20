"""Tool registration for the cooking skill."""

from app.tools.cooking_tools import get_cooking_tools


def get_tools() -> list:
    return list(get_cooking_tools())


__all__ = ["get_tools"]
