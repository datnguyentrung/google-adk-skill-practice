from pathlib import Path

from app.skills.skill_template import load_rendered_skill_from_dir
from app.tools.calculate_tool import get_calculate_tools

_SKILL_DIR = Path(__file__).resolve().parent

calculate_skill = load_rendered_skill_from_dir(
    _SKILL_DIR,
    {},
)


def get_tools() -> list:
    """Return all Python tools available to the calculate skill."""

    return list(get_calculate_tools())


__all__ = ["calculate_skill"]
