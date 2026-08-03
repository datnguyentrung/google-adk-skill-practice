"""Search graph nodes by node names, identifiers, and connected roads."""

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Literal

from app.core.schemas.navigation import NavigationGraph

LocationTargetType = Literal["auto", "node", "road"]


class LocationResolver:
    """Resolve user location text to ranked graph-node candidates."""

    def __init__(self, graph: NavigationGraph) -> None:
        self._graph = graph

    def search(
        self,
        query: str,
        target_type: LocationTargetType,
        min_similarity: float,
        limit: int = 5,
    ) -> list[dict[str, object]]:
        """Return unique node candidates scoring above the requested threshold."""

        normalized_query = self._normalize(query)
        normalized_road_query = self._normalize_road(query)
        candidates: list[dict[str, object]] = []

        for node_id, node in self._graph.nodes.items():
            best_score = 0.0
            match_type: Literal["node", "road"] = "node"
            matched_road: str | None = None

            if target_type in {"auto", "node"}:
                best_score = max(
                    self._similarity(normalized_query, self._normalize(node_id)),
                    self._similarity(normalized_query, self._normalize(node.name)),
                )

            if target_type in {"auto", "road"} and normalized_road_query:
                for road_name in self._connected_road_names(node_id):
                    road_score = self._similarity(
                        normalized_road_query,
                        self._normalize_road(road_name),
                    )
                    if road_score > best_score:
                        best_score = road_score
                        match_type = "road"
                        matched_road = road_name

            if best_score <= min_similarity:
                continue

            candidate: dict[str, object] = {
                "nodeId": node_id,
                "name": node.name,
                "targetType": match_type,
                "description": node.description,
                "similarity": best_score,
            }
            if matched_road is not None:
                candidate["roadName"] = matched_road
            candidates.append(candidate)

        candidates.sort(
            key=lambda candidate: (
                -float(candidate["similarity"]),
                str(candidate["nodeId"]),
            )
        )
        return candidates[:limit]

    def _connected_road_names(self, node_id: str) -> list[str]:
        road_names = {
            edge.road_name for edge in self._graph.graph.get(node_id, [])
        }
        for edges in self._graph.graph.values():
            road_names.update(
                edge.road_name for edge in edges if edge.to == node_id
            )
        return sorted(road_names)

    @classmethod
    def _normalize_road(cls, value: str) -> str:
        normalized = cls._normalize(value)
        return re.sub(
            r"^(?:tuyen duong|duong|pho|ngo|hem|tuyen)\s+",
            "",
            normalized,
        ).strip()

    @staticmethod
    def _normalize(value: str) -> str:
        decomposed = unicodedata.normalize("NFD", value)
        without_marks = "".join(
            character
            for character in decomposed
            if unicodedata.category(character) != "Mn"
        )
        return " ".join(
            without_marks.replace("đ", "d").replace("Đ", "D").casefold().split()
        )

    @staticmethod
    def _similarity(needle: str, haystack: str) -> float:
        if not needle or not haystack:
            return 0.0
        if needle == haystack:
            return 1.0
        if needle in haystack or haystack in needle:
            return 0.92

        needle_tokens = set(needle.split())
        haystack_tokens = set(haystack.split())
        token_score = (
            len(needle_tokens & haystack_tokens) / len(needle_tokens)
            if needle_tokens
            else 0.0
        )
        sequence_score = SequenceMatcher(None, needle, haystack).ratio()
        return max(token_score * 0.9, sequence_score)


__all__ = ["LocationResolver", "LocationTargetType"]
