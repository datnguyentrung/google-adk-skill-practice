"""Product Sales Knowledge Graph ingestion skill."""

from pathlib import Path

from app.skills.skill_template import (
    load_rendered_skill_from_dir,
)
from app.tools.ingestion_tools import (
    get_ingestion_tools,
)

_SKILL_DIR = Path(__file__).resolve().parent

def _build_ingestion_substitutions() -> dict[str, str]:
    """Build dynamic values injected into ingestion SKILL.md."""

    return {}


def build_skill():
    """Build from current files so digest invalidation refreshes instructions."""

    return load_rendered_skill_from_dir(
        _SKILL_DIR,
        _build_ingestion_substitutions(),
    )


ingestion_skill = build_skill()


def get_tools() -> list:
    """Return all Python tools available to the ingestion skill."""
    return list(get_ingestion_tools())


__all__ = ["build_skill", "ingestion_skill"]
