"""Google ADK skill for orchestrating urban navigation tools."""

from app.tools.navigation_tools import get_navigation_tools
import json
from pathlib import Path

from app.core.enums import AccessMode, OptimizationMode
from app.core.schemas.navigation import NAVIGATION_STATE_KEY
from app.skills.skill_template import load_rendered_skill_from_dir
from app.tools.navigation_tools import NAVIGATION_TOOLS

_SKILL_DIR = Path(__file__).resolve().parent
_SEMANTIC_THRESHOLD = 0.80
_DEFAULT_OPTIMIZATION = OptimizationMode.FASTEST_TIME
_TOOL_NAMES = {role: tool.__name__ for role, tool in NAVIGATION_TOOLS.items()}
_GET_STATE_TOOL = _TOOL_NAMES["get_state"]
_UPDATE_STATE_TOOL = _TOOL_NAMES["update_state"]
_SEARCH_LOCATION_TOOL = _TOOL_NAMES["search_location"]
_FIND_ROUTE_TOOL = _TOOL_NAMES["find_route"]
_FIND_RECOVERY_ROUTE_TOOL = _TOOL_NAMES["find_recovery_route"]


def _build_navigation_substitutions() -> dict[str, str]:
    """Build dynamic values injected into the navigation SKILL.md prompt."""

    state_contract = {
        "startPositionInput": None,
        "endPositionInput": None,
        "startPosition": None,
        "endPosition": None,
        "access": None,
        "optimization": _DEFAULT_OPTIMIZATION.value,
        "pendingSelection": None,
        "route": None,
        "currentStepIndex": 0,
        "recoveryRoute": None,
        "recoveryStepIndex": 0,
        "resumeStepIndex": None,
        "awaitingConfirmation": False,
        "scenario": "initial_route",
        "status": "collecting_input",
    }
    return {
        "state_key": NAVIGATION_STATE_KEY,
        "semantic_threshold": f"{_SEMANTIC_THRESHOLD:.2f}",
        "access_values": ", ".join(mode.value for mode in AccessMode),
        "optimization_values": ", ".join(mode.value for mode in OptimizationMode),
        "default_optimization": _DEFAULT_OPTIMIZATION.value,
        "state_contract": json.dumps(
            state_contract,
            ensure_ascii=False,
            indent=2,
        ),
        "get_state_tool": _GET_STATE_TOOL,
        "update_state_tool": _UPDATE_STATE_TOOL,
        "search_location_tool": _SEARCH_LOCATION_TOOL,
        "find_route_tool": _FIND_ROUTE_TOOL,
        "find_recovery_route_tool": _FIND_RECOVERY_ROUTE_TOOL,
    }

def get_tools() -> list:
    """Return all Python tools available to the navigation skill."""
    return list(get_navigation_tools())

navigation_skill = load_rendered_skill_from_dir(
    _SKILL_DIR,
    _build_navigation_substitutions(),
)

__all__ = ["navigation_skill"]
