"""Cooking skill instructions for the root agent."""

from app.tools.cooking_tools import get_cooking_tools
import json
from pathlib import Path

from app.core.schemas.cooking import COOKING_STATE_KEY, CookingState
from app.skills.skill_template import load_rendered_skill_from_dir
from app.tools.cooking_tools import COOKING_TOOLS

_SKILL_DIR = Path(__file__).resolve().parent
_DEFAULT_TOP_K = 5
_TOOL_NAMES = {role: tool.__name__ for role, tool in COOKING_TOOLS.items()}
_GET_STATE_TOOL = _TOOL_NAMES["get_state"]
_UPDATE_STATE_TOOL = _TOOL_NAMES["update_state"]
_SEARCH_DISHES_TOOL = _TOOL_NAMES["search_dishes"]
_GET_RECIPE_TOOL = _TOOL_NAMES["get_recipe"]
_SCALE_RECIPE_TOOL = _TOOL_NAMES["scale_recipe"]


def _build_cooking_substitutions() -> dict[str, str]:
    """Build dynamic values injected into the cooking SKILL.md prompt."""

    state_contract = CookingState().model_dump(mode="json", by_alias=True)
    return {
        "state_key": COOKING_STATE_KEY,
        "default_top_k": str(_DEFAULT_TOP_K),
        "state_contract": json.dumps(
            state_contract,
            ensure_ascii=False,
            indent=2,
        ),
        "get_state_tool": _GET_STATE_TOOL,
        "update_state_tool": _UPDATE_STATE_TOOL,
        "search_dishes_tool": _SEARCH_DISHES_TOOL,
        "get_recipe_tool": _GET_RECIPE_TOOL,
        "scale_recipe_tool": _SCALE_RECIPE_TOOL,
    }


cooking_skill = load_rendered_skill_from_dir(
    _SKILL_DIR,
    _build_cooking_substitutions(),
)

def get_tools() -> list:
    """Return all Python tools available to the cooking skill."""
    return list(get_cooking_tools())

__all__ = ["cooking_skill"]
