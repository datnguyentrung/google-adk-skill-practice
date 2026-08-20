"""Product Sales Knowledge Graph ingestion skill."""

from pathlib import Path

from app.skills.skill_template import (
    load_rendered_skill_from_dir,
)
from app.tools.ingestion_tools import (
    INGESTION_TOOLS,
    get_ingestion_tools,
)

_SKILL_DIR = Path(__file__).resolve().parent

_TOOL_NAMES = {role: tool.__name__ for role, tool in INGESTION_TOOLS.items()}

_PREPARE_EXTRACTION_CONTEXT_TOOL = _TOOL_NAMES["prepare_extraction_context"]

_VALIDATE_GRAPH_PATCH_TOOL = _TOOL_NAMES["validate_graph_patch"]

_FILL_GRAPH_PATCH_TOOL = _TOOL_NAMES["fill_graph_patch"]


def _build_ingestion_substitutions() -> dict[str, str]:
    """Build dynamic values injected into ingestion SKILL.md."""

    return {
        "prepare_extraction_context_tool": (_PREPARE_EXTRACTION_CONTEXT_TOOL),
        "validate_graph_patch_tool": (_VALIDATE_GRAPH_PATCH_TOOL),
        "fill_graph_patch_tool": (_FILL_GRAPH_PATCH_TOOL),
    }


ingestion_skill = load_rendered_skill_from_dir(
    _SKILL_DIR,
    _build_ingestion_substitutions(),
)


def get_tools() -> list:
    """Return all Python tools available to the ingestion skill."""
    return list(get_ingestion_tools())


__all__ = ["ingestion_skill"]
