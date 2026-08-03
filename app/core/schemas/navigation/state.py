"""The serializable state persisted for one navigation session."""

from typing import Any, Literal

from pydantic import Field

from app.core.enums import AccessMode, NavigationScenario, OptimizationMode
from app.core.schemas.navigation.base import SchemaBaseModel

NAVIGATION_STATE_KEY = "navigation"


class NavigationState(SchemaBaseModel):
    """Top-level state required to continue an active navigation flow."""

    start_position_input: str | None = Field(default=None, alias="startPositionInput")
    end_position_input: str | None = Field(default=None, alias="endPositionInput")
    start_position: dict[str, Any] | None = Field(default=None, alias="startPosition")
    end_position: dict[str, Any] | None = Field(default=None, alias="endPosition")
    access: AccessMode | None = None
    optimization: OptimizationMode = OptimizationMode.FASTEST_TIME
    pending_selection: dict[str, Any] | None = Field(
        default=None, alias="pendingSelection"
    )
    route: dict[str, Any] | None = None
    current_step_index: int = Field(default=0, ge=0, alias="currentStepIndex")
    recovery_route: dict[str, Any] | None = Field(
        default=None, alias="recoveryRoute"
    )
    recovery_step_index: int = Field(default=0, ge=0, alias="recoveryStepIndex")
    resume_step_index: int | None = Field(default=None, ge=0, alias="resumeStepIndex")
    awaiting_confirmation: bool = Field(
        default=False, alias="awaitingConfirmation"
    )
    scenario: NavigationScenario = NavigationScenario.INITIAL_ROUTE
    status: Literal[
        "collecting_input",
        "awaiting_location_selection",
        "awaiting_route_confirmation",
        "navigating",
        "collecting_current_position",
        "recovering",
        "arrived",
        "error",
    ] = "collecting_input"


__all__ = ["NAVIGATION_STATE_KEY", "NavigationState"]
