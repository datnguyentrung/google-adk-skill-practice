from app.agent import SKILLS_DIR
from app.skills.skill_loader import (
    discover_skill_descriptors,
    discover_skills,
)


def _tool_names(tools: tuple) -> set[str]:
    return {getattr(tool, "__name__", getattr(tool, "name", "")) for tool in tools}


def test_discover_skill_descriptors_reads_frontmatter_catalog():
    descriptors = discover_skill_descriptors(SKILLS_DIR)
    by_code = {descriptor.code: descriptor for descriptor in descriptors}

    assert {
        "calculate",
        "cooking",
        "hello-world",
        "graph-qa",
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
        "graph-qa",
        "ingestion",
        "navigation",
    }.issubset(set(by_code))
    assert _tool_names(by_code["ingestion"].tools) == {
        "begin_ingestion",
        "get_ingestion_batch",
        "submit_ingestion_batch",
        "finalize_ingestion",
        "fill_ingestion",
        "get_ingestion_status",
        "delete_document",
        "validate_graph_patch",
        "fill_graph_patch",
    }
    assert _tool_names(by_code["graph-qa"].tools) == {
        "execute_read_cypher",
        "vector_search",
        "hybrid_search",
    }
    assert "$" not in by_code["cooking"].skill.instructions
    assert "$" not in by_code["navigation"].skill.instructions
    assert "$" not in by_code["ingestion"].skill.instructions
