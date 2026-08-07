from app.agent import SKILLS_DIR, root_agent, root_skill_toolset
from app.core.schemas.cooking import COOKING_STATE_KEY
from app.core.schemas.navigation import NAVIGATION_STATE_KEY
from app.skills.skill_loader import discover_skills


def _tool_names(tools: tuple) -> set[str]:
    return {getattr(tool, "__name__", getattr(tool, "name", "")) for tool in tools}


def test_discover_skills_loads_all_skill_directories_and_tools():
    loaded_skills = discover_skills(SKILLS_DIR)
    by_code = {loaded.code: loaded for loaded in loaded_skills}

    assert set(by_code) == {"calculate", "cooking", "hello-world", "navigation"}
    assert {loaded.skill.name for loaded in loaded_skills} == set(by_code)

    assert _tool_names(by_code["calculate"].tools) == {
        "prepare_calculation",
        "calculate",
    }
    assert _tool_names(by_code["cooking"].tools) == {
        "get_cooking_state",
        "update_cooking_state",
        "search_dishes",
        "get_recipe_detail",
        "scale_recipe",
    }
    assert by_code["hello-world"].tools == ()
    assert _tool_names(by_code["navigation"].tools) == {
        "get_navigation_state",
        "update_navigation_state",
        "search_locations",
        "find_route",
        "find_recovery_route",
    }


def test_discover_skills_uses_rendered_skill_prompts():
    by_code = {loaded.code: loaded for loaded in discover_skills(SKILLS_DIR)}

    assert "$" not in by_code["cooking"].skill.instructions
    assert "$" not in by_code["navigation"].skill.instructions
    assert COOKING_STATE_KEY in by_code["cooking"].skill.instructions
    assert NAVIGATION_STATE_KEY in by_code["navigation"].skill.instructions


def test_root_agent_exposes_dynamic_skill_toolset():
    assert root_agent.name == "root_agent"
    assert root_skill_toolset is root_agent.tools[0]
    assert {skill.name for skill in root_skill_toolset.skills} == {
        "calculate",
        "cooking",
        "hello-world",
        "navigation",
    }
