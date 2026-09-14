import asyncio

from google.adk.skills import models

from app.agent import SKILLS_DIR, root_agent, root_skill_toolset
from app.core.schemas.cooking import COOKING_STATE_KEY
from app.core.schemas.navigation import NAVIGATION_STATE_KEY
from app.skills.local_skill_registry import LocalSkillRegistry
from app.skills.skill_loader import (
    SkillDescriptor,
    discover_skill_descriptors,
    discover_skills,
)


def _tool_names(tools: tuple) -> set[str]:
    return {
        getattr(tool, "__name__", getattr(tool, "name", ""))
        for tool in tools
    }


def test_discover_skill_descriptors_reads_frontmatter_catalog():
    descriptors = discover_skill_descriptors(SKILLS_DIR)
    by_code = {descriptor.code: descriptor for descriptor in descriptors}

    assert {
        "calculate",
        "cooking",
        "hello-world",
        "ingestion",
        "navigation",
    }.issubset(set(by_code))
    assert by_code["ingestion"].name == "ingestion"
    assert "Knowledge Graph" in by_code["ingestion"].description


def test_explicit_eager_loader_still_loads_rendered_skills_and_tools():
    loaded_skills = discover_skills(SKILLS_DIR)
    by_code = {loaded.code: loaded for loaded in loaded_skills}

    assert {
        "calculate",
        "cooking",
        "hello-world",
        "ingestion",
        "navigation",
    }.issubset(set(by_code))
    assert _tool_names(by_code["ingestion"].tools) == {
        "ingest_document_end_to_end",
        "update_document",
        "delete_document",
        "apply_changes",
        "get_ingestion_status",
        "validate_graph_patch",
        "fill_graph_patch",
    }
    assert "$" not in by_code["cooking"].skill.instructions
    assert "$" not in by_code["navigation"].skill.instructions
    assert "$" not in by_code["ingestion"].skill.instructions
