"""Load the graph and provide reusable navigation service dependencies."""

import json
from pathlib import Path

from app.core.schemas.navigation import NavigationGraph
from app.services.navigation.location_resolver import LocationResolver
from app.services.navigation.route_finder import RouteFinder


class NavigationService:
    """Own the validated graph shared by location and route services."""

    def __init__(self, graph_path: Path | None = None) -> None:
        self._graph_path = graph_path or (
            Path(__file__).resolve().parents[2]
            / "data"
            / "urban_navigation_graph.json"
        )
        self._graph: NavigationGraph | None = None
        self._location_resolver: LocationResolver | None = None
        self._route_finder: RouteFinder | None = None

    @property
    def graph(self) -> NavigationGraph:
        """Load and validate the graph once."""

        if self._graph is None:
            with self._graph_path.open(encoding="utf-8") as graph_file:
                self._graph = NavigationGraph.model_validate(json.load(graph_file))
        return self._graph

    @property
    def locations(self) -> LocationResolver:
        """Return the location resolver bound to the shared graph."""

        if self._location_resolver is None:
            self._location_resolver = LocationResolver(self.graph)
        return self._location_resolver

    @property
    def routes(self) -> RouteFinder:
        """Return the route finder bound to the shared graph."""

        if self._route_finder is None:
            self._route_finder = RouteFinder(self.graph)
        return self._route_finder


__all__ = ["NavigationService"]
