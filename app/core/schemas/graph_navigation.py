"""Reusable schemas for the simulated urban navigation graph.

The models in this module are based on:
    app/data/urban_navigation_graph.json

They are intentionally reusable across:
- graph JSON loading and validation
- shortest-path / fastest-route tools
- route response rendering tools
- navigation skill state
"""

# from __future__ import annotations

from app.core.enums import NodeType, RoadType, EdgeStatus, AccessMode, OptimizationMode, TurnDirection, NavigationScenario

from typing import Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, model_validator


NodeId: TypeAlias = str
EdgeId: TypeAlias = str
RouteId: TypeAlias = str
Level: TypeAlias = Literal[-1, 0, 1]

class SchemaBaseModel(BaseModel):
    """Base model for graph/navigation schemas."""

    model_config = ConfigDict(
        populate_by_name=True,
        extra="forbid",
        str_strip_whitespace=True,
    )


class GraphMetadata(SchemaBaseModel):
    name: str
    description: str
    city: str
    is_simulation: bool = Field(alias="isSimulation")
    distance_unit: str = Field(alias="distanceUnit")
    time_unit: str = Field(alias="timeUnit")
    coordinate_system: str = Field(alias="coordinateSystem")


class GraphNode(SchemaBaseModel):
    name: str
    type: NodeType
    lat: float
    lng: float
    level: Level
    description: str

class EdgeInstructions(SchemaBaseModel):
    default: str
    landmark: str


class EdgeRestrictions(SchemaBaseModel):
    no_left_turn: bool = Field(alias="noLeftTurn", default=False)
    no_right_turn: bool = Field(alias="noRightTurn", default=False)
    no_u_turn: bool = Field(alias="noUTurn", default=False)
    straight_only: bool = Field(alias="straightOnly", default=False)
    requires_permission: bool = Field(alias="requiresPermission", default=False)


class GraphEdge(SchemaBaseModel):
    id: EdgeId
    to: NodeId
    road_name: str = Field(alias="roadName")
    distance: float = Field(gt=0)
    time: float = Field(gt=0)
    heading: int = Field(ge=0, le=359)
    one_way: bool = Field(alias="oneWay")
    road_type: RoadType = Field(alias="roadType")
    max_speed: float = Field(alias="maxSpeed", gt=0)
    status: EdgeStatus
    access: list[AccessMode]
    toll: bool
    level: Level
    instructions: EdgeInstructions
    restrictions: EdgeRestrictions


GraphAdjacencyList: TypeAlias = dict[NodeId, list[GraphEdge]]
NodeMap: TypeAlias = dict[NodeId, GraphNode]


class NavigationGraph(SchemaBaseModel):
    metadata: GraphMetadata
    nodes: NodeMap
    graph: GraphAdjacencyList

    @model_validator(mode="after")
    def validate_graph_integrity(self) -> "NavigationGraph":
        node_ids = set(self.nodes.keys())
        edge_ids: set[str] = set()
        missing_graph_keys = node_ids - set(self.graph.keys())

        if missing_graph_keys:
            raise ValueError(
                "Every node must have a graph adjacency key. Missing keys: "
                f"{sorted(missing_graph_keys)}"
            )

        unknown_graph_keys = set(self.graph.keys()) - node_ids
        if unknown_graph_keys:
            raise ValueError(
                "Graph contains adjacency keys that are not declared nodes: "
                f"{sorted(unknown_graph_keys)}"
            )

        for from_node_id, edges in self.graph.items():
            for edge in edges:
                if edge.id in edge_ids:
                    raise ValueError(f"Duplicate edge id: {edge.id}")
                edge_ids.add(edge.id)

                if edge.to not in node_ids:
                    raise ValueError(
                        f"Edge {edge.id} from {from_node_id} points to unknown node {edge.to}"
                    )

        return self

class LocationRef(SchemaBaseModel):
    """A flexible location reference supplied by the user or system."""

    node_id: NodeId | None = Field(default=None, alias="nodeId")
    name: str | None = None
    road_name: str | None = Field(default=None, alias="roadName")
    lat: float | None = None
    lng: float | None = None
    heading: int | None = Field(default=None, ge=0, le=359)
    raw_text: str | None = Field(default=None, alias="rawText")


class ResolvedLocation(SchemaBaseModel):
    """A location after it has been resolved to a graph node."""

    node_id: NodeId = Field(alias="nodeId")
    name: str
    lat: float
    lng: float
    level: Level
    heading: int | None = Field(default=None, ge=0, le=359)


class RouteCost(SchemaBaseModel):
    distance: float = Field(gt=0)
    time: float = Field(gt=0)

class RouteStep(SchemaBaseModel):
    index: int = Field(ge=0)
    from_node_id: NodeId = Field(alias="fromNodeId")
    to_node_id: NodeId = Field(alias="toNodeId")
    edge_id: EdgeId = Field(alias="edgeId")
    road_name: str = Field(alias="roadName")
    distance: float = Field(gt=0)
    time: float = Field(gt=0)
    heading: int = Field(ge=0, le=359)
    turn: TurnDirection = "unknown"
    instruction: str
    landmark: str | None = None
    restrictions: EdgeRestrictions | None = None


class RouteCandidate(SchemaBaseModel):
    route_id: str = Field(alias="routeId")
    optimization: OptimizationMode
    start: ResolvedLocation
    end: ResolvedLocation
    total_distance: float = Field(alias="totalDistance", ge=0)
    total_time: float = Field(alias="totalTime", ge=0)
    steps: list[RouteStep]
    warnings: list[str] = Field(default_factory=list)
    blocked_edge_ids: list[EdgeId] = Field(default_factory=list, alias="blockedEdgeIds")
    restricted_edge_ids: list[EdgeId] = Field(default_factory=list, alias="restrictedEdgeIds")
    toll_edge_ids: list[EdgeId] = Field(default_factory=list, alias="tollEdgeIds")


class OffRouteInfo(SchemaBaseModel):
    is_off_route: bool = Field(alias="isOffRoute")
    nearest_node_id: NodeId | None = Field(default=None, alias="nearestNodeId")
    expected_node_id: NodeId | None = Field(default=None, alias="expectedNodeId")
    distance_from_route: float | None = Field(default=None, ge=0, alias="distanceFromRoute")
    reason: str | None = None


class NavigationState(SchemaBaseModel):
    route_id: str | None = Field(default=None, alias="routeId")
    current_step_index: int | None = Field(default=None, ge=0, alias="currentStepIndex")
    current_position: LocationRef | None = Field(default=None, alias="currentPosition")
    destination: LocationRef | None = None
    access: list[AccessMode] = Field(default_factory=list)


class RouteResult(SchemaBaseModel):
    """Tool result for a route search; it never contains session mutations."""

    success: bool
    route: RouteCandidate | None = None
    off_route: OffRouteInfo | None = Field(default=None, alias="offRoute")
    warnings: list[str] = Field(default_factory=list)
    error_code: str | None = Field(default=None, alias="errorCode")
    error_message: str | None = Field(default=None, alias="errorMessage")


class NavigationSessionState(SchemaBaseModel):
    route: RouteCandidate | None = None
    steps: list[RouteStep] = Field(default_factory=list)
    current_step_index: int = Field(default=0, ge=0, alias="currentStepIndex")
    current_position: LocationRef | None = Field(default=None, alias="currentPosition")
    destination: LocationRef | None = None
    access: list[AccessMode] = Field(default_factory=list)
    optimization: OptimizationMode = "fastest_time"
    warnings: list[str] = Field(default_factory=list)
    reroute_required: bool = Field(default=False, alias="rerouteRequired")
    reroute_reason: str | None = Field(default=None, alias="rerouteReason")


class NavigationRequest(SchemaBaseModel):
    """Normalized navigation intent extracted from the latest user message."""

    user_message: str = Field(alias="userMessage")
    start: LocationRef | None = None
    destination: LocationRef | None = None
    current_position: LocationRef | None = Field(default=None, alias="currentPosition")
    access: list[AccessMode] | None = None
    optimization: OptimizationMode | None = None
    is_wrong_or_lost: bool = Field(default=False, alias="isWrongOrLost")
    asks_next_step: bool = Field(default=False, alias="asksNextStep")

class FindRouteRequest(SchemaBaseModel):
    start: LocationRef
    end: LocationRef
    optimization: OptimizationMode = "fastest_time"
    access: list[AccessMode] = Field(default_factory=lambda: ["car", "motorbike"])
    current_position: LocationRef | None = Field(default=None, alias="currentPosition")
    current_heading: int | None = Field(default=None, ge=0, le=359, alias="currentHeading")
    previous_route_id: str | None = Field(default=None, alias="previousRouteId")
    previous_step_index: int | None = Field(default=None, ge=0, alias="previousStepIndex")
    prefer_continue_current_route: bool = Field(default=False, alias="preferContinueCurrentRoute")
    max_alternatives: int = Field(default=2, ge=0, le=5, alias="maxAlternatives")
    avoid_closed: bool = Field(default=True, alias="avoidClosed")
    allow_restricted: bool = Field(default=False, alias="allowRestricted")


class FindRouteResponse(SchemaBaseModel):
    success: bool
    dynamic_prompt: str | None = Field(default=None, alias="dynamicPrompt")
    route: RouteCandidate | None = None
    alternatives: list[RouteCandidate] = Field(default_factory=list)
    off_route: OffRouteInfo | None = Field(default=None, alias="offRoute")
    warnings: list[str] = Field(default_factory=list)
    error_code: str | None = Field(default=None, alias="errorCode")
    error_message: str | None = Field(default=None, alias="errorMessage")

    @model_validator(mode="after")
    def validate_dynamic_prompt_when_success(self) -> "FindRouteResponse":
        if self.success and not self.dynamic_prompt:
            raise ValueError("dynamicPrompt is required when route search succeeds")
        return self


class RenderNavigationRequest(SchemaBaseModel):
    scenario: NavigationScenario
    dynamic_prompt: str = Field(alias="dynamicPrompt")
    route: RouteCandidate | None = None
    alternatives: list[RouteCandidate] = Field(default_factory=list)
    navigation_state: NavigationState | None = Field(default=None, alias="navigationState")
    user_message: str | None = Field(default=None, alias="userMessage")
    locale: str = "vi-VN"

class RenderNavigationResponse(SchemaBaseModel):
    success: bool
    message: str
    next_step: RouteStep | None = Field(default=None, alias="nextStep")
    route_id: str | None = Field(default=None, alias="routeId")
    current_step_index: int | None = Field(default=None, ge=0, alias="currentStepIndex")
    warnings: list[str] = Field(default_factory=list)
    error_code: str | None = Field(default=None, alias="errorCode")
    error_message: str | None = Field(default=None, alias="errorMessage")


class GraphLoadResult(SchemaBaseModel):
    success: bool
    graph: NavigationGraph | None = None
    path: str | None = None
    node_count: int = Field(default=0, ge=0, alias="nodeCount")
    edge_count: int = Field(default=0, ge=0, alias="edgeCount")
    error_message: str | None = Field(default=None, alias="errorMessage")

def count_edges(graph: GraphAdjacencyList) -> int:
    """Return the total number of directed edges in an adjacency list."""

    return sum(len(edges) for edges in graph.values())


def build_graph_load_result(graph: NavigationGraph, path: str | None = None) -> GraphLoadResult:
    """Create a reusable successful load result for a validated graph."""

    return GraphLoadResult(
        success=True,
        graph=graph,
        path=path,
        nodeCount=len(graph.nodes),
        edgeCount=count_edges(graph.graph),
    )


__all__ = [
    "EdgeId",
    "EdgeInstructions",
    "EdgeRestrictions",
    "FindRouteRequest",
    "FindRouteResponse",
    "GraphAdjacencyList",
    "GraphEdge",
    "GraphLoadResult",
    "GraphMetadata",
    "GraphNode",
    "Level",
    "LocationRef",
    "NavigationGraph",
    "NavigationState",
    "NodeId",
    "NodeMap",
    "RenderNavigationRequest",
    "RenderNavigationResponse",
    "ResolvedLocation",
    "RouteCandidate",
    "RouteCost",
    "RouteStep",
    "SchemaBaseModel",
    "build_graph_load_result",
    "count_edges",
]

# Resolve postponed annotations for Pydantic v2 when this module is imported directly.
for _model in (
    GraphMetadata,
    GraphNode,
    EdgeInstructions,
    EdgeRestrictions,
    GraphEdge,
    NavigationGraph,
    LocationRef,
    ResolvedLocation,
    RouteCost,
    RouteStep,
    RouteCandidate,
    OffRouteInfo,
    NavigationState,
    FindRouteRequest,
    FindRouteResponse,
    RenderNavigationRequest,
    RenderNavigationResponse,
    GraphLoadResult,
):
    _model.model_rebuild(_types_namespace=globals())

del _model
