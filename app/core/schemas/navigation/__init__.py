"""Minimal schemas for the navigation graph and session state."""

from app.core.schemas.base import SchemaBaseModel
from app.core.schemas.navigation.graph import (
    EdgeId,
    EdgeInstructions,
    EdgeRestrictions,
    GraphAdjacencyList,
    GraphEdge,
    GraphMetadata,
    GraphNode,
    Level,
    NavigationGraph,
    NodeId,
    NodeMap,
)
from app.core.schemas.navigation.state import (
    NAVIGATION_STATE_KEY,
    NavigationState,
)

__all__ = [
    "EdgeId",
    "EdgeInstructions",
    "EdgeRestrictions",
    "GraphAdjacencyList",
    "GraphEdge",
    "GraphMetadata",
    "GraphNode",
    "Level",
    "NAVIGATION_STATE_KEY",
    "NavigationGraph",
    "NavigationState",
    "NodeId",
    "NodeMap",
    "SchemaBaseModel",
]
