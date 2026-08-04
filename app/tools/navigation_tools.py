"""Small ADK function tools for simulated urban navigation."""

import json
from typing import Any

from google.adk.tools import ToolContext

from app.core.enums import AccessMode, OptimizationMode
from app.core.schemas.navigation import NAVIGATION_STATE_KEY, NavigationState
from app.services.navigation import NavigationService
from app.services.navigation.location_resolver import LocationTargetType

_navigation_service = NavigationService()


def get_navigation_tools():
    return list(NAVIGATION_TOOLS.values())


def get_navigation_state(tool_context: ToolContext) -> dict[str, Any]:
    """Return the current serializable navigation state for this session."""

    try:
        state = _read_navigation_state(tool_context)
        return {"success": True, "state": _serialize_state(state)}
    except Exception as error:
        return _error("INVALID_STATE", str(error))


def update_navigation_state(
    changes: dict[str, Any],
    tool_context: ToolContext,
) -> dict[str, Any]:
    """Merge validated navigation fields into the current session state."""

    try:
        current = _read_navigation_state(tool_context)
        payload = current.model_dump(mode="python")
        payload.update(_canonical_state_changes(changes))
        updated = NavigationState.model_validate(payload)
        serialized = _serialize_state(updated)
        tool_context.state[NAVIGATION_STATE_KEY] = serialized
        return {"success": True, "state": serialized}
    except Exception as error:
        return _error("INVALID_STATE", str(error))


def search_locations(
    query: str,
    target_type: LocationTargetType,
    min_similarity: float,
) -> dict[str, Any]:
    """Find graph-node candidates matching a location name, ID, or road."""

    normalized_query = query.strip()
    if not normalized_query:
        return _error("INVALID_QUERY", "query must not be empty")
    if target_type not in {"auto", "node", "road"}:
        return _error(
            "INVALID_TARGET_TYPE",
            "target_type must be auto, node, or road",
        )
    if not 0.0 <= min_similarity <= 1.0:
        return _error(
            "INVALID_SIMILARITY",
            "min_similarity must be between 0 and 1",
        )

    try:
        candidates = _navigation_service.locations.search(
            normalized_query,
            target_type,
            min_similarity,
        )
        return {"success": True, "candidates": candidates}
    except Exception as error:
        return _error("NAVIGATION_TOOL_ERROR", str(error))


def find_route(
    start_node_id: str,
    end_node_id: str,
    access: AccessMode,
    optimization: OptimizationMode,
) -> dict[str, Any]:
    """Find a route between two graph nodes for one travel mode."""

    options = _route_options(access, optimization)
    if isinstance(options, dict):
        return options
    access_mode, optimization_mode = options

    try:
        route = _navigation_service.routes.find_route(
            start_node_id,
            end_node_id,
            access_mode,
            optimization_mode,
        )
    except ValueError as error:
        return _error("NODE_NOT_FOUND", str(error))
    except Exception as error:
        return _error("NAVIGATION_TOOL_ERROR", str(error))

    if route is None:
        return _error(
            "NO_ROUTE",
            "Không có tuyến đường hợp lệ với phương tiện đã chọn.",
        )
    return {"success": True, "route": route}


def find_recovery_route(
    current_node_id: str,
    route_node_ids: list[str],
    current_step_index: int,
    access: AccessMode,
    optimization: OptimizationMode,
) -> dict[str, Any]:
    """Find a temporary route to the nearest reachable remaining route node."""

    options = _route_options(access, optimization)
    if isinstance(options, dict):
        return options
    access_mode, optimization_mode = options

    try:
        result = _navigation_service.routes.find_recovery_route(
            current_node_id,
            route_node_ids,
            current_step_index,
            access_mode,
            optimization_mode,
        )
    except ValueError as error:
        return _error("INVALID_RECOVERY_INPUT", str(error))
    except Exception as error:
        return _error("NAVIGATION_TOOL_ERROR", str(error))

    if result is None:
        return _error(
            "NO_RECOVERY_ROUTE",
            "Không có tuyến phục hồi tới phần còn lại của tuyến chính.",
        )

    recovery_route, resume_step_index = result
    return {
        "success": True,
        "recoveryRoute": recovery_route,
        "resumeStepIndex": resume_step_index,
    }


def _read_navigation_state(tool_context: ToolContext) -> NavigationState:
    raw_state = tool_context.state.get(NAVIGATION_STATE_KEY)
    return NavigationState.model_validate(raw_state if raw_state is not None else {})


def _canonical_state_changes(changes: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(changes, dict):
        raise TypeError("changes must be a dictionary")

    aliases = {
        field.alias or field_name: field_name
        for field_name, field in NavigationState.model_fields.items()
    }
    canonical: dict[str, Any] = {}
    for key, value in changes.items():
        field_name = aliases.get(key, key)
        if field_name not in NavigationState.model_fields:
            raise ValueError(f"Unknown navigation state field: {key}")
        canonical[field_name] = value
    return canonical


def _serialize_state(state: NavigationState) -> dict[str, Any]:
    serialized = state.model_dump(mode="json", by_alias=True)
    json.dumps(serialized, ensure_ascii=False)
    return serialized


def _route_options(
    access: AccessMode,
    optimization: OptimizationMode,
) -> tuple[AccessMode, OptimizationMode] | dict[str, Any]:
    try:
        access_mode = AccessMode(access)
    except ValueError:
        return _error("INVALID_ACCESS", f"Unsupported access mode: {access}")

    try:
        optimization_mode = OptimizationMode(optimization)
    except ValueError:
        return _error(
            "INVALID_OPTIMIZATION",
            f"Unsupported optimization mode: {optimization}",
        )
    return access_mode, optimization_mode


def _error(code: str, message: str) -> dict[str, Any]:
    return {
        "success": False,
        "errorCode": code,
        "errorMessage": message,
    }


NAVIGATION_TOOLS = {
    "get_state": get_navigation_state,
    "update_state": update_navigation_state,
    "search_location": search_locations,
    "find_route": find_route,
    "find_recovery_route": find_recovery_route,
}


__all__ = [
    "NAVIGATION_TOOLS",
    "find_recovery_route",
    "find_route",
    "get_navigation_state",
    "search_locations",
    "update_navigation_state",
]
