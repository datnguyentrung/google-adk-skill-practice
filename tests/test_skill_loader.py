import asyncio

from app.agent import SKILLS_DIR, root_agent, root_skill_toolset
from app.core.schemas.cooking import COOKING_STATE_KEY
from app.core.schemas.navigation import NAVIGATION_STATE_KEY
from app.skills.skill_loader import (
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

    assert set(by_code) == {
        "calculate",
        "cooking",
        "hello-world",
        "ingestion",
        "navigation",
    }
    assert by_code["ingestion"].name == "ingestion"
    assert "Knowledge Graph" in by_code["ingestion"].description


def test_explicit_eager_loader_still_loads_rendered_skills_and_tools():
    loaded_skills = discover_skills(SKILLS_DIR)
    by_code = {loaded.code: loaded for loaded in loaded_skills}

    assert set(by_code) == {
        "calculate",
        "cooking",
        "hello-world",
        "ingestion",
        "navigation",
    }
    assert _tool_names(by_code["ingestion"].tools) == {
        "prepare_extraction_context",
        "validate_graph_patch",
        "fill_graph_patch",
    }
    assert "$" not in by_code["cooking"].skill.instructions
    assert "$" not in by_code["navigation"].skill.instructions
    assert "$" not in by_code["ingestion"].skill.instructions
    assert COOKING_STATE_KEY in by_code["cooking"].skill.instructions
    assert NAVIGATION_STATE_KEY in by_code["navigation"].skill.instructions


def test_root_agent_uses_lazy_registry_instead_of_preloaded_skills():
    assert root_agent.name == "root_agent"
    assert root_skill_toolset is root_agent.tools[0]
    assert root_skill_toolset.skills == []
    assert root_skill_toolset._registry is not None
    assert "Skill name: `ingestion`" in root_agent.instruction


def test_lazy_registry_loads_full_skill_only_on_demand():
    async def run():
        registry = root_skill_toolset._registry
        assert registry is not None

        first = await registry.get_skill(name="ingestion")
        second = await registry.get_skill(name="ingestion")

        assert first is second
        assert first.name == "ingestion"
        assert "$" not in first.instructions
        assert "prepare_extraction_context" in first.instructions
        assert "validate_graph_patch" in first.instructions
        assert "fill_graph_patch" in first.instructions

    asyncio.run(run())


def test_adk_app_enables_uploaded_file_artifacts():
    from app.agent import app

    assert app.root_agent is root_agent
    assert any(
        type(plugin).__name__ == "SaveFilesAsArtifactsPlugin"
        for plugin in app.plugins
    )
