"""Python implementation of the runtime navigation workflow for Google ADK."""

import logging
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Any
from uuid import uuid4

from google.adk.agents import Agent, BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.adk.models.llm_response import LlmResponse
from google.genai.types import Content, Part
from pydantic import PrivateAttr

from app.core.enums import AccessMode, OptimizationMode
from app.core.schemas.graph_navigation import (
    FindRouteRequest,
    LocationRef,
    NavigationExtractionOutput,
    NavigationRequest,
    NavigationSessionState,
    RouteResult,
)
from app.services.session import SessionStateService
from app.skills.navigation.helper._heuristic_request import _heuristic_request
from app.skills.navigation.helper.build_prompt import (
    build_extraction_prompt,
    build_navigation_dynamic_prompt,
)
from app.tools.navigation_tools import find_route, render_navigation_response

STATE_KEY = "navigation_state"
EXTRACTION_OUTPUT_KEY = "navigation_request_extracted"
DEFAULT_ACCESS = [AccessMode.MOTORBIKE]
DEFAULT_OPTIMIZATION = OptimizationMode.FASTEST_TIME
logger = logging.getLogger(__name__)

IntentExtractor = Callable[
    [str, NavigationSessionState | None],
    Awaitable[NavigationRequest],
]


class NavigationCoordinatorAgent(BaseAgent):
    """Manages navigation workflow and state while executing ADK tools."""

    _intent_extractor: IntentExtractor | None = PrivateAttr(default=None)
    _intent_extraction_agent: Agent = PrivateAttr()
    _tool_agent: Agent = PrivateAttr()
    _pending_tool_call: dict[str, Any] | None = PrivateAttr(default=None)
    _tool_call_history: list[str] = PrivateAttr(default_factory=list)

    def __init__(
        self,
        *,
        intent_extractor: Callable[
            [str, NavigationSessionState | None], Awaitable[NavigationRequest]
        ]
        | None = None,
    ) -> None:
        extraction_agent = Agent(
            name="navigation_intent_extractor",
            model="gemini-3.1-flash-lite",
            description=(
                "Extracts a structured navigation request from the latest user message."
            ),
            instruction=(
                "Extract NavigationExtractionOutput fields. "
                "Do not calculate routes or provide directions."
            ),
            output_schema=NavigationExtractionOutput,
            output_key=EXTRACTION_OUTPUT_KEY,
            disallow_transfer_to_parent=True,
            disallow_transfer_to_peers=True,
        )
        tool_agent = Agent(
            name="navigation_tool_agent",
            model="gemini-3.1-flash-lite",
            description=(
                "Executes verified navigation ADK tools for route search and "
                "response rendering."
            ),
            instruction=(
                "You are the navigation skill tool runner. Use find_route only "
                "when the coordinator provides route-search arguments. Use "
                "render_navigation_response only when the coordinator provides "
                "verified navigation state and a dynamic prompt. Never invent "
                "routes, distances, times, or instructions."
            ),
            tools=[
                find_route,
                render_navigation_response,
            ],
            before_model_callback=self._tool_before_model_callback,
            disallow_transfer_to_parent=True,
            disallow_transfer_to_peers=True,
        )
        super().__init__(
            name="navigation_coordinator",
            description="Routes navigation requests with verified graph data.",
            sub_agents=[extraction_agent, tool_agent],
        )
        self._intent_extraction_agent = extraction_agent
        self._tool_agent = tool_agent
        self._intent_extractor = intent_extractor or _heuristic_request

    @property
    def tool_call_history(self) -> tuple[str, ...]:
        """Names of ADK tools executed by this coordinator instance."""
        return tuple(self._tool_call_history)

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        message = ""
        if ctx.user_content and ctx.user_content.parts:
            message = " ".join(part.text or "" for part in ctx.user_content.parts)
        raw_state = ctx.session.state.get(STATE_KEY)
        previous = (
            NavigationSessionState.model_validate(raw_state) if raw_state else None
        )

        try:
            request = await self._extract_request_with_llm(
                message=message,
                previous=previous,
                ctx=ctx,
            )
        except Exception as error:
            logger.exception("Navigation extraction failed: %s", error)
            request = _heuristic_request(message, previous)

        result = await self.handle(request, previous, ctx)
        yield Event(
            invocationId=ctx.invocation_id,
            author=self.name,
            content=Content(parts=[Part(text=result["message"])]),
        )

    async def _extract_request_with_llm(
        self,
        *,
        message: str,
        previous: NavigationSessionState | None,
        ctx: InvocationContext,
    ) -> NavigationRequest:
        extraction_prompt = build_extraction_prompt(message, previous)

        extraction_ctx = ctx.model_copy(
            update={
                "user_content": Content(
                    role="user", parts=[Part(text=extraction_prompt)]
                ),
            }
        )

        last_structured_text: str | None = None
        async for event in self._intent_extraction_agent.run_async(extraction_ctx):
            if event.author != self._intent_extraction_agent.name:
                continue

            if not event.content or not event.content.parts:
                continue

            texts = [
                part.text
                for part in event.content.parts
                if part.text and not getattr(part, "thought", False)
            ]

            if texts:
                last_structured_text = "".join(texts)

        raw_output = extraction_ctx.session.state.get(EXTRACTION_OUTPUT_KEY)

        if raw_output is not None:
            if isinstance(raw_output, str):
                return NavigationExtractionOutput.model_validate_json(
                    raw_output
                ).to_navigation_request(fallback_message=message)

            return NavigationExtractionOutput.model_validate(
                raw_output
            ).to_navigation_request(fallback_message=message)

        if last_structured_text:
            return NavigationExtractionOutput.model_validate_json(
                last_structured_text
            ).to_navigation_request(fallback_message=message)

        raise RuntimeError("Navigation intent extractor produced no structured output.")

    async def handle(
        self,
        request: NavigationRequest,
        previous: NavigationSessionState | None,
        ctx: InvocationContext,
    ) -> dict[str, Any]:
        """Workflow entrypoint designed for direct unit testing and ADK execution."""
        changed = previous and (
            (request.destination and request.destination != previous.destination)
            or (request.access and request.access != previous.access)
            or (request.optimization and request.optimization != previous.optimization)
        )
        reroute = bool(
            request.is_wrong_or_lost
            or changed
            or previous is None
            or previous.route is None
            or previous.reroute_required
        )
        if reroute:
            start = (
                request.current_position
                or request.start
                or (previous.current_position if previous else None)
            )
            destination = request.destination or (
                previous.destination if previous else None
            )
            access = (
                request.access
                or (previous.access if previous and previous.access else None)
                or DEFAULT_ACCESS
            )
            optimization = (
                request.optimization
                or (previous.optimization if previous else None)
                or DEFAULT_OPTIMIZATION
            )
            if not start or not destination:
                return {
                    "success": False,
                    "message": self._missing_location_message(
                        has_start=bool(start),
                        has_destination=bool(destination),
                    ),
                }

            if not isinstance(start, LocationRef):
                raise TypeError(
                    f"Expected start to be LocationRef, got "
                    f"{type(start).__name__}: {start!r}"
                )

            if not isinstance(destination, LocationRef):
                raise TypeError(
                    f"Expected destination to be LocationRef, got "
                    f"{type(destination).__name__}: {destination!r}"
                )

            route_raw = await self._run_navigation_tool(
                "find_route",
                {
                    "request": FindRouteRequest(
                        start=start,
                        end=destination,
                        currentPosition=start,
                        access=access,
                        optimization=optimization,
                    ).model_dump(by_alias=True)
                },
                ctx,
            )
            route_result = RouteResult.model_validate(route_raw)
            if not route_result.success or route_result.route is None:
                return {
                    "success": False,
                    "message": route_result.error_message
                    or "Không tìm thấy tuyến đường hợp lệ.",
                }

            reroute_reason = self._reroute_reason(
                request=request,
                previous=previous,
                changed=bool(changed),
            )
            state = NavigationSessionState(
                route=route_result.route,
                steps=route_result.route.steps,
                currentStepIndex=0,
                currentPosition=start,
                destination=destination,
                access=access,
                optimization=optimization,
                warnings=route_result.warnings,
                rerouteRequired=False,
                rerouteReason=reroute_reason,
            )
            await SessionStateService(ctx.session_service).update(
                app_name=ctx.session.app_name,
                user_id=ctx.session.user_id,
                session_id=ctx.session.id,
                state={STATE_KEY: state.model_dump(by_alias=True)},
            )
            scenario = (
                "wrong_turn_reroute"
                if request.is_wrong_or_lost
                else "reroute"
                if previous
                else "initial_route"
            )
        else:
            state = previous
            assert state is not None
            scenario = "continue_guidance"

        prompt = build_navigation_dynamic_prompt(
            request,
            state,
            reroute=reroute,
        )
        return await self._run_navigation_tool(
            "render_navigation_response",
            {
                "navigation_state": state.model_dump(by_alias=True),
                "dynamic_prompt": prompt,
                "scenario": scenario,
            },
            ctx,
        )

    async def _run_navigation_tool(
        self,
        tool_name: str,
        args: dict[str, Any],
        ctx: InvocationContext,
    ) -> dict[str, Any]:
        """Run the child tool agent and extract its ADK function response."""
        registered_names = {
            getattr(tool, "__name__", getattr(tool, "name", ""))
            for tool in self._tool_agent.tools
        }
        if tool_name not in registered_names:
            raise RuntimeError(
                f"Navigation tool '{tool_name}' is not registered on "
                f"{self._tool_agent.name}."
            )

        self._pending_tool_call = {
            "name": tool_name,
            "args": args,
            "call_id": f"{tool_name}-{uuid4()}",
            "function_call_emitted": False,
        }
        tool_ctx = ctx.model_copy(
            update={
                "agent": self._tool_agent,
                "user_content": Content(
                    role="user",
                    parts=[Part(text=f"Execute navigation tool: {tool_name}")],
                ),
            }
        )

        try:
            async for event in self._tool_agent.run_async(tool_ctx):
                for function_response in event.get_function_responses():
                    if function_response.name != tool_name:
                        continue
                    result = function_response.response
                    self._tool_call_history.append(tool_name)
                    if not isinstance(result, dict):
                        raise TypeError(
                            f"Navigation tool '{tool_name}' returned non-dict result."
                        )
                    return result
        finally:
            self._pending_tool_call = None

        raise RuntimeError(f"Navigation tool '{tool_name}' produced no response.")

    def _tool_before_model_callback(self, callback_context, llm_request):
        """Deterministically select one registered navigation tool for tests/runtime."""
        del callback_context, llm_request
        pending = self._pending_tool_call
        if not pending:
            return None

        if pending["function_call_emitted"]:
            return LlmResponse(
                content=Content(
                    role="model",
                    parts=[Part(text="Navigation tool execution complete.")],
                )
            )

        pending["function_call_emitted"] = True
        part = Part.from_function_call(
            name=pending["name"],
            args=pending["args"],
        )
        part.function_call.id = pending["call_id"]
        return LlmResponse(content=Content(role="model", parts=[part]))

    @staticmethod
    def _reroute_reason(
        *,
        request: NavigationRequest,
        previous: NavigationSessionState | None,
        changed: bool,
    ) -> str:
        if previous is None:
            return "initial_route"
        if request.is_wrong_or_lost:
            return "wrong_or_lost"
        if previous.reroute_required:
            return previous.reroute_reason or "route_invalid"
        if changed:
            return "request_changed"
        return "route_missing"

    @staticmethod
    def _missing_location_message(*, has_start: bool, has_destination: bool) -> str:
        if not has_start and not has_destination:
            return "Vui lòng cho biết điểm bắt đầu và điểm đến."
        if not has_start:
            return "Vui lòng cho biết điểm bắt đầu."
        return "Vui lòng cho biết điểm đến."


navigation_agent = NavigationCoordinatorAgent()
