import asyncio

from google.adk.agents.invocation_context import InvocationContext, new_invocation_context_id
from google.adk.sessions import InMemorySessionService

from app.core.schemas.graph_navigation import LocationRef, NavigationRequest, NavigationSessionState
from app.skills.navigation import NavigationCoordinatorAgent, build_navigation_dynamic_prompt
from app.tools.navigation_tools import find_route


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
    return InvocationContext(session_service=service, session=session, invocation_id=new_invocation_context_id(), agent=agent)


def test_new_route_persists_session_state():
    async def run():
        agent = NavigationCoordinatorAgent()
        ctx = await context(agent)
        result = await agent.handle(request(), None, ctx)
        saved = await ctx.session_service.get_session(app_name="test", user_id="u", session_id="s")
        state = NavigationSessionState.model_validate(saved.state["navigation_state"])
        assert result["success"] is True
        assert state.route and state.steps and state.current_step_index == 0
        assert state.destination.node_id == "N01_04" and state.access == ["car"]
    asyncio.run(run())


def test_next_step_does_not_reroute(monkeypatch):
    async def run():
        agent = NavigationCoordinatorAgent()
        ctx = await context(agent)
        await agent.handle(request(), None, ctx)
        import app.skills.navigation as navigation
        monkeypatch.setattr(navigation, "find_route", lambda _: (_ for _ in ()).throw(AssertionError("must not reroute")))
        saved = await ctx.session_service.get_session(app_name="test", user_id="u", session_id="s")
        state = NavigationSessionState.model_validate(saved.state["navigation_state"])
        result = await agent.handle(request(), state, ctx)
        assert result["success"] is True
    asyncio.run(run())


def test_wrong_turn_reroutes(monkeypatch):
    async def run():
        agent = NavigationCoordinatorAgent()
        ctx = await context(agent)
        await agent.handle(request(), None, ctx)
        saved = await ctx.session_service.get_session(app_name="test", user_id="u", session_id="s")
        state = NavigationSessionState.model_validate(saved.state["navigation_state"])
        import app.skills.navigation as navigation
        called = {"value": False}
        original = navigation.find_route
        monkeypatch.setattr(navigation, "find_route", lambda value: (called.__setitem__("value", True) or original(value)))
        assert (await agent.handle(request("Tôi bị lạc", wrong=True), state, ctx))["success"] is True
        assert called["value"] is True
    asyncio.run(run())


def test_missing_input_and_tool_error(monkeypatch):
    async def run():
        agent = NavigationCoordinatorAgent()
        ctx = await context(agent)
        assert "Vui lòng" in (await agent.handle(NavigationRequest(userMessage="Chỉ đường"), None, ctx))["message"]
        import app.skills.navigation as navigation
        monkeypatch.setattr(navigation, "find_route", lambda _: {"success": False, "errorCode": "X", "errorMessage": "tool failed"})
        assert "tool failed" in (await agent.handle(request(), None, ctx))["message"]
        monkeypatch.setattr(navigation, "find_route", find_route)
        monkeypatch.setattr(navigation, "render_navigation_response", lambda *_: {"success": False, "message": "render failed", "errorCode": "RENDER_TOOL_ERROR"})
        assert (await agent.handle(request(), None, ctx))["message"] == "render failed"
    asyncio.run(run())


def test_dynamic_prompt_reflects_state_and_routing_constraints():
    route = find_route({"start": {"nodeId": "N01_01"}, "end": {"nodeId": "N01_04"}, "access": ["car"], "optimization": "shortest_distance"})
    state = NavigationSessionState(route=route["route"], steps=route["route"]["steps"], currentPosition=LocationRef(nodeId="N01_01"), destination=LocationRef(nodeId="N01_04"), access=["car"], optimization="shortest_distance", warnings=["cảnh báo"])
    prompt = build_navigation_dynamic_prompt(request("Đi tiếp"), state, reroute=False)
    assert "shortest_distance" in prompt and "cảnh báo" in prompt and "Current step index" in prompt
    no_route = find_route({"start": {"nodeId": "N01_01"}, "end": {"nodeId": "N01_04"}, "access": ["service_vehicle"], "optimization": "fastest_time"})
    assert no_route["success"] is False
