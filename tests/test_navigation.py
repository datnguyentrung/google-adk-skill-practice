import asyncio
import inspect
from pathlib import Path
from types import SimpleNamespace

from google.adk.agents.invocation_context import (
    InvocationContext,
    new_invocation_context_id,
)
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.agents.run_config import RunConfig
from google.adk.sessions import InMemorySessionService
from google.adk.skills import load_skill_from_dir
from google.adk.tools import FunctionTool

from app.agent import root_agent, root_skill_toolset
from app.core.enums import AccessMode, OptimizationMode
from app.core.schemas.navigation import NAVIGATION_STATE_KEY, NavigationGraph
from app.services.navigation import NavigationService, RouteFinder
from app.skills.navigation import navigation_skill
from app.tools.navigation_tools import (
    NAVIGATION_TOOLS,
    find_recovery_route,
    find_route,
    get_navigation_state,
    search_locations,
    update_navigation_state,
)

NAVIGATION_TOOL_NAMES = {tool.__name__ for tool in NAVIGATION_TOOLS.values()}
NAVIGATION_SKILL_DIR = Path(__file__).parents[1] / "app" / "skills" / "navigation"


def _tool_context() -> SimpleNamespace:
    return SimpleNamespace(state={})


def _edge(
    edge_id: str,
    to_node_id: str,
    *,
    distance: float,
    time: float,
    status: str = "open",
    access: list[str] | None = None,
) -> dict:
    return {
        "id": edge_id,
        "to": to_node_id,
        "roadName": f"Road {edge_id}",
        "distance": distance,
        "time": time,
        "heading": 90,
        "oneWay": True,
        "roadType": "main",
        "maxSpeed": 40,
        "status": status,
        "access": access or ["car"],
        "toll": False,
        "level": 0,
        "instructions": {
            "default": f"Follow Road {edge_id}",
            "landmark": f"Landmark {edge_id}",
        },
        "restrictions": {},
    }


def _graph(adjacency: dict[str, list[dict]]) -> NavigationGraph:
    node_ids = set(adjacency)
    node_ids.update(edge["to"] for edges in adjacency.values() for edge in edges)
    complete_adjacency = {
        node_id: adjacency.get(node_id, []) for node_id in sorted(node_ids)
    }
    return NavigationGraph.model_validate(
        {
            "metadata": {
                "name": "test graph",
                "description": "controlled navigation test graph",
                "city": "test",
                "isSimulation": True,
                "distanceUnit": "meter",
                "timeUnit": "second",
                "coordinateSystem": "WGS84",
            },
            "nodes": {
                node_id: {
                    "name": f"Node {node_id}",
                    "type": "intersection",
                    "lat": 0,
                    "lng": 0,
                    "level": 0,
                    "description": f"Description {node_id}",
                }
                for node_id in complete_adjacency
            },
            "graph": complete_adjacency,
        }
    )


def test_state_defaults_and_patch_updates_are_serializable():
    context = _tool_context()

    initial = get_navigation_state(context)
    assert initial["success"] is True
    assert initial["state"] == {
        "startPositionInput": None,
        "endPositionInput": None,
        "startPosition": None,
        "endPosition": None,
        "access": None,
        "optimization": "fastest_time",
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

    updated = update_navigation_state(
        {
            "startPositionInput": "N01_01",
            "endPositionInput": "Pham Hung",
            "access": "motorbike",
            "awaitingConfirmation": True,
            "status": "awaiting_route_confirmation",
        },
        context,
    )
    assert updated["success"] is True
    assert updated["state"]["startPositionInput"] == "N01_01"
    assert updated["state"]["optimization"] == "fastest_time"
    assert context.state[NAVIGATION_STATE_KEY] == updated["state"]

    cleared = update_navigation_state(
        {"endPositionInput": None, "awaitingConfirmation": False},
        context,
    )
    assert cleared["state"]["endPositionInput"] is None
    assert cleared["state"]["startPositionInput"] == "N01_01"


def test_state_patch_accepts_python_names_and_rejects_invalid_data():
    context = _tool_context()
    snake_case = update_navigation_state({"current_step_index": 2}, context)
    assert snake_case["state"]["currentStepIndex"] == 2

    unknown = update_navigation_state({"steps": []}, context)
    assert unknown["success"] is False
    assert unknown["errorCode"] == "INVALID_STATE"
    assert context.state[NAVIGATION_STATE_KEY]["currentStepIndex"] == 2

    negative = update_navigation_state({"currentStepIndex": -1}, context)
    assert negative["success"] is False
    assert negative["errorCode"] == "INVALID_STATE"

    unserializable = update_navigation_state(
        {"route": {"value": object()}},
        context,
    )
    assert unserializable["success"] is False
    assert unserializable["errorCode"] == "INVALID_STATE"
    assert context.state[NAVIGATION_STATE_KEY]["route"] is None

    context.state[NAVIGATION_STATE_KEY] = []
    corrupt = get_navigation_state(context)
    assert corrupt["success"] is False
    assert corrupt["errorCode"] == "INVALID_STATE"


def test_search_locations_resolves_nodes_and_unaccented_roads():
    exact_node = search_locations("N01_01", "node", 0.8)
    assert exact_node["success"] is True
    assert exact_node["candidates"][0]["nodeId"] == "N01_01"
    assert exact_node["candidates"][0]["targetType"] == "node"

    fuzzy_node = search_locations(
        "Nguyen Trai Khuat Duy Tien",
        "node",
        0.8,
    )
    assert fuzzy_node["candidates"][0]["nodeId"] == "N01_04"

    road = search_locations("duong Pham Hung", "road", 0.8)
    assert road["success"] is True
    assert len(road["candidates"]) == 5
    assert all(candidate["targetType"] == "road" for candidate in road["candidates"])
    assert all(candidate["roadName"] == "Phạm Hùng" for candidate in road["candidates"])
    assert len({candidate["nodeId"] for candidate in road["candidates"]}) == 5


def test_search_locations_uses_strict_threshold_and_validates_input():
    strict = search_locations("N01_01", "node", 1.0)
    assert strict == {"success": True, "candidates": []}

    missing = search_locations("not present anywhere", "auto", 0.99)
    assert missing == {"success": True, "candidates": []}

    assert search_locations("", "auto", 0.8)["errorCode"] == "INVALID_QUERY"
    assert (
        search_locations("N01_01", "building", 0.8)["errorCode"]
        == "INVALID_TARGET_TYPE"
    )
    assert search_locations("N01_01", "node", 1.1)["errorCode"] == "INVALID_SIMILARITY"


def test_search_locations_does_not_round_a_score_down_to_the_threshold(
    monkeypatch,
):
    resolver = NavigationService().locations
    monkeypatch.setattr(
        resolver,
        "_similarity",
        lambda needle, haystack: 0.80004,
    )

    candidates = resolver.search("near threshold", "node", 0.8, limit=1)
    assert candidates[0]["similarity"] == 0.80004
    assert candidates[0]["similarity"] > 0.8


def test_find_route_tool_returns_small_verified_route_shape():
    result = find_route(
        "N01_01",
        "N01_04",
        "car",
        "fastest_time",
    )
    assert result["success"] is True
    route = result["route"]
    assert set(route) == {
        "routeId",
        "startNodeId",
        "endNodeId",
        "optimization",
        "totalDistance",
        "totalTime",
        "steps",
        "warnings",
    }
    assert route["steps"]
    step = route["steps"][0]
    assert set(step) == {"index", "fromNode", "toNode", "edge", "turn"}
    assert step["fromNode"]["id"] == "N01_01"
    assert step["edge"]["to"] == step["toNode"]["id"]
    assert "instructions" in step["edge"]


def test_find_route_tool_validates_nodes_and_options():
    assert (
        find_route("missing", "N01_01", "car", "fastest_time")["errorCode"]
        == "NODE_NOT_FOUND"
    )
    assert (
        find_route("N01_01", "N01_04", "plane", "fastest_time")["errorCode"]
        == "INVALID_ACCESS"
    )
    assert (
        find_route("N01_01", "N01_04", "car", "cheapest")["errorCode"]
        == "INVALID_OPTIMIZATION"
    )


def test_route_finder_chooses_requested_cost_and_handles_zero_steps():
    graph = _graph(
        {
            "A": [
                _edge("AB", "B", distance=10, time=2),
                _edge("AC", "C", distance=2, time=10),
            ],
            "C": [_edge("CB", "B", distance=2, time=10)],
        }
    )
    finder = RouteFinder(graph)

    shortest = finder.find_route(
        "A",
        "B",
        AccessMode.CAR,
        OptimizationMode.SHORTEST_DISTANCE,
    )
    fastest = finder.find_route(
        "A",
        "B",
        AccessMode.CAR,
        OptimizationMode.FASTEST_TIME,
    )
    same_node = finder.find_route(
        "A",
        "A",
        AccessMode.CAR,
        OptimizationMode.FASTEST_TIME,
    )

    assert [step["edge"]["id"] for step in shortest["steps"]] == ["AC", "CB"]
    assert [step["edge"]["id"] for step in fastest["steps"]] == ["AB"]
    assert same_node["steps"] == []
    assert same_node["totalDistance"] == 0
    assert same_node["totalTime"] == 0


def test_route_finder_skips_closed_restricted_and_inaccessible_edges():
    graph = _graph(
        {
            "A": [
                _edge("closed", "B", distance=1, time=1, status="closed"),
                _edge(
                    "restricted",
                    "C",
                    distance=1,
                    time=1,
                    status="restricted",
                ),
                _edge(
                    "pedestrian",
                    "D",
                    distance=1,
                    time=1,
                    access=["pedestrian"],
                ),
            ]
        }
    )
    finder = RouteFinder(graph)

    for destination in ("B", "C", "D"):
        assert (
            finder.find_route(
                "A",
                destination,
                AccessMode.CAR,
                OptimizationMode.FASTEST_TIME,
            )
            is None
        )


def test_recovery_finds_cheapest_remaining_node_and_resume_index():
    graph = _graph(
        {
            "A": [_edge("AB", "B", distance=5, time=5)],
            "B": [_edge("BC", "C", distance=5, time=5)],
            "X": [_edge("XB", "B", distance=1, time=1)],
        }
    )
    finder = RouteFinder(graph)

    recovered = finder.find_recovery_route(
        "X",
        ["A", "B", "C"],
        0,
        AccessMode.CAR,
        OptimizationMode.FASTEST_TIME,
    )
    assert recovered is not None
    route, resume_index = recovered
    assert route["endNodeId"] == "B"
    assert [step["edge"]["id"] for step in route["steps"]] == ["XB"]
    assert resume_index == 1

    already_on_route = finder.find_recovery_route(
        "B",
        ["A", "B", "C"],
        0,
        AccessMode.CAR,
        OptimizationMode.FASTEST_TIME,
    )
    route, resume_index = already_on_route
    assert route["steps"] == []
    assert resume_index == 1


def test_recovery_reports_invalid_and_unreachable_requests():
    invalid = find_recovery_route(
        "N01_01",
        [],
        0,
        "car",
        "fastest_time",
    )
    assert invalid["errorCode"] == "INVALID_RECOVERY_INPUT"

    graph = _graph({"A": [], "B": [], "X": []})
    finder = RouteFinder(graph)
    assert (
        finder.find_recovery_route(
            "X",
            ["A", "B"],
            0,
            AccessMode.CAR,
            OptimizationMode.FASTEST_TIME,
        )
        is None
    )


def test_state_supports_confirmation_progress_recovery_and_lost_reset():
    context = _tool_context()
    positions = {
        "startPosition": {"nodeId": "A", "name": "Node A"},
        "endPosition": {"nodeId": "C", "name": "Node C"},
        "access": "car",
        "awaitingConfirmation": True,
        "status": "awaiting_route_confirmation",
    }
    assert update_navigation_state(positions, context)["success"] is True

    route = {
        "routeId": "main",
        "startNodeId": "A",
        "endNodeId": "C",
        "optimization": "fastest_time",
        "totalDistance": 2,
        "totalTime": 2,
        "steps": [{"index": 0}, {"index": 1}],
        "warnings": [],
    }
    navigating = update_navigation_state(
        {
            "route": route,
            "currentStepIndex": 1,
            "awaitingConfirmation": False,
            "status": "navigating",
        },
        context,
    )
    assert navigating["state"]["currentStepIndex"] == 1
    before_info_question = get_navigation_state(context)["state"]
    assert get_navigation_state(context)["state"] == before_info_question

    recovering = update_navigation_state(
        {
            "recoveryRoute": {"routeId": "recovery", "steps": []},
            "recoveryStepIndex": 0,
            "resumeStepIndex": 2,
            "scenario": "wrong_turn_reroute",
            "status": "recovering",
        },
        context,
    )
    assert recovering["state"]["route"]["routeId"] == "main"

    lost = update_navigation_state(
        {
            "startPositionInput": None,
            "startPosition": None,
            "route": None,
            "currentStepIndex": 0,
            "recoveryRoute": None,
            "recoveryStepIndex": 0,
            "resumeStepIndex": None,
            "awaitingConfirmation": False,
            "scenario": "forgotten_route",
            "status": "collecting_current_position",
        },
        context,
    )
    assert lost["state"]["endPosition"]["nodeId"] == "C"
    assert lost["state"]["access"] == "car"
    assert lost["state"]["route"] is None


def test_tool_sequence_supports_destination_first_confirmation_and_arrival():
    context = _tool_context()
    collected = update_navigation_state(
        {
            "startPositionInput": "N01_01",
            "endPositionInput": "N01_04",
            "access": "car",
        },
        context,
    )["state"]
    assert collected["route"] is None
    assert collected["status"] == "collecting_input"

    destination = search_locations(
        collected["endPositionInput"],
        "auto",
        0.8,
    )["candidates"][0]
    pending_destination = update_navigation_state(
        {
            "pendingSelection": {
                "field": "endPosition",
                "candidates": [destination],
            },
            "status": "awaiting_location_selection",
        },
        context,
    )["state"]
    assert pending_destination["startPosition"] is None
    assert pending_destination["endPosition"] is None

    destination_resolved = update_navigation_state(
        {"endPosition": destination, "pendingSelection": None},
        context,
    )["state"]
    assert destination_resolved["endPosition"]["nodeId"] == "N01_04"
    assert destination_resolved["startPosition"] is None

    start = search_locations(
        destination_resolved["startPositionInput"],
        "auto",
        0.8,
    )["candidates"][0]
    pending_start = update_navigation_state(
        {
            "pendingSelection": {
                "field": "startPosition",
                "candidates": [start],
            },
            "status": "awaiting_location_selection",
        },
        context,
    )["state"]
    assert pending_start["endPosition"]["nodeId"] == "N01_04"
    assert pending_start["startPosition"] is None

    resolved = update_navigation_state(
        {
            "startPosition": start,
            "pendingSelection": None,
            "awaitingConfirmation": True,
            "status": "awaiting_route_confirmation",
        },
        context,
    )["state"]
    assert resolved["route"] is None
    assert resolved["awaitingConfirmation"] is True

    route_result = find_route(
        resolved["startPosition"]["nodeId"],
        resolved["endPosition"]["nodeId"],
        resolved["access"],
        resolved["optimization"],
    )
    assert route_result["success"] is True
    navigating = update_navigation_state(
        {
            "route": route_result["route"],
            "currentStepIndex": 0,
            "awaitingConfirmation": False,
            "status": "navigating",
        },
        context,
    )["state"]
    assert navigating["route"]["steps"]
    assert navigating["route"]["steps"][0]["fromNode"]["id"] == "N01_01"

    arrived = update_navigation_state(
        {
            "currentStepIndex": len(navigating["route"]["steps"]),
            "status": "arrived",
        },
        context,
    )["state"]
    assert arrived["status"] == "arrived"
    assert arrived["endPosition"]["nodeId"] == "N01_04"


def test_recovery_candidate_selection_survives_a_turn_without_replacing_route():
    context = _tool_context()
    main_route = find_route(
        "N01_01",
        "N01_04",
        "car",
        "fastest_time",
    )["route"]
    candidates = search_locations("Pham Hung", "road", 0.8)["candidates"]
    original = update_navigation_state(
        {
            "startPosition": {"nodeId": "N01_01", "name": "Start"},
            "endPosition": {"nodeId": "N01_04", "name": "End"},
            "access": "car",
            "route": main_route,
            "pendingSelection": {
                "field": "recoveryPosition",
                "candidates": candidates,
            },
            "status": "awaiting_location_selection",
        },
        context,
    )["state"]

    next_turn = get_navigation_state(context)["state"]
    assert next_turn["pendingSelection"]["field"] == "recoveryPosition"
    assert next_turn["route"]["routeId"] == original["route"]["routeId"]
    selected_node_id = next_turn["pendingSelection"]["candidates"][0]["nodeId"]
    cleared = update_navigation_state(
        {"pendingSelection": None, "status": "recovering"},
        context,
    )["state"]
    assert selected_node_id
    assert cleared["startPosition"]["nodeId"] == "N01_01"
    assert cleared["endPosition"]["nodeId"] == "N01_04"
    assert cleared["route"]["routeId"] == main_route["routeId"]


def test_navigation_service_loads_one_validated_graph():
    service = NavigationService()
    assert service.graph is service.graph
    assert service.locations is service.locations
    assert service.routes is service.routes
    assert service.graph.nodes


def test_navigation_skill_declares_flow_and_exact_dynamic_tools():
    raw_skill = load_skill_from_dir(NAVIGATION_SKILL_DIR)
    assert raw_skill.name == "navigation"
    assert "$state_contract" in raw_skill.instructions
    assert raw_skill.instructions != navigation_skill.instructions

    metadata = navigation_skill.frontmatter.metadata
    assert set(metadata["adk_additional_tools"]) == NAVIGATION_TOOL_NAMES

    instructions = navigation_skill.instructions
    assert NAVIGATION_STATE_KEY in instructions
    assert "Similarity threshold: strictly greater than `0.80`" in instructions
    assert "Default optimization: `fastest_time`" in instructions
    assert "find_recovery_route" in instructions
    assert '"startPositionInput": null' in instructions
    assert "$" not in instructions

    required_rules = [
        "Ask only for missing fields",
        "Resolve destination before start",
        "explicit route confirmation",
        "An unrelated reply is not confirmation",
        "currentStepIndex",
        "Advance an index only when the user clearly confirms",
        "find_recovery_route",
        "resumeStepIndex",
        "User is lost",
        "Current node and edge questions",
        "information-only question",
        "do not advance any index",
    ]
    for rule in required_rules:
        assert rule in instructions


def test_navigation_tool_signatures_have_no_defaults():
    for tool in NAVIGATION_TOOLS.values():
        for parameter in inspect.signature(tool).parameters.values():
            assert parameter.default is inspect.Parameter.empty
        assert inspect.signature(tool).return_annotation != inspect.Signature.empty

    route_schema = FunctionTool(find_route)._get_declaration().parameters_json_schema
    assert route_schema["$defs"]["AccessMode"]["enum"] == [
        mode.value for mode in AccessMode
    ]
    assert route_schema["$defs"]["OptimizationMode"]["enum"] == [
        mode.value for mode in OptimizationMode
    ]
    search_schema = (
        FunctionTool(search_locations)._get_declaration().parameters_json_schema
    )
    assert search_schema["properties"]["target_type"]["enum"] == [
        "auto",
        "node",
        "road",
    ]


def test_root_agent_activates_navigation_tools_only_after_skill_load():
    async def run():
        inactive_names = {
            tool.name for tool in await root_skill_toolset.get_tools(None)
        }
        assert inactive_names.isdisjoint(NAVIGATION_TOOL_NAMES)

        session_service = InMemorySessionService()
        session = await session_service.create_session(
            app_name="test",
            user_id="user",
            session_id="session",
            state={f"_adk_activated_skill_{root_agent.name}": ["navigation"]},
        )
        invocation_context = InvocationContext(
            session_service=session_service,
            session=session,
            invocation_id=new_invocation_context_id(),
            agent=root_agent,
            run_config=RunConfig(),
        )
        active_names = {
            tool.name
            for tool in await root_skill_toolset.get_tools(
                ReadonlyContext(invocation_context)
            )
        }
        assert NAVIGATION_TOOL_NAMES <= active_names

    asyncio.run(run())
    assert root_agent.sub_agents == []
    assert root_skill_toolset.skills == []
