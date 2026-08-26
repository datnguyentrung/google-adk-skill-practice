import re
from typing import Any

from app.core.schemas.ingestion.graph_patch import GraphPatchFragment
from app.services.ingestion.source_grounding import SourceGroundingValidator

CRITICAL_NON_SKIPPABLE_PROPERTIES = {
    "pskg:productCode",
    "pskg:bankingProductStatus",
    "pskg:bankingProductEffectiveFrom",
}
RULE_TYPE_PROPERTY = "pskg:ruleType"


def failed_chunk_indexes(
    response: dict[str, Any],
    fragment: GraphPatchFragment,
    batch_payload: dict[str, Any],
) -> list[int]:
    indexes = {int(item) for item in response.get("affectedChunkIndexes", [])}
    for issue in response.get("errors", []):
        location = str(issue.get("location", ""))
        match = re.search(r"coverage\.(\d+)", location)
        if match:
            indexes.add(int(match.group(1)))

        property_name = issue.get("propertyName")
        node_temp_id = issue.get("nodeTempId")
        if property_name:
            indexes.update(
                _property_evidence_chunks(fragment, property_name, node_temp_id)
            )
        edge_name = issue.get("edgeName")
        if edge_name:
            indexes.update(
                evidence.chunk_index
                for edge in fragment.edges
                if edge.edge_name == edge_name
                for evidence in edge.evidence
            )

    allowed = {int(item) for item in batch_payload.get("chunkIndexes", [])}
    indexes &= allowed
    if not indexes:
        indexes = {
            item.chunk_index
            for item in fragment.coverage
            if item.decision == "MAPPED" and item.chunk_index in allowed
        }
    return sorted(indexes)


def _property_evidence_chunks(
    fragment: GraphPatchFragment,
    property_name: str,
    node_temp_id: str | None,
) -> set[int]:
    indexes: set[int] = set()
    for node in fragment.nodes:
        if node_temp_id and node.temp_id != node_temp_id:
            continue
        for prop in node.properties:
            if prop.property_name == property_name:
                indexes.update(item.chunk_index for item in prop.evidence)
    return indexes


def can_skip_chunks_safely_with_validator(
    fragment: GraphPatchFragment,
    skipped_indexes: set[int],
    validator: SourceGroundingValidator,
) -> bool:
    critical_edges = validator.registry.edge_names_deriving_property(RULE_TYPE_PROPERTY)
    for node in fragment.nodes:
        for prop in node.properties:
            if prop.property_name in CRITICAL_NON_SKIPPABLE_PROPERTIES and any(item.chunk_index in skipped_indexes for item in prop.evidence):
                return False
    for edge in fragment.edges:
        if (
            edge.edge_name in critical_edges
            and edge.evidence
            and all(item.chunk_index in skipped_indexes for item in edge.evidence)
        ):
            return False
    return True


def prune_fragment_for_skips(
    fragment: GraphPatchFragment,
    skipped_indexes: set[int],
    validator: SourceGroundingValidator,
) -> GraphPatchFragment:
    candidate = fragment.model_copy(deep=True)
    kept_nodes = []
    for node in candidate.nodes:
        node.properties = _prune_properties(
            node.properties,
            skipped_indexes,
            validator,
        )
        node.evidence = [
            item
            for item in node.evidence
            if item.chunk_index not in skipped_indexes
        ]
        if not node.evidence and node.properties:
            node.evidence = [node.properties[0].evidence[0].model_copy(deep=True)]
        if node.properties and node.evidence:
            kept_nodes.append(node)
    candidate.nodes = kept_nodes

    node_ids = {node.temp_id for node in candidate.nodes}
    kept_edges = []
    for edge in candidate.edges:
        edge.evidence = [
            item
            for item in edge.evidence
            if item.chunk_index not in skipped_indexes
        ]
        if (
            edge.evidence
            and edge.source_temp_id in node_ids
            and edge.target_temp_id in node_ids
        ):
            kept_edges.append(edge)
    candidate.edges = kept_edges

    for coverage in candidate.coverage:
        if coverage.chunk_index in skipped_indexes:
            coverage.decision = "NOT_RELEVANT"
            coverage.reason = (
                "Skipped after repeated validation failure; "
                "remaining batch facts continue ingestion."
            )
    return candidate


def _prune_properties(
    properties,
    skipped_indexes: set[int],
    validator: SourceGroundingValidator,
):
    kept = []
    for prop in properties:
        remaining = [
            item
            for item in prop.evidence
            if item.chunk_index not in skipped_indexes
        ]
        if not remaining:
            continue

        if isinstance(prop.value, list):
            attribute = validator.registry.get_attribute(prop.property_name)
            if attribute is not None:
                values = [
                    value
                    for value in prop.value
                    if any(
                        validator._value_supported(value, item.text, attribute.range)
                        for item in remaining
                    )
                ]
                if not values:
                    continue
                prop.value = values

        prop.evidence = remaining
        kept.append(prop)
    return kept


__all__ = [
    "can_skip_chunks_safely_with_validator",
    "failed_chunk_indexes",
    "prune_fragment_for_skips",
]
