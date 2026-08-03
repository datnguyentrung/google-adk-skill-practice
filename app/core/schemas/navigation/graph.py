"""Schemas that validate the urban navigation graph dataset."""

from typing import Literal, TypeAlias

from pydantic import Field, model_validator

from app.core.enums import AccessMode, EdgeStatus, NodeType, RoadType
from app.core.schemas.navigation.base import SchemaBaseModel

NodeId: TypeAlias = str
EdgeId: TypeAlias = str
Level: TypeAlias = Literal[-1, 0, 1]


class GraphMetadata(SchemaBaseModel):
    """Metadata stored at the root of the graph file."""

    name: str
    description: str
    city: str
    is_simulation: bool = Field(alias="isSimulation")
    distance_unit: str = Field(alias="distanceUnit")
    time_unit: str = Field(alias="timeUnit")
    coordinate_system: str = Field(alias="coordinateSystem")


class GraphNode(SchemaBaseModel):
    """A named point that can participate in a route."""

    name: str
    type: NodeType
    lat: float
    lng: float
    level: Level
    description: str


class EdgeInstructions(SchemaBaseModel):
    """Navigation text attached to a graph edge."""

    default: str
    landmark: str


class EdgeRestrictions(SchemaBaseModel):
    """Road restrictions recorded by the graph dataset."""

    no_left_turn: bool = Field(alias="noLeftTurn", default=False)
    no_right_turn: bool = Field(alias="noRightTurn", default=False)
    no_u_turn: bool = Field(alias="noUTurn", default=False)
    straight_only: bool = Field(alias="straightOnly", default=False)
    requires_permission: bool = Field(alias="requiresPermission", default=False)


class GraphEdge(SchemaBaseModel):
    """A directed connection between two graph nodes."""

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
    """The complete validated navigation graph."""

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


__all__ = [
    "EdgeInstructions",
    "EdgeRestrictions",
    "GraphAdjacencyList",
    "GraphEdge",
    "GraphMetadata",
    "GraphNode",
    "Level",
    "NavigationGraph",
    "NodeId",
    "NodeMap",
]
