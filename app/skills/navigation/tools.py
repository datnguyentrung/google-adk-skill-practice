"""Tool registration for the navigation skill."""

from app.tools.navigation_tools import get_navigation_tools


def get_tools() -> list:
    return list(get_navigation_tools())


__all__ = ["get_tools"]
