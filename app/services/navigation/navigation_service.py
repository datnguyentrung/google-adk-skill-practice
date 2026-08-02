"""Provider, mapper, routing algorithm, and response rendering for navigation."""



import heapq
import json
import re
import unicodedata
from dataclasses import dataclass
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


@dataclass(frozen=True)
class _Resolution:
    location: ResolvedLocation | None
    error_code: str | None = None
    error_message: str | None = None


class NavigationService:
    """Owns graph loading, location resolution, and constrained Dijkstra routing."""

    def __init__(self, graph_path: Path | None = None) -> None:
        self._graph_path = (
            graph_path
            or Path(__file__).resolve().parents[2]
            / "data"
            / "urban_navigation_graph.json"
        )
        self._graph: NavigationGraph | None = None

    @property
    def graph(self) -> NavigationGraph:
        if self._graph is None:
            with self._graph_path.open(encoding="utf-8") as file:
                self._graph = NavigationGraph.model_validate(json.load(file))
        return self._graph

    def resolve(self, location: LocationRef) -> ResolvedLocation | None:
        return self._resolve(location, role="generic").location

    def find(self, request: FindRouteRequest) -> RouteResult:
        start_resolution = self._resolve(
            request.current_position or request.start, role="start"
        )
        end_resolution = self._resolve(request.end, role="destination")
        if not start_resolution.location or not end_resolution.location:
            error = (
                start_resolution
                if not start_resolution.location
                else end_resolution
            )
            return RouteResult(
                success=False,
                errorCode=error.error_code or "LOCATION_NOT_FOUND",
                errorMessage=error.error_message
                or "Không tìm thấy điểm bắt đầu hoặc điểm đến trong bản đồ mô phỏng.",
            )

        start = start_resolution.location
        end = end_resolution.location
        queue: list[tuple[float, str, list[tuple[str, object]]]] = [
            (0.0, start.node_id, [])
        ]
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
                if not permitted or (
                    edge.status == "restricted" and not request.allow_restricted
                ):
                    continue
                edge_cost = (
                    edge.distance
                    if request.optimization == "shortest_distance"
                    else edge.time
                )
                next_cost = cost + edge_cost
                if next_cost < best.get(edge.to, float("inf")):
                    best[edge.to] = next_cost
                    heapq.heappush(
                        queue, (next_cost, edge.to, [*path, (node_id, edge)])
                    )
        return RouteResult(
            success=False,
            errorCode="NO_ROUTE",
            errorMessage="Không có tuyến đường hợp lệ với phương thức di chuyển đã chọn.",
        )

    def _resolve(self, location: LocationRef, *, role: str) -> _Resolution:
        candidates = [
            value
            for value in (
                location.node_id,
                location.name,
                location.road_name,
                location.raw_text,
            )
            if value
        ]

        if location.target_type in ("auto", "node"):
            resolved = self._resolve_node(location, candidates)
            if resolved:
                return _Resolution(location=resolved)

        if location.target_type in ("auto", "road"):
            road_resolution = self._resolve_road(location, candidates, role=role)
            if road_resolution.location or road_resolution.error_code:
                return road_resolution

        label = self._location_label(location)
        target = "điểm/đường" if location.target_type == "auto" else location.target_type
        return _Resolution(
            location=None,
            error_code="LOCATION_NOT_FOUND",
            error_message=f"Không tìm thấy {target} '{label}' trong bản đồ mô phỏng.",
        )

    def _resolve_node(
        self, location: LocationRef, candidates: list[str]
    ) -> ResolvedLocation | None:
        for candidate in candidates:
            needle = self._normalize(candidate)
            for node_id, node in self.graph.nodes.items():
                if needle in {self._normalize(node_id), self._normalize(node.name)}:
                    return ResolvedLocation(
                        nodeId=node_id,
                        name=node.name,
                        lat=node.lat,
                        lng=node.lng,
                        level=node.level,
                        heading=location.heading,
                    )
        return None

    def _resolve_road(
        self, location: LocationRef, candidates: list[str], *, role: str
    ) -> _Resolution:
        matches: dict[str, list[tuple[str, object]]] = {}
        for candidate in candidates:
            needle = self._normalize_road_query(candidate)
            if not needle:
                continue
            for from_node_id, edges in self.graph.graph.items():
                for edge in edges:
                    road_key = self._normalize(edge.road_name)
                    if road_key == needle or needle in road_key:
                        matches.setdefault(edge.road_name, []).append(
                            (from_node_id, edge)
                        )

        if not matches:
            return _Resolution(location=None)

        if len(matches) > 1:
            options = ", ".join(sorted(matches))
            return _Resolution(
                location=None,
                error_code="AMBIGUOUS_LOCATION",
                error_message=(
                    f"Vị trí '{self._location_label(location)}' khớp nhiều đường: "
                    f"{options}. Vui lòng nói rõ đường/ngõ cần đi."
                ),
            )

        road_name, road_edges = next(iter(matches.items()))
        from_node_id, edge = road_edges[0]
        node_id = edge.to if role == "destination" else from_node_id
        node = self.graph.nodes[node_id]
        return _Resolution(
            location=ResolvedLocation(
                nodeId=node_id,
                name=f"{node.name} ({road_name})",
                lat=node.lat,
                lng=node.lng,
                level=node.level,
                heading=location.heading,
            )
        )

    def _result(self, start, end, request, path) -> RouteResult:
        steps, warnings = [], []
        previous_heading = None
        for index, (from_node_id, edge) in enumerate(path):
            delta = (
                0
                if previous_heading is None
                else (edge.heading - previous_heading) % 360
            )
            turn = (
                "start"
                if previous_heading is None
                else (
                    "straight"
                    if delta <= 30 or delta >= 330
                    else "right"
                    if delta < 180
                    else "left"
                )
            )
            if edge.status == "restricted":
                warnings.append(f"Đường hạn chế: {edge.road_name}")
            if edge.toll:
                warnings.append(f"Có phí: {edge.road_name}")
            steps.append(
                RouteStep(
                    index=index,
                    fromNodeId=from_node_id,
                    toNodeId=edge.to,
                    edgeId=edge.id,
                    roadName=edge.road_name,
                    distance=edge.distance,
                    time=edge.time,
                    heading=edge.heading,
                    turn=turn,
                    instruction=edge.instructions.default,
                    landmark=edge.instructions.landmark,
                    restrictions=edge.restrictions,
                )
            )
            previous_heading = edge.heading
        route = RouteCandidate(
            routeId=str(uuid4()),
            optimization=request.optimization,
            start=start,
            end=end,
            totalDistance=sum(step.distance for step in steps),
            totalTime=sum(step.time for step in steps),
            steps=steps,
            warnings=warnings,
        )
        return RouteResult(success=True, route=route, warnings=warnings)

    def render(
        self, state: NavigationSessionState, *, dynamic_prompt: str, scenario: str
    ) -> dict:
        if state.route is None:
            return {
                "success": False,
                "message": "Tôi chưa có tuyến đường đang hoạt động.",
                "errorCode": "NO_ACTIVE_ROUTE",
            }
        if state.current_step_index >= len(state.steps):
            return {
                "success": True,
                "message": "Bạn đã đến điểm đến.",
                "currentStepIndex": state.current_step_index,
                "warnings": state.warnings,
            }
        step = state.steps[state.current_step_index]
        prefix = "Đã cập nhật tuyến. " if scenario != "continue_guidance" else ""
        warning = f" Lưu ý: {state.warnings[0]}." if state.warnings else ""
        return {
            "success": True,
            "message": f"{prefix}{step.instruction} ({step.road_name}, {step.distance:.0f} m).{warning}",
            "nextStep": step.model_dump(by_alias=True),
            "currentStepIndex": state.current_step_index,
            "warnings": state.warnings,
        }

    @staticmethod
    def _location_label(location: LocationRef) -> str:
        return (
            location.raw_text
            or location.name
            or location.road_name
            or location.node_id
            or "không rõ"
        )

    @classmethod
    def _normalize_road_query(cls, value: str) -> str:
        normalized = cls._normalize(value)
        return re.sub(
            r"^(tuyen duong|duong|pho|ngo|hem|tuyen)\s+",
            "",
            normalized,
        ).strip()

    @staticmethod
    def _normalize(value: str) -> str:
        decomposed = unicodedata.normalize("NFD", value)
        without_marks = "".join(
            char for char in decomposed if unicodedata.category(char) != "Mn"
        )
        return " ".join(
            without_marks.replace("đ", "d").replace("Đ", "D").casefold().split()
        )
