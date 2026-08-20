import asyncio
import inspect
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

from google.adk.agents.invocation_context import (
    InvocationContext,
    new_invocation_context_id,
)
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.agents.run_config import RunConfig
from google.adk.sessions import InMemorySessionService
from google.adk.skills import load_skill_from_dir
from google.adk.tools import FunctionTool

from app.agent import root_agent, root_skill_toolset
from app.core.schemas.cooking import COOKING_STATE_KEY
from app.services.cooking import CookingService
from app.skills.cooking import cooking_skill
from app.tools.cooking_tools import (
    COOKING_TOOLS,
    get_cooking_state,
    get_recipe_detail,
    scale_recipe,
    search_dishes,
    update_cooking_state,
)

COOKING_TOOL_NAMES = {tool.__name__ for tool in COOKING_TOOLS.values()}
COOKING_SKILL_DIR = Path(__file__).parents[1] / "app" / "skills" / "cooking"
SOTO_AYAM_ID = "fcc8cd75-4b29-443b-afe5-271825cf95c7"


def _tool_context() -> SimpleNamespace:
    return SimpleNamespace(state={})


def _search(**overrides):
    values = {
        "query": None,
        "ingredients": [],
        "category": None,
        "cuisine": None,
        "dietary": [],
        "difficulty": None,
        "tags": [],
        "maximum_total_time": None,
        "top_k": 5,
    }
    values.update(overrides)
    return search_dishes(**values)


def test_cooking_service_loads_validated_data_once():
    service = CookingService()

    assert service.dishes is service.dishes
    assert service.recipes is service.recipes
    assert len(service.dishes) == 11
    assert service.recipes[SOTO_AYAM_ID].data.name == "Soto Ayam"


def test_state_defaults_and_patch_updates_are_serializable():
    context = _tool_context()

    initial = get_cooking_state(context)
    assert initial == {
        "success": True,
        "state": {
            "dishNameInput": None,
            "searchCriteria": {
                "availableIngredients": [],
                "category": None,
                "cuisine": None,
                "tags": [],
                "dietaryFlags": [],
                "difficulty": None,
                "maximumTotalTime": None,
            },
            "desiredServings": None,
            "selectedDish": None,
            "pendingSelection": None,
            "recipe": None,
            "scaleFactor": 1.0,
            "scaledIngredients": None,
            "ingredientAdjustments": [],
            "currentStepIndex": 0,
            "completedStepNumbers": [],
            "activeIssue": None,
            "resumeStepIndex": None,
            "awaitingConfirmation": False,
            "scenario": "dish_discovery",
            "status": "collecting_input",
        },
    }

    updated = update_cooking_state(
        {
            "dishNameInput": "Soto Ayam",
            "searchCriteria": {
                "availableIngredients": ["chicken"],
                "cuisine": "Indonesian",
                "maximumTotalTime": 90,
            },
            "desiredServings": 8,
            "status": "searching_dishes",
        },
        context,
    )
    assert updated["success"] is True
    assert updated["state"]["desiredServings"] == 8
    assert updated["state"]["searchCriteria"]["availableIngredients"] == ["chicken"]
    assert context.state[COOKING_STATE_KEY] == updated["state"]

    snake_case = update_cooking_state({"current_step_index": 2}, context)
    assert snake_case["state"]["currentStepIndex"] == 2

    merged = update_cooking_state(
        {"searchCriteria": {"difficulty": "Intermediate"}},
        context,
    )["state"]["searchCriteria"]
    assert merged["availableIngredients"] == ["chicken"]
    assert merged["cuisine"] == "Indonesian"
    assert merged["maximumTotalTime"] == 90
    assert merged["difficulty"] == "Intermediate"


def test_state_rejects_unknown_invalid_and_corrupt_values():
    context = _tool_context()
    assert update_cooking_state({"currentStepIndex": 1}, context)["success"]

    unknown = update_cooking_state({"steps": []}, context)
    assert unknown["errorCode"] == "INVALID_STATE"
    assert context.state[COOKING_STATE_KEY]["currentStepIndex"] == 1

    for changes in (
        {"desiredServings": 0},
        {"currentStepIndex": -1},
        {"status": "unknown"},
        {"recipe": {"made_up": True}},
    ):
        assert update_cooking_state(changes, context)["errorCode"] == "INVALID_STATE"

    context.state[COOKING_STATE_KEY] = []
    assert get_cooking_state(context)["errorCode"] == "INVALID_STATE"


def test_search_ranks_name_and_ingredient_matches():
    by_name = _search(query="soto ayam")
    assert by_name["success"] is True
    assert [dish["id"] for dish in by_name["dishes"]] == [SOTO_AYAM_ID]

    by_ingredient = _search(ingredients=["chicken"], top_k=20)
    assert by_ingredient["success"] is True
    assert len(by_ingredient["dishes"]) == 11
    assert all(
        "chicken" in dish["name"].casefold()
        or "chicken" in dish["description"].casefold()
        for dish in by_ingredient["dishes"]
    )

    missing = _search(query="not present anywhere")
    assert missing == {"success": True, "dishes": []}


def test_search_applies_metadata_and_time_filters():
    result = _search(
        ingredients=["chicken"],
        category="soup",
        cuisine="indonesian",
        dietary=["gluten-free", "dairy-free"],
        difficulty="intermediate",
        tags=["asian"],
        maximum_total_time=90,
    )
    assert [dish["id"] for dish in result["dishes"]] == [SOTO_AYAM_ID]

    too_short = _search(category="soup", maximum_total_time=20)
    assert too_short == {"success": True, "dishes": []}

    impossible_diet = _search(category="soup", dietary=["vegan"])
    assert impossible_diet == {"success": True, "dishes": []}

    stable = _search(category="soup", top_k=20)["dishes"]
    assert [dish["name"].casefold() for dish in stable] == sorted(
        dish["name"].casefold() for dish in stable
    )


def test_search_validates_required_criteria_and_limits():
    assert _search()["errorCode"] == "INVALID_SEARCH"
    assert _search(ingredients=["   "])["errorCode"] == "INVALID_SEARCH"
    assert _search(dietary=[""], tags=["  "])["errorCode"] == "INVALID_SEARCH"
    assert _search(category="soup", top_k=0)["errorCode"] == "INVALID_TOP_K"
    assert _search(category="soup", top_k=21)["errorCode"] == "INVALID_TOP_K"
    assert (
        _search(category="soup", maximum_total_time=0)["errorCode"]
        == "INVALID_MAXIMUM_TIME"
    )


def test_recipe_detail_returns_data_or_explicit_not_found():
    result = get_recipe_detail(SOTO_AYAM_ID)
    assert result["success"] is True
    assert result["recipe"]["name"] == "Soto Ayam"
    assert result["usage"]["detail_limit"] > 0

    missing = get_recipe_detail("8c691e9b-c961-4d6d-9b27-37e076309a5d")
    assert missing["errorCode"] == "RECIPE_NOT_FOUND"
    assert get_recipe_detail("  ")["errorCode"] == "INVALID_DISH_ID"


def test_scale_recipe_preserves_original_and_group_shape():
    recipe = get_recipe_detail(SOTO_AYAM_ID)["recipe"]
    original = deepcopy(recipe)

    result = scale_recipe(recipe, 6)
    assert result["success"] is True
    assert result["scaleFactor"] == 1.5
    assert recipe == original
    assert [group["group_name"] for group in result["scaledIngredients"]] == [
        group["group_name"] for group in recipe["ingredients"]
    ]
    assert result["scaledIngredients"][0]["items"][0]["quantity"] == 150.0
    assert result["scaledIngredients"][0]["items"][0]["name"] == "shallots"

    assert scale_recipe(recipe, 0)["errorCode"] == "INVALID_SERVINGS"
    assert scale_recipe({"invalid": True}, 4)["errorCode"] == "INVALID_RECIPE"


def test_tool_sequence_supports_selection_cooking_issue_and_completion():
    context = _tool_context()
    collected = update_cooking_state(
        {
            "dishNameInput": "Soto Ayam",
            "desiredServings": 6,
            "status": "searching_dishes",
        },
        context,
    )["state"]
    assert collected["recipe"] is None

    candidates = _search(query=collected["dishNameInput"])["dishes"]
    awaiting_choice = update_cooking_state(
        {
            "pendingSelection": {"type": "dish", "candidates": candidates},
            "status": "awaiting_dish_selection",
        },
        context,
    )["state"]
    selected = awaiting_choice["pendingSelection"]["candidates"][0]
    loading = update_cooking_state(
        {
            "selectedDish": selected,
            "pendingSelection": None,
            "status": "loading_recipe",
        },
        context,
    )["state"]

    detail = get_recipe_detail(loading["selectedDish"]["id"])
    reviewing = update_cooking_state(
        {
            "recipe": detail["recipe"],
            "scenario": "recipe_preparation",
            "status": "reviewing_recipe",
        },
        context,
    )["state"]
    scaled = scale_recipe(reviewing["recipe"], reviewing["desiredServings"])
    confirmed = update_cooking_state(
        {
            "scaleFactor": scaled["scaleFactor"],
            "scaledIngredients": scaled["scaledIngredients"],
            "awaitingConfirmation": True,
            "status": "awaiting_cooking_confirmation",
        },
        context,
    )["state"]
    assert confirmed["recipe"]["meta"]["yield_count"] == 4
    assert confirmed["scaleFactor"] == 1.5

    cooking = update_cooking_state(
        {
            "awaitingConfirmation": False,
            "scenario": "initial_cooking",
            "status": "cooking",
            "currentStepIndex": 0,
        },
        context,
    )["state"]
    step_number = cooking["recipe"]["instructions"][0]["step_number"]
    progressed = update_cooking_state(
        {
            "completedStepNumbers": [step_number],
            "currentStepIndex": 1,
        },
        context,
    )["state"]

    issue = update_cooking_state(
        {
            "activeIssue": {"symptom": "The broth is too salty", "stepIndex": 1},
            "resumeStepIndex": 1,
            "scenario": "cooking_troubleshooting",
            "status": "resolving_issue",
        },
        context,
    )["state"]
    assert issue["currentStepIndex"] == progressed["currentStepIndex"]

    resumed = update_cooking_state(
        {
            "activeIssue": None,
            "resumeStepIndex": None,
            "scenario": "initial_cooking",
            "status": "cooking",
        },
        context,
    )["state"]
    completed = update_cooking_state(
        {
            "currentStepIndex": len(resumed["recipe"]["instructions"]),
            "scenario": "cooking_completed",
            "status": "completed",
        },
        context,
    )["state"]
    assert completed["status"] == "completed"
    assert completed["recipe"]["id"] == SOTO_AYAM_ID


def test_cooking_skill_declares_workflow_and_exact_dynamic_tools():
    raw_skill = load_skill_from_dir(COOKING_SKILL_DIR)
    assert raw_skill.name == "cooking"
    assert "$state_contract" in raw_skill.instructions
    assert raw_skill.instructions != cooking_skill.instructions

    assert set(cooking_skill.frontmatter.metadata["adk_additional_tools"]) == (
        COOKING_TOOL_NAMES
    )
    assert COOKING_STATE_KEY in cooking_skill.instructions
    assert "Default search size: `5`" in cooking_skill.instructions
    assert "get_cooking_state" in cooking_skill.instructions
    assert '"dishNameInput": null' in cooking_skill.instructions
    assert "$" not in cooking_skill.instructions

    required_rules = [
        "start of every cooking request",
        "Do not search again",
        "Never invent a dish",
        "Never create a substitute recipe",
        "explicitly wants the first step",
        "Advance `currentStepIndex` only",
        "information-only question never changes",
        "return only useful Vietnamese cooking guidance",
    ]
    for rule in required_rules:
        assert rule in cooking_skill.instructions


def test_cooking_tool_signatures_have_no_defaults():
    for tool in COOKING_TOOLS.values():
        for parameter in inspect.signature(tool).parameters.values():
            assert parameter.default is inspect.Parameter.empty
        assert inspect.signature(tool).return_annotation != inspect.Signature.empty
        FunctionTool(tool)._get_declaration()


def test_root_agent_activates_cooking_tools_only_after_skill_load():
    async def run():
        inactive_names = {
            tool.name for tool in await root_skill_toolset.get_tools(None)
        }
        assert inactive_names.isdisjoint(COOKING_TOOL_NAMES)

        session_service = InMemorySessionService()
        session = await session_service.create_session(
            app_name="test",
            user_id="user",
            session_id="cooking-session",
            state={f"_adk_activated_skill_{root_agent.name}": ["cooking"]},
        )
        invocation_context = InvocationContext(
            session_service=session_service,
            session=session,
            invocation_id=new_invocation_context_id(),
            agent=root_agent,
            run_config=RunConfig(),
        )
        active_names = {
            tool.name
            for tool in await root_skill_toolset.get_tools(
                ReadonlyContext(invocation_context)
            )
        }
        assert COOKING_TOOL_NAMES <= active_names

    asyncio.run(run())
    assert "cooking" in {skill.name for skill in root_skill_toolset.skills}
