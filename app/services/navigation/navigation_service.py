"""Provider, mapper, routing algorithm, and response rendering for navigation."""

from __future__ import annotations

import heapq
import json
from pathlib import Path
from uuid import uuid4

from app.core.schemas.graph_navigation import (
    FindRouteRequest,
    LocationRef,
    NavigationGraph,
    NavigationSessionState,
    ResolvedLocation,
    RouteCandidate,
    RouteResult,
    RouteStep,
)


class NavigationService:
    """Owns graph loading, location resolution, and constrained Dijkstra routing."""

    def __init__(self, graph_path: Path | None = None) -> None:
        self._graph_path = graph_path or Path(__file__).resolve().parents[2] / "data" / "urban_navigation_graph.json"
        self._graph: NavigationGraph | None = None

    @property
    def graph(self) -> NavigationGraph:
        if self._graph is None:
            with self._graph_path.open(encoding="utf-8") as file:
                self._graph = NavigationGraph.model_validate(json.load(file))
        return self._graph

    def resolve(self, location: LocationRef) -> ResolvedLocation | None:
        candidates = [value for value in (location.node_id, location.name, location.road_name) if value]
        for candidate in candidates:
            needle = candidate.casefold()
            for node_id, node in self.graph.nodes.items():
                if node_id.casefold() == needle or node.name.casefold() == needle:
                    return ResolvedLocation(nodeId=node_id, name=node.name, lat=node.lat, lng=node.lng, level=node.level, heading=location.heading)
        return None

    def find(self, request: FindRouteRequest) -> RouteResult:
        start = self.resolve(request.current_position or request.start)
        end = self.resolve(request.end)
        if not start or not end:
            return RouteResult(success=False, errorCode="LOCATION_NOT_FOUND", errorMessage="Không tìm thấy điểm bắt đầu hoặc điểm đến trong bản đồ mô phỏng.")

        queue: list[tuple[float, str, list[tuple[str, object]]]] = [(0.0, start.node_id, [])]
        best = {start.node_id: 0.0}
        while queue:
            cost, node_id, path = heapq.heappop(queue)
            if node_id == end.node_id:
                return self._result(start, end, request, path)
            if cost != best.get(node_id):
                continue
            for edge in self.graph.graph[node_id]:
                if edge.status == "closed" and request.avoid_closed:
                    continue
                permitted = bool(set(edge.access).intersection(request.access))
                if not permitted or (edge.status == "restricted" and not request.allow_restricted):
                    continue
                edge_cost = edge.distance if request.optimization == "shortest_distance" else edge.time
                next_cost = cost + edge_cost
                if next_cost < best.get(edge.to, float("inf")):
                    best[edge.to] = next_cost
                    heapq.heappush(queue, (next_cost, edge.to, [*path, (node_id, edge)]))
        return RouteResult(success=False, errorCode="NO_ROUTE", errorMessage="Không có tuyến đường hợp lệ với phương thức di chuyển đã chọn.")

    def _result(self, start, end, request, path) -> RouteResult:
        steps, warnings = [], []
        previous_heading = None
        for index, (from_node_id, edge) in enumerate(path):
            delta = 0 if previous_heading is None else (edge.heading - previous_heading) % 360
            turn = "start" if previous_heading is None else ("straight" if delta <= 30 or delta >= 330 else "right" if delta < 180 else "left")
            if edge.status == "restricted": warnings.append(f"Đường hạn chế: {edge.road_name}")
            if edge.toll: warnings.append(f"Có phí: {edge.road_name}")
            steps.append(RouteStep(index=index, fromNodeId=from_node_id, toNodeId=edge.to, edgeId=edge.id, roadName=edge.road_name, distance=edge.distance, time=edge.time, heading=edge.heading, turn=turn, instruction=edge.instructions.default, landmark=edge.instructions.landmark, restrictions=edge.restrictions))
            previous_heading = edge.heading
        route = RouteCandidate(routeId=str(uuid4()), optimization=request.optimization, start=start, end=end, totalDistance=sum(step.distance for step in steps), totalTime=sum(step.time for step in steps), steps=steps, warnings=warnings)
        return RouteResult(success=True, route=route, warnings=warnings)

    def render(self, state: NavigationSessionState, *, dynamic_prompt: str, scenario: str) -> dict:
        if state.route is None:
            return {"success": False, "message": "Tôi chưa có tuyến đường đang hoạt động.", "errorCode": "NO_ACTIVE_ROUTE"}
        if state.current_step_index >= len(state.steps):
            return {"success": True, "message": "Bạn đã đến điểm đến.", "currentStepIndex": state.current_step_index, "warnings": state.warnings}
        step = state.steps[state.current_step_index]
        prefix = "Đã cập nhật tuyến. " if scenario != "continue_guidance" else ""
        warning = f" Lưu ý: {state.warnings[0]}." if state.warnings else ""
        return {"success": True, "message": f"{prefix}{step.instruction} ({step.road_name}, {step.distance:.0f} m).{warning}", "nextStep": step.model_dump(by_alias=True), "currentStepIndex": state.current_step_index, "warnings": state.warnings}
