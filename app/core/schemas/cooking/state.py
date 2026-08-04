"""Serializable state for one cooking session."""

from typing import Annotated, Any, Literal

from pydantic import Field

from app.core.schemas.base import SchemaBaseModel
from app.core.schemas.cooking.dish import Dish
from app.core.schemas.cooking.recipe import IngredientGroup, Recipe

COOKING_STATE_KEY = "cooking"
PositiveStepNumber = Annotated[int, Field(gt=0)]


class SearchCriteria(SchemaBaseModel):
    """Dish filters collected from the user."""

    available_ingredients: list[str] = Field(
        default_factory=list, alias="availableIngredients"
    )
    category: str | None = None
    cuisine: str | None = None
    tags: list[str] = Field(default_factory=list)
    dietary_flags: list[str] = Field(default_factory=list, alias="dietaryFlags")
    difficulty: str | None = None
    maximum_total_time: int | None = Field(default=None, gt=0, alias="maximumTotalTime")


class IngredientAdjustment(SchemaBaseModel):
    """A user-approved ingredient replacement."""

    ingredient_id: str = Field(alias="ingredientId")
    original_name: str = Field(alias="originalName")
    replacement_name: str = Field(alias="replacementName")


class ActiveIssue(SchemaBaseModel):
    """Cooking problem being resolved without advancing progress."""

    symptom: str
    step_index: int = Field(ge=0, alias="stepIndex")


class CookingState(SchemaBaseModel):
    """Top-level state required to continue an active cooking flow."""

    dish_name_input: str | None = Field(default=None, alias="dishNameInput")
    search_criteria: SearchCriteria = Field(
        default_factory=SearchCriteria, alias="searchCriteria"
    )
    desired_servings: int | None = Field(default=None, gt=0, alias="desiredServings")
    selected_dish: Dish | None = Field(default=None, alias="selectedDish")
    pending_selection: dict[str, Any] | None = Field(
        default=None, alias="pendingSelection"
    )
    recipe: Recipe | None = None
    scale_factor: float = Field(default=1.0, gt=0, alias="scaleFactor")
    scaled_ingredients: list[IngredientGroup] | None = Field(
        default=None, alias="scaledIngredients"
    )
    ingredient_adjustments: list[IngredientAdjustment] = Field(
        default_factory=list, alias="ingredientAdjustments"
    )
    current_step_index: int = Field(default=0, ge=0, alias="currentStepIndex")
    completed_step_numbers: list[PositiveStepNumber] = Field(
        default_factory=list, alias="completedStepNumbers"
    )
    active_issue: ActiveIssue | None = Field(default=None, alias="activeIssue")
    resume_step_index: int | None = Field(default=None, ge=0, alias="resumeStepIndex")
    awaiting_confirmation: bool = Field(default=False, alias="awaitingConfirmation")
    scenario: Literal[
        "dish_discovery",
        "dish_not_found",
        "recipe_preparation",
        "initial_cooking",
        "cooking_troubleshooting",
        "recipe_changed",
        "cooking_completed",
        "recipe_error",
    ] = "dish_discovery"
    status: Literal[
        "collecting_input",
        "searching_dishes",
        "awaiting_dish_selection",
        "loading_recipe",
        "reviewing_recipe",
        "awaiting_substitution_selection",
        "awaiting_cooking_confirmation",
        "cooking",
        "resolving_issue",
        "completed",
        "error",
    ] = "collecting_input"


__all__ = [
    "COOKING_STATE_KEY",
    "ActiveIssue",
    "CookingState",
    "IngredientAdjustment",
    "PositiveStepNumber",
    "SearchCriteria",
]
