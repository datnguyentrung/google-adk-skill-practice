"""ADK-callable tools for simulated urban navigation."""

from __future__ import annotations

from typing import Any

from app.core.schemas.graph_navigation import (
    FindRouteRequest,
    NavigationSessionState,
    RouteResult,
)
from app.services.navigation import NavigationService

_navigation_service = NavigationService()


def find_route(request: dict[str, Any]) -> dict[str, Any]:
    """Find a verified route when no valid route is active or rerouting is required.

    Call this tool only for initial routing or when the destination, access mode,
    optimization preference, or user's route validity changes. Do not call it for
    simple next-step guidance on an already valid route, and do not use it to read
    or write session state.
    """
    try:
        payload = _normalize_find_route_payload(request)
        parsed = FindRouteRequest.model_validate(payload)
        result = _navigation_service.find(parsed)
        return result.model_dump(mode="json", by_alias=True)

    except Exception as error:
        return RouteResult(
            success=False,
            errorCode="ROUTE_TOOL_ERROR",
            errorMessage=str(error),
        ).model_dump(mode="json", by_alias=True)


def _normalize_find_route_payload(request: dict[str, Any]) -> dict[str, Any]:
    payload = dict(request)
    for alias in ("origin", "from"):
        if alias in payload and "start" not in payload:
            payload["start"] = payload.pop(alias)
            break

    for alias in ("destination", "to"):
        if alias in payload and "end" not in payload:
            payload["end"] = payload.pop(alias)
            break

    for key in ("start", "end", "currentPosition"):
        if key in payload:
            payload[key] = _normalize_location_input(payload[key])

    return payload


def _normalize_location_input(value: Any) -> Any:
    if not isinstance(value, str):
        return value

    text = value.strip()
    if not text:
        return value

    return {
        "roadName": text,
        "rawText": text,
        "targetType": _infer_target_type(text),
    }


def _infer_target_type(value: str) -> str:
    lower = value.casefold().strip()
    if lower.startswith(("ngõ ", "đường ", "phố ", "hẻm ", "tuyến ")):
        return "road"
    return "auto"


def render_navigation_response(
    navigation_state: dict[str, Any],
    dynamic_prompt: str,
    scenario: str,
) -> dict[str, Any]:
    """Render verified navigation state into a Vietnamese user-facing response.

    Call this tool after the Python workflow has selected or persisted the route
    state and built the internal English dynamic prompt. Do not call it to compute
    routes, mutate state, or invent distance, time, road, or instruction data.
    """
    try:
        state = NavigationSessionState.model_validate(navigation_state)
        return _navigation_service.render(
            state, dynamic_prompt=dynamic_prompt, scenario=scenario
        )
    except Exception as error:
        return {
            "success": False,
            "message": "Không thể tạo hướng dẫn điều hướng.",
            "errorCode": "RENDER_TOOL_ERROR",
            "errorMessage": str(error),
        }
