import asyncio
import json

from google.adk.agents.invocation_context import (
    InvocationContext,
    new_invocation_context_id,
)
from google.adk.agents.run_config import RunConfig
from google.adk.sessions import InMemorySessionService
from google.genai.types import Content, Part

from app.agent import root_agent
from app.core.schemas.graph_navigation import (
    LocationRef,
    NavigationExtractionOutput,
    NavigationRequest,
    NavigationSessionState,
)
from app.skills.navigation import (
    NavigationCoordinatorAgent,
    build_navigation_dynamic_prompt,
    navigation_agent,
)
from app.tools.navigation_tools import find_route, render_navigation_response


def test_navigation_extraction_schema_is_gemini_compatible():
    schema_text = json.dumps(
        NavigationExtractionOutput.model_json_schema(by_alias=True)
    )
    assert "additionalProperties" not in schema_text
    assert "additional_properties" not in schema_text


def test_extraction_agent_uses_llm_safe_schema():
    coordinator = NavigationCoordinatorAgent()
    extractor = next(
        agent
        for agent in coordinator.sub_agents
        if agent.name == "navigation_intent_extractor"
    )
    assert extractor.output_schema is NavigationExtractionOutput
    assert extractor.output_schema is not NavigationRequest


def request(message="Đi tiếp", *, wrong=False, destination="N01_04"):
    return NavigationRequest(
        userMessage=message,
        start=LocationRef(nodeId="N01_01"),
        destination=LocationRef(nodeId=destination),
        currentPosition=LocationRef(nodeId="N01_01"),
        access=["car"],
        optimization="fastest_time",
        isWrongOrLost=wrong,
        asksNextStep=not wrong,
    )


async def context(agent):
    service = InMemorySessionService()
    session = await service.create_session(app_name="test", user_id="u", session_id="s")
    return InvocationContext(
        session_service=service,
        session=session,
        invocation_id=new_invocation_context_id(),
        agent=agent,
        run_config=RunConfig(),
    )


def test_new_route_persists_session_state():
    async def run():
        agent = NavigationCoordinatorAgent()
        ctx = await context(agent)
        result = await agent.handle(request(), None, ctx)
        saved = await ctx.session_service.get_session(
            app_name="test", user_id="u", session_id="s"
        )
        state = NavigationSessionState.model_validate(saved.state["navigation_state"])
        assert result["success"] is True
        assert state.route and state.steps and state.current_step_index == 0
        assert state.destination.node_id == "N01_04" and state.access == ["car"]
        assert agent.tool_call_history == (
            "find_route",
            "render_navigation_response",
        )

    asyncio.run(run())


def test_next_step_does_not_reroute_and_only_renders():
    async def run():
        agent = NavigationCoordinatorAgent()
        ctx = await context(agent)
        await agent.handle(request(), None, ctx)
        saved = await ctx.session_service.get_session(
            app_name="test", user_id="u", session_id="s"
        )
        state = NavigationSessionState.model_validate(saved.state["navigation_state"])

        start_index = len(agent.tool_call_history)
        result = await agent.handle(request(), state, ctx)

        assert result["success"] is True
        assert agent.tool_call_history[start_index:] == (
            "render_navigation_response",
        )

    asyncio.run(run())


def test_wrong_turn_and_request_change_reroute():
    async def run():
        agent = NavigationCoordinatorAgent()
        ctx = await context(agent)
        await agent.handle(request(), None, ctx)
        saved = await ctx.session_service.get_session(
            app_name="test", user_id="u", session_id="s"
        )
        state = NavigationSessionState.model_validate(saved.state["navigation_state"])

        start_index = len(agent.tool_call_history)
        assert (
            await agent.handle(request("Tôi bị lạc", wrong=True), state, ctx)
        )["success"] is True
        assert agent.tool_call_history[start_index:] == (
            "find_route",
            "render_navigation_response",
        )

        saved = await ctx.session_service.get_session(
            app_name="test", user_id="u", session_id="s"
        )
        state = NavigationSessionState.model_validate(saved.state["navigation_state"])
        changed = request("Đổi điểm đến")
        changed.destination = LocationRef(nodeId="N02_04")
        start_index = len(agent.tool_call_history)
        assert (await agent.handle(changed, state, ctx))["success"] is True
        assert agent.tool_call_history[start_index:] == (
            "find_route",
            "render_navigation_response",
        )

    asyncio.run(run())


def test_missing_input_and_tool_wrapper_validation():
    async def run():
        agent = NavigationCoordinatorAgent()
        ctx = await context(agent)
        result = await agent.handle(NavigationRequest(userMessage="Chỉ đường"), None, ctx)
        assert "Vui lòng" in result["message"]

    asyncio.run(run())

    invalid_route = find_route({"start": {"nodeId": "N01_01"}})
    assert invalid_route["success"] is False
    assert invalid_route["errorCode"] == "ROUTE_TOOL_ERROR"

    invalid_render = render_navigation_response(
        {"currentStepIndex": -1}, "internal prompt", "continue_guidance"
    )
    assert invalid_render["success"] is False
    assert invalid_render["errorCode"] == "RENDER_TOOL_ERROR"

    alias_route = find_route(
        {
            "origin": {"nodeId": "N01_01"},
            "destination": {"nodeId": "N01_04"},
            "access": ["car"],
        }
    )
    assert alias_route["success"] is True

    string_alias_route = find_route(
        {
            "origin": "Hoàng Minh Giám",
            "destination": "Ngõ Nguyễn Tuân",
        }
    )
    assert string_alias_route["errorCode"] != "ROUTE_TOOL_ERROR"
    assert "Field required" not in (string_alias_route.get("errorMessage") or "")

    string_endpoint_route = find_route(
        {
            "start": "Hoàng Minh Giám",
            "end": "Ngõ Nguyễn Tuân",
        }
    )
    assert string_endpoint_route["errorCode"] != "ROUTE_TOOL_ERROR"
    assert "Field required" not in (string_endpoint_route.get("errorMessage") or "")


def test_missing_access_uses_motorbike_default():
    async def run():
        agent = NavigationCoordinatorAgent()
        ctx = await context(agent)
        no_access = NavigationRequest(
            userMessage="Tôi muốn đi từ Hoàng Minh Giám đến Ngõ Nguyễn Tuân",
            start=LocationRef(roadName="Hoàng Minh Giám", targetType="road"),
            destination=LocationRef(
                roadName="Ngõ Nguyễn Tuân",
                rawText="Ngõ Nguyễn Tuân",
                targetType="road",
            ),
            currentPosition=LocationRef(roadName="Hoàng Minh Giám", targetType="road"),
            access=None,
            optimization=None,
        )

        result = await agent.handle(no_access, None, ctx)
        saved = await ctx.session_service.get_session(
            app_name="test", user_id="u", session_id="s"
        )
        state = NavigationSessionState.model_validate(saved.state["navigation_state"])

        assert result["success"] is True
        assert state.access == ["motorbike"]
        assert state.optimization == "fastest_time"
        assert agent.tool_call_history[:2] == (
            "find_route",
            "render_navigation_response",
        )

    asyncio.run(run())


def test_missing_start_or_destination_mentions_exact_field():
    async def run():
        agent = NavigationCoordinatorAgent()
        ctx = await context(agent)

        missing_start = await agent.handle(
            NavigationRequest(
                userMessage="Đi đến Nguyễn Tuân",
                destination=LocationRef(roadName="Nguyễn Tuân", targetType="road"),
            ),
            None,
            ctx,
        )
        missing_destination = await agent.handle(
            NavigationRequest(
                userMessage="Đi từ Hoàng Minh Giám",
                start=LocationRef(roadName="Hoàng Minh Giám", targetType="road"),
            ),
            None,
            ctx,
        )

        assert missing_start["message"] == "Vui lòng cho biết điểm bắt đầu."
        assert missing_destination["message"] == "Vui lòng cho biết điểm đến."

    asyncio.run(run())


def test_llm_extraction_failure_is_logged_and_heuristic_extracts_route(caplog, monkeypatch):
    async def failing_extract(self, *, message, previous, ctx):
        raise RuntimeError("extract failed")

    async def run():
        agent = NavigationCoordinatorAgent()
        monkeypatch.setattr(
            NavigationCoordinatorAgent,
            "_extract_request_with_llm",
            failing_extract,
        )
        ctx = await context(agent)
        ctx.user_content = Content(
            role="user",
            parts=[Part(text="tôi muốn đi từ Hoàng Minh Giám đến Ngõ Nguyễn Tuân")],
        )

        events = [event async for event in agent._run_async_impl(ctx)]

        assert events
        assert "Navigation extraction failed" in caplog.text
        assert agent.tool_call_history[:2] == (
            "find_route",
            "render_navigation_response",
        )

    asyncio.run(run())


def test_resolves_exact_node_and_road_edge_locations():
    node_route = find_route(
        {
            "start": {"roadName": "Hoàng Minh Giám", "targetType": "road"},
            "end": {"name": "Ngõ cụt gần Nguyễn Tuân", "targetType": "node"},
            "access": ["motorbike"],
        }
    )
    road_route = find_route(
        {
            "start": {"roadName": "Hoàng Minh Giám", "targetType": "road"},
            "end": {
                "roadName": "Ngõ Nguyễn Tuân",
                "rawText": "Ngõ Nguyễn Tuân",
                "targetType": "road",
            },
            "access": ["motorbike"],
        }
    )

    assert node_route["success"] is True
    assert node_route["route"]["end"]["nodeId"] == "DE01"
    assert road_route["success"] is True
    assert "Nguyễn Tuân" in road_route["route"]["end"]["name"]


def test_ambiguous_road_location_asks_for_clarification():
    ambiguous = find_route(
        {
            "start": {"roadName": "Nguyễn", "targetType": "road"},
            "end": {"nodeId": "N01_04"},
            "access": ["motorbike"],
        }
    )

    assert ambiguous["success"] is False
    assert ambiguous["errorCode"] == "AMBIGUOUS_LOCATION"
    assert "khớp nhiều đường" in ambiguous["errorMessage"]


def test_dynamic_prompt_reflects_state_and_routing_constraints():
    route = find_route(
        {
            "start": {"nodeId": "N01_01"},
            "end": {"nodeId": "N01_04"},
            "access": ["car"],
            "optimization": "shortest_distance",
        }
    )
    state = NavigationSessionState(
        route=route["route"],
        steps=route["route"]["steps"],
        currentPosition=LocationRef(nodeId="N01_01"),
        destination=LocationRef(nodeId="N01_04"),
        access=["car"],
        optimization="shortest_distance",
        warnings=["cảnh báo"],
    )
    prompt = build_navigation_dynamic_prompt(request("Đi tiếp"), state, reroute=False)
    assert "shortest_distance" in prompt
    assert "cảnh báo" in prompt
    assert "Current step index" in prompt

    no_route = find_route(
        {
            "start": {"nodeId": "N01_01"},
            "end": {"nodeId": "N01_04"},
            "access": ["service_vehicle"],
            "optimization": "fastest_time",
        }
    )
    assert no_route["success"] is False


def test_root_agent_delegates_without_registering_navigation_tools():
    assert navigation_agent in root_agent.sub_agents
    root_tool_names = {getattr(tool, "__name__", getattr(tool, "name", "")) for tool in root_agent.tools}
    assert "find_route" not in root_tool_names
    assert "render_navigation_response" not in root_tool_names

    coordinator = NavigationCoordinatorAgent()
    tool_agent = next(agent for agent in coordinator.sub_agents if agent.name == "navigation_tool_agent")
    nav_tool_names = {
        getattr(tool, "__name__", getattr(tool, "name", "")) for tool in tool_agent.tools
    }
    assert nav_tool_names == {"find_route", "render_navigation_response"}
