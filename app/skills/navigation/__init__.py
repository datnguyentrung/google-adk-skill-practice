"""Navigation skill package."""

__all__ = ["navigation_skill"]


def __getattr__(name: str):
    if name == "navigation_skill":
        from app.skills.navigation.navigation import navigation_skill

        return navigation_skill
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
