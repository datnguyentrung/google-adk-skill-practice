"""Cooking skill package."""

__all__ = ["cooking_skill"]


def __getattr__(name: str):
    if name == "cooking_skill":
        from app.skills.cooking.cooking import cooking_skill

        return cooking_skill
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
