"""ADK-callable tools for simulated urban navigation."""

from __future__ import annotations

from typing import Any

from app.core.schemas.graph_navigation import FindRouteRequest, NavigationSessionState, RouteResult
from app.services.navigation import NavigationService

_navigation_service = NavigationService()


def find_route(request: FindRouteRequest | dict[str, Any]) -> dict[str, Any]:
    """Find a valid graph route. This tool never reads or writes session state."""
    try:
        parsed = request if isinstance(request, FindRouteRequest) else FindRouteRequest.model_validate(request)
        return _navigation_service.find(parsed).model_dump(by_alias=True)
    except Exception as error:
        return RouteResult(success=False, errorCode="ROUTE_TOOL_ERROR", errorMessage=str(error)).model_dump(by_alias=True)


def render_navigation_response(
    navigation_state: NavigationSessionState | dict[str, Any],
    dynamic_prompt: str,
    scenario: str,
) -> dict[str, Any]:
    """Render verified state into a concise Vietnamese navigation response."""
    try:
        state = navigation_state if isinstance(navigation_state, NavigationSessionState) else NavigationSessionState.model_validate(navigation_state)
        return _navigation_service.render(state, dynamic_prompt=dynamic_prompt, scenario=scenario)
    except Exception as error:
        return {"success": False, "message": "Không thể tạo hướng dẫn điều hướng.", "errorCode": "RENDER_TOOL_ERROR", "errorMessage": str(error)}
