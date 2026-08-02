"""Python implementation of the runtime navigation workflow for Google ADK."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from typing import Any

from pydantic import PrivateAttr

from google.adk.agents import Agent, BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.genai.types import Content, Part

from app.core.schemas.graph_navigation import (
    FindRouteRequest,
    LocationRef,
    NavigationRequest,
    NavigationSessionState,
    RouteResult,
)
from app.services.session import SessionStateService
from app.tools.navigation_tools import find_route, render_navigation_response

STATE_KEY = "navigation_state"


def build_navigation_dynamic_prompt(request: NavigationRequest, state: NavigationSessionState, *, reroute: bool) -> str:
    """Create the English runtime prompt consumed by the response renderer."""
    return "\n".join((
        "You are rendering verified navigation guidance. Never invent route data.",
        f"User request: {request.user_message}",
        f"Current route: {state.route.route_id if state.route else 'none'}",
        f"Current position: {state.current_position.model_dump() if state.current_position else 'unknown'}",
        f"Destination: {state.destination.model_dump() if state.destination else 'unknown'}",
        f"Access mode: {state.access}",
        f"Optimization: {state.optimization}",
        f"Current step index: {state.current_step_index}",
        f"Warnings: {state.warnings}",
        f"Reroute required: {reroute}",
        f"Reroute status: {state.reroute_reason or 'not required'}",
        "Respond in Vietnamese with one concise, action-oriented instruction.",
    ))


def _heuristic_request(message: str, previous: NavigationSessionState | None) -> NavigationRequest:
    """Fallback only; the extraction LLM is configured for normal ADK execution."""
    lower = message.casefold()
    return NavigationRequest(
        userMessage=message,
        destination=previous.destination if previous else None,
        currentPosition=previous.current_position if previous else None,
        access=previous.access if previous else None,
        optimization=previous.optimization if previous else None,
        isWrongOrLost=any(word in lower for word in ("lạc", "sai đường", "wrong turn", "lost", "missed turn")),
        asksNextStep=any(word in lower for word in ("bước tiếp", "tiếp theo", "next step", "continue")),
    )


class NavigationCoordinatorAgent(BaseAgent):
    """Deterministically calls route and render tools, with ADK-managed state."""

    _intent_extractor: Callable[[str, NavigationSessionState | None], NavigationRequest] = PrivateAttr()
    _intent_extraction_agent: Agent = PrivateAttr()

    def __init__(self, *, intent_extractor: Callable[[str, NavigationSessionState | None], NavigationRequest] | None = None) -> None:
        # This sub-agent provides typed LLM extraction when the runtime supplies a model.
        extraction_agent = Agent(
            name="navigation_intent_extractor",
            model="gemini-3.1-flash-lite",
            description="Extracts a typed navigation request from a user message.",
            instruction="Extract NavigationRequest fields from the user request and current state. Do not provide directions.",
            output_schema=NavigationRequest,
        )
        super().__init__(name="navigation_coordinator", description="Routes navigation requests with verified graph data.", sub_agents=[extraction_agent])
        self._intent_extraction_agent = extraction_agent
        self._intent_extractor = intent_extractor or _heuristic_request

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        message = ""
        if ctx.user_content and ctx.user_content.parts:
            message = " ".join(part.text or "" for part in ctx.user_content.parts)
        raw_state = ctx.session.state.get(STATE_KEY)
        previous = NavigationSessionState.model_validate(raw_state) if raw_state else None
        request = self._intent_extractor(message, previous)
        result = await self.handle(request, previous, ctx)
        yield Event(
            invocationId=ctx.invocation_id,
            author=self.name,
            content=Content(parts=[Part(text=result["message"])]),
        )

    async def handle(self, request: NavigationRequest, previous: NavigationSessionState | None, ctx: InvocationContext) -> dict[str, Any]:
        """Workflow entrypoint designed for direct unit testing and ADK execution."""
        changed = previous and ((request.destination and request.destination != previous.destination) or (request.access and request.access != previous.access) or (request.optimization and request.optimization != previous.optimization))
        reroute = bool(request.is_wrong_or_lost or changed or previous is None or previous.route is None)
        if reroute:
            start = request.current_position or request.start or (previous.current_position if previous else None)
            destination = request.destination or (previous.destination if previous else None)
            access = request.access or (previous.access if previous else None)
            optimization = request.optimization or (previous.optimization if previous else "fastest_time")
            if not start or not destination or not access:
                return {"success": False, "message": "Vui lòng cho biết vị trí hiện tại, điểm đến và phương thức di chuyển."}
            route_raw = find_route(FindRouteRequest(start=start, end=destination, currentPosition=start, access=access, optimization=optimization).model_dump(by_alias=True))
            route_result = RouteResult.model_validate(route_raw)
            if not route_result.success or route_result.route is None:
                return {"success": False, "message": route_result.error_message or "Không tìm thấy tuyến đường hợp lệ."}
            state = NavigationSessionState(route=route_result.route, steps=route_result.route.steps, currentStepIndex=0, currentPosition=start, destination=destination, access=access, optimization=optimization, warnings=route_result.warnings, rerouteRequired=False, rerouteReason="rerouted" if previous else "initial_route")
            await SessionStateService(ctx.session_service).update(app_name=ctx.session.app_name, user_id=ctx.session.user_id, session_id=ctx.session.id, state={STATE_KEY: state.model_dump(by_alias=True)})
            scenario = "wrong_turn_reroute" if previous else "initial_route"
        else:
            state = previous
            assert state is not None
            scenario = "continue_guidance"
        prompt = build_navigation_dynamic_prompt(request, state, reroute=reroute)
        return render_navigation_response(state.model_dump(by_alias=True), prompt, scenario)


navigation_agent = NavigationCoordinatorAgent()
