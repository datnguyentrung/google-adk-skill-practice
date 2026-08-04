"""Small ADK function tools for the cooking workflow."""

import json
from typing import Any

from google.adk.tools import ToolContext

from app.core.schemas.cooking import (
    COOKING_STATE_KEY,
    CookingState,
    Recipe,
    SearchCriteria,
)
from app.services.cooking import CookingService

_cooking_service = CookingService()


def get_cooking_tools() -> list:
    return list(COOKING_TOOLS.values())


def get_cooking_state(tool_context: ToolContext) -> dict[str, Any]:
    """Return the current serializable cooking state for this session."""

    try:
        state = _read_cooking_state(tool_context)
        return {"success": True, "state": _serialize_state(state)}
    except (TypeError, ValueError) as error:
        return _error("INVALID_STATE", str(error))


def update_cooking_state(
    changes: dict[str, Any],
    tool_context: ToolContext,
) -> dict[str, Any]:
    """Merge validated cooking fields into the current session state."""

    try:
        current = _read_cooking_state(tool_context)
        payload = current.model_dump(mode="python")
        canonical_changes = _canonical_state_changes(changes)
        if "search_criteria" in canonical_changes:
            criteria_changes = _canonical_search_criteria_changes(
                canonical_changes["search_criteria"]
            )
            current_criteria = current.search_criteria.model_dump(mode="python")
            current_criteria.update(criteria_changes)
            canonical_changes["search_criteria"] = current_criteria
        payload.update(canonical_changes)
        updated = CookingState.model_validate(payload)
        serialized = _serialize_state(updated)
        tool_context.state[COOKING_STATE_KEY] = serialized
        return {"success": True, "state": serialized}
    except (TypeError, ValueError) as error:
        return _error("INVALID_STATE", str(error))


def search_dishes(
    query: str | None,
    ingredients: list[str],
    category: str | None,
    cuisine: str | None,
    dietary: list[str],
    difficulty: str | None,
    tags: list[str],
    maximum_total_time: int | None,
    top_k: int,
) -> dict[str, Any]:
    """Find ranked dish summaries using text and exact metadata filters."""

    if not isinstance(top_k, int) or isinstance(top_k, bool) or not 1 <= top_k <= 20:
        return _error("INVALID_TOP_K", "top_k must be an integer between 1 and 20")
    if maximum_total_time is not None and (
        not isinstance(maximum_total_time, int)
        or isinstance(maximum_total_time, bool)
        or maximum_total_time <= 0
    ):
        return _error(
            "INVALID_MAXIMUM_TIME",
            "maximum_total_time must be a positive integer number of minutes",
        )

    try:
        ingredients = _clean_string_list(ingredients, "ingredients")
        dietary = _clean_string_list(dietary, "dietary")
        tags = _clean_string_list(tags, "tags")
    except TypeError as error:
        return _error("INVALID_SEARCH", str(error))

    text_values = [query, category, cuisine, difficulty]
    has_scalar_criterion = any(
        isinstance(value, str) and value.strip() for value in text_values
    )
    has_list_criterion = any(values for values in (ingredients, dietary, tags))
    if (
        not has_scalar_criterion
        and not has_list_criterion
        and maximum_total_time is None
    ):
        return _error("INVALID_SEARCH", "at least one dish criterion is required")

    try:
        dishes = _cooking_service.search(
            query,
            ingredients,
            category,
            cuisine,
            dietary,
            difficulty,
            tags,
            maximum_total_time,
            top_k,
        )
        return {
            "success": True,
            "dishes": [dish.model_dump(mode="json") for dish in dishes],
        }
    except (OSError, TypeError, ValueError) as error:
        return _error("COOKING_TOOL_ERROR", str(error))


def get_recipe_detail(dish_id: str) -> dict[str, Any]:
    """Return the complete recipe and usage metadata for one selected dish."""

    normalized_dish_id = dish_id.strip()
    if not normalized_dish_id:
        return _error("INVALID_DISH_ID", "dish_id must not be empty")

    try:
        response = _cooking_service.get_recipe(normalized_dish_id)
    except (OSError, TypeError, ValueError) as error:
        return _error("COOKING_TOOL_ERROR", str(error))
    if response is None:
        return _error(
            "RECIPE_NOT_FOUND",
            f"Không có công thức chi tiết cho món '{normalized_dish_id}'.",
        )
    return {
        "success": True,
        "recipe": response.data.model_dump(mode="json"),
        "usage": response.usage.model_dump(mode="json"),
    }


def scale_recipe(
    recipe: dict[str, Any],
    desired_servings: int,
) -> dict[str, Any]:
    """Scale ingredient quantities for a positive number of servings."""

    if (
        not isinstance(desired_servings, int)
        or isinstance(desired_servings, bool)
        or desired_servings <= 0
    ):
        return _error("INVALID_SERVINGS", "desired_servings must be a positive integer")

    try:
        validated_recipe = Recipe.model_validate(recipe)
        if validated_recipe.meta.yield_count <= 0:
            return _error(
                "INVALID_RECIPE",
                "recipe.meta.yield_count must be greater than zero",
            )
        scale_factor, ingredients = _cooking_service.scale(
            validated_recipe,
            desired_servings,
        )
        return {
            "success": True,
            "scaleFactor": scale_factor,
            "scaledIngredients": ingredients,
        }
    except (TypeError, ValueError) as error:
        return _error("INVALID_RECIPE", str(error))


def _read_cooking_state(tool_context: ToolContext) -> CookingState:
    raw_state = tool_context.state.get(COOKING_STATE_KEY)
    return CookingState.model_validate(raw_state if raw_state is not None else {})


def _canonical_state_changes(changes: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(changes, dict):
        raise TypeError("changes must be a dictionary")

    aliases = {
        field.alias or field_name: field_name
        for field_name, field in CookingState.model_fields.items()
    }
    canonical: dict[str, Any] = {}
    for key, value in changes.items():
        field_name = aliases.get(key, key)
        if field_name not in CookingState.model_fields:
            raise ValueError(f"Unknown cooking state field: {key}")
        canonical[field_name] = value
    return canonical


def _canonical_search_criteria_changes(changes: object) -> dict[str, Any]:
    if not isinstance(changes, dict):
        raise TypeError("searchCriteria changes must be a dictionary")

    aliases = {
        field.alias or field_name: field_name
        for field_name, field in SearchCriteria.model_fields.items()
    }
    canonical: dict[str, Any] = {}
    for key, value in changes.items():
        field_name = aliases.get(key, key)
        if field_name not in SearchCriteria.model_fields:
            raise ValueError(f"Unknown cooking search criterion: {key}")
        canonical[field_name] = value
    return canonical


def _clean_string_list(values: object, field_name: str) -> list[str]:
    if not isinstance(values, list) or any(
        not isinstance(value, str) for value in values
    ):
        raise TypeError(f"{field_name} must be a list of strings")
    return [value.strip() for value in values if value.strip()]


def _serialize_state(state: CookingState) -> dict[str, Any]:
    serialized = state.model_dump(mode="json", by_alias=True)
    json.dumps(serialized, ensure_ascii=False)
    return serialized


def _error(code: str, message: str) -> dict[str, Any]:
    return {
        "success": False,
        "errorCode": code,
        "errorMessage": message,
    }


COOKING_TOOLS = {
    "get_state": get_cooking_state,
    "update_state": update_cooking_state,
    "search_dishes": search_dishes,
    "get_recipe": get_recipe_detail,
    "scale_recipe": scale_recipe,
}


__all__ = [
    "COOKING_TOOLS",
    "get_cooking_state",
    "get_recipe_detail",
    "scale_recipe",
    "search_dishes",
    "update_cooking_state",
]
