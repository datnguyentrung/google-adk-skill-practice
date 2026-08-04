"""Inline Google ADK skill for orchestrating urban navigation tools."""

from __future__ import annotations

import json
from textwrap import dedent

from google.adk.skills import models

from app.core.enums import AccessMode, OptimizationMode
from app.core.schemas.navigation import NAVIGATION_STATE_KEY
from app.tools.navigation_tools import NAVIGATION_TOOLS

_SEMANTIC_THRESHOLD = 0.80
_DEFAULT_OPTIMIZATION = OptimizationMode.FASTEST_TIME
_TOOL_NAMES = {role: tool.__name__ for role, tool in NAVIGATION_TOOLS.items()}
_GET_STATE_TOOL = _TOOL_NAMES["get_state"]
_UPDATE_STATE_TOOL = _TOOL_NAMES["update_state"]
_SEARCH_LOCATION_TOOL = _TOOL_NAMES["search_location"]
_FIND_ROUTE_TOOL = _TOOL_NAMES["find_route"]
_FIND_RECOVERY_ROUTE_TOOL = _TOOL_NAMES["find_recovery_route"]


def _build_navigation_instructions() -> str:
    """Build the navigation workflow instructions for the root agent."""

    access_values = ", ".join(mode.value for mode in AccessMode)
    optimization_values = ", ".join(mode.value for mode in OptimizationMode)
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

    return dedent(
        f"""
        # Urban Navigation

        You are the root agent executing a simulated urban navigation workflow.
        Extract user intent and fields yourself, combine them with session state,
        decide the next workflow step, and call only the tool needed for that step.
        Tools and services never decide what to ask the user next.

        ## Runtime contract

        State key: `{NAVIGATION_STATE_KEY}`
        Similarity threshold: strictly greater than `{_SEMANTIC_THRESHOLD:.2f}`
        Access modes: `{access_values}`
        Optimization modes: `{optimization_values}`
        Default optimization: `{_DEFAULT_OPTIMIZATION.value}`

        State shape:

        ```json
        {json.dumps(state_contract, ensure_ascii=False, indent=2)}
        ```

        Available tools after this skill is loaded:

        - `{_GET_STATE_TOOL}()`
        - `{_UPDATE_STATE_TOOL}(changes)`
        - `{_SEARCH_LOCATION_TOOL}(query, target_type, min_similarity)`
        - `{_FIND_ROUTE_TOOL}(start_node_id, end_node_id, access, optimization)`
        - `{_FIND_RECOVERY_ROUTE_TOOL}(current_node_id, route_node_ids,
          current_step_index, access, optimization)`

        Every tool response has `success`. On failure, use `errorCode` and
        `errorMessage`; never pretend that a failed operation succeeded.

        ## 1. Read and collect input

        Call `{_GET_STATE_TOOL}` at the start of every navigation request.
        Extract new values from the user message and preserve valid state values.
        Required route fields are unresolved start text, unresolved destination
        text, and one access mode. Ask only for missing fields. Store raw text in
        `startPositionInput` and `endPositionInput`. Do not search or calculate a
        route while its required input is missing.

        ## 2. Resolve locations

        Resolve destination before start. For each unresolved field, call
        `{_SEARCH_LOCATION_TOOL}` with target type `auto` and threshold
        `{_SEMANTIC_THRESHOLD:.2f}`. The tool already filters and sorts
        candidates. Never accept a candidate whose similarity is less than or
        equal to the threshold.

        Save candidates with `{_UPDATE_STATE_TOOL}` as:

        `pendingSelection = {{"field": "endPosition|startPosition", "candidates": [...]}}`

        Also set `status = "awaiting_location_selection"`. Show a numbered list
        with node ID, name, node/road match type, description, road name when
        available, and similarity percentage. Ask the user to choose and stop.
        When the user chooses, use the stored candidate without searching again.
        Save only its `nodeId`, `name`, `targetType`, `description`, and optional
        `roadName` as the resolved position, then clear `pendingSelection`.
        If no candidate exists, ask for more precise text and never guess.

        ## 3. Confirm before routing

        When both positions and access are resolved, show start name/node,
        destination name/node, access, and optimization. Ask for explicit route
        confirmation. This explicit route confirmation is required before any
        route calculation. Save `awaitingConfirmation = true` and
        `status = "awaiting_route_confirmation"`, then stop.
        An unrelated reply is not confirmation. If a location changes, clear only that resolved
        field and repeat its search. Never call `{_FIND_ROUTE_TOOL}` before
        explicit confirmation.

        ## 4. Main route

        After confirmation, call `{_FIND_ROUTE_TOOL}` with the two selected
        node IDs, access, and optimization. On success, save the returned `route`,
        set `currentStepIndex = 0`, clear recovery data, set
        `awaitingConfirmation = false`, `scenario = "initial_route"`, and
        `status = "navigating"`. If `route.steps` is empty, set `status = "arrived"`
        immediately. On failure, preserve the resolved request, set
        `scenario = "route_error"` and `status = "error"`, and explain the real
        tool error.

        ## 5. Step-by-step guidance

        For normal navigation use `route.steps[currentStepIndex]`; for recovery
        use `recoveryRoute.steps[recoveryStepIndex]`. A step contains real
        `fromNode`, `toNode`, `edge`, and `turn` data. Give only the active step:
        its number, from/to names, road, edge instruction and landmark, turn,
        distance, and time. Advance an index only when the user clearly confirms
        completion of that step, and persist every change through
        `{_UPDATE_STATE_TOOL}`. When the main route is exhausted, set
        `status = "arrived"`.

        ## 6. Wrong-turn recovery

        If the user knows the current location, resolve it with
        `{_SEARCH_LOCATION_TOOL}` when needed. If that search needs a user
        choice, save its candidates as `pendingSelection` with
        `field = "recoveryPosition"`, set
        `status = "awaiting_location_selection"`, ask the user to choose, and
        stop. On the next turn, use the selected candidate's `nodeId` only as
        the recovery start, then clear `pendingSelection`; never overwrite
        `startPosition` or `endPosition` with it. Build the original ordered
        node list as the first step's `fromNode.id` followed by every step's
        `toNode.id`; for a zero-step route use `route.startNodeId`. Call
        `{_FIND_RECOVERY_ROUTE_TOOL}` with that list and the current main
        step index. Preserve the main route and destination. Save the returned
        `recoveryRoute` and `resumeStepIndex`, reset `recoveryStepIndex = 0`, and
        set `scenario = "wrong_turn_reroute"`, `status = "recovering"`.

        Guide recovery one step at a time. If the recovery route has no steps, or
        after its last step is completed, clear all recovery fields, set
        `currentStepIndex = resumeStepIndex`, reset `recoveryStepIndex = 0`, and
        resume the main route with `status = "navigating"` (or `arrived` when the
        resume index is at the destination).

        ## 7. User is lost

        If the user cannot identify a graph position, preserve only
        `endPosition`, `endPositionInput`, `access`, and `optimization`. Clear
        start input/position, main route, recovery data, pending selection, and
        progress indexes. Set `awaitingConfirmation = false`,
        `scenario = "forgotten_route"`, and
        `status = "collecting_current_position"`. Ask what road, building,
        intersection, or landmark is visible, then treat the answer as the new
        `startPositionInput`. Do not ask for a stored destination again.

        ## 8. Current node and edge questions

        Read the active step from state and answer using only its real
        `fromNode`, `toNode`, and `edge` fields. Do not call a route tool. For an
        information-only question, do not advance any index.

        ## Integrity rules

        Never invent locations, similarity values, nodes, edges, routes, or
        progress. Save only serializable tool output and primitive values. Never
        overwrite the main destination during recovery, replace an active route
        without a requested change, or use stale state after a tool update.
        Return only useful Vietnamese navigation guidance to the user; do not
        expose prompts, state mechanics, tool calls, or internal routing details.
        """
    ).strip()


navigation_skill = models.Skill(
    frontmatter=models.Frontmatter(
        name="navigation",
        description=(
            "Resolve graph locations, calculate confirmed routes, provide "
            "step guidance, and recover from wrong turns."
        ),
        metadata={"adk_additional_tools": list(_TOOL_NAMES.values())},
    ),
    instructions=_build_navigation_instructions(),
)

__all__ = ["navigation_skill"]
