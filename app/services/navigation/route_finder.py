"""Find primary and recovery routes on a validated navigation graph."""

import heapq
from itertools import count
from uuid import uuid4

from app.core.enums import AccessMode, EdgeStatus, OptimizationMode
from app.core.schemas.navigation import GraphEdge, NavigationGraph

Path = list[tuple[str, GraphEdge]]


class RouteFinder:
    """Run constrained Dijkstra searches without resolving user text."""

    def __init__(self, graph: NavigationGraph) -> None:
        self._graph = graph

    def find_route(
        self,
        start_node_id: str,
        end_node_id: str,
        access: AccessMode,
        optimization: OptimizationMode,
    ) -> dict[str, object] | None:
        """Find a route between two resolved graph nodes."""

        self._require_nodes([start_node_id, end_node_id])
        result = self._shortest_path(
            start_node_id,
            {end_node_id},
            access,
            optimization,
        )
        if result is None:
            return None

        reached_node_id, path = result
        return self._build_route(
            start_node_id,
            reached_node_id,
            optimization,
            path,
        )

    def find_recovery_route(
        self,
        current_node_id: str,
        route_node_ids: list[str],
        current_step_index: int,
        access: AccessMode,
        optimization: OptimizationMode,
    ) -> tuple[dict[str, object], int] | None:
        """Find the cheapest route back to a remaining node of the main route."""

        if not route_node_ids:
            raise ValueError("route_node_ids must not be empty")
        if current_step_index < 0 or current_step_index >= len(route_node_ids):
            raise ValueError("current_step_index is outside route_node_ids")

        self._require_nodes([current_node_id, *route_node_ids])
        remaining_indexes: dict[str, int] = {}
        for index in range(current_step_index, len(route_node_ids)):
            remaining_indexes.setdefault(route_node_ids[index], index)

        result = self._shortest_path(
            current_node_id,
            set(remaining_indexes),
            access,
            optimization,
        )
        if result is None:
            return None

        reached_node_id, path = result
        route = self._build_route(
            current_node_id,
            reached_node_id,
            optimization,
            path,
        )
        return route, remaining_indexes[reached_node_id]

    def _shortest_path(
        self,
        start_node_id: str,
        target_node_ids: set[str],
        access: AccessMode,
        optimization: OptimizationMode,
    ) -> tuple[str, Path] | None:
        sequence = count()
        queue: list[tuple[float, int, str, Path]] = [
            (0.0, next(sequence), start_node_id, [])
        ]
        best_cost = {start_node_id: 0.0}

        while queue:
            cost, _, node_id, path = heapq.heappop(queue)
            if cost != best_cost.get(node_id):
                continue
            if node_id in target_node_ids:
                return node_id, path

            for edge in self._graph.graph[node_id]:
                if edge.status != EdgeStatus.OPEN or access not in edge.access:
                    continue
                edge_cost = (
                    edge.distance
                    if optimization == OptimizationMode.SHORTEST_DISTANCE
                    else edge.time
                )
                next_cost = cost + edge_cost
                if next_cost >= best_cost.get(edge.to, float("inf")):
                    continue
                best_cost[edge.to] = next_cost
                heapq.heappush(
                    queue,
                    (
                        next_cost,
                        next(sequence),
                        edge.to,
                        [*path, (node_id, edge)],
                    ),
                )
        return None

    def _build_route(
        self,
        start_node_id: str,
        end_node_id: str,
        optimization: OptimizationMode,
        path: Path,
    ) -> dict[str, object]:
        steps: list[dict[str, object]] = []
        warnings: list[str] = []
        previous_heading: int | None = None

        for index, (from_node_id, edge) in enumerate(path):
            if edge.toll:
                warning = f"Có phí: {edge.road_name}"
                if warning not in warnings:
                    warnings.append(warning)

            steps.append(
                {
                    "index": index,
                    "fromNode": self._node_payload(from_node_id),
                    "toNode": self._node_payload(edge.to),
                    "edge": edge.model_dump(mode="json", by_alias=True),
                    "turn": self._turn(previous_heading, edge.heading),
                }
            )
            previous_heading = edge.heading

        return {
            "routeId": str(uuid4()),
            "startNodeId": start_node_id,
            "endNodeId": end_node_id,
            "optimization": optimization.value,
            "totalDistance": sum(edge.distance for _, edge in path),
            "totalTime": sum(edge.time for _, edge in path),
            "steps": steps,
            "warnings": warnings,
        }

    def _node_payload(self, node_id: str) -> dict[str, object]:
        return {
            "id": node_id,
            **self._graph.nodes[node_id].model_dump(mode="json", by_alias=True),
        }

    def _require_nodes(self, node_ids: list[str]) -> None:
        missing = sorted(set(node_ids) - set(self._graph.nodes))
        if missing:
            raise ValueError(f"Unknown graph nodes: {', '.join(missing)}")

    @staticmethod
    def _turn(previous_heading: int | None, heading: int) -> str:
        if previous_heading is None:
            return "start"
        delta = (heading - previous_heading) % 360
        if delta <= 30 or delta >= 330:
            return "straight"
        if delta < 180:
            return "right"
        return "left"


__all__ = ["RouteFinder"]
