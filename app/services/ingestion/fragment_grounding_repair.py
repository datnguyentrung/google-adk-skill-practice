from __future__ import annotations

from typing import Any

from app.core.schemas.ingestion.document import DocumentChunk
from app.core.schemas.ingestion.graph_patch import Evidence, GraphPatchFragment
from app.services.ingestion.source_grounding import SourceGroundingValidator


def repair_fragment_grounding(
    fragment: GraphPatchFragment,
    chunks: list[DocumentChunk],
    validator: SourceGroundingValidator,
) -> GraphPatchFragment:
    """Repair deterministic grounding defects without changing semantics."""

    repaired = fragment.model_copy(deep=True)
    chunk_by_index = {chunk.index: chunk for chunk in chunks}

    for node in repaired.nodes:
        kept_properties = []
        for prop in node.properties:
            prop.evidence = _repair_property_evidence(
                prop.property_name,
                prop.value,
                prop.evidence,
                chunk_by_index,
                validator,
            )
            if prop.evidence:
                kept_properties.append(prop)
        node.properties = kept_properties
        node.evidence = _repair_node_evidence(
            node.evidence,
            node.properties,
            chunk_by_index,
            validator,
        )

    node_by_temp_id = {node.temp_id: node for node in repaired.nodes}
    for edge in repaired.edges:
        edge.evidence = _repair_edge_evidence(
            edge,
            edge.evidence,
            chunk_by_index,
            node_by_temp_id,
            validator,
        )
    _reconcile_coverage_with_repaired_facts(repaired)
    return repaired


def _reconcile_coverage_with_repaired_facts(fragment: GraphPatchFragment) -> None:
    fact_chunks: set[int] = set()
    for node in fragment.nodes:
        for prop in node.properties:
            fact_chunks.update(item.chunk_index for item in prop.evidence)
    for edge in fragment.edges:
        fact_chunks.update(item.chunk_index for item in edge.evidence)

    for coverage in fragment.coverage:
        if coverage.decision == "MAPPED" and coverage.chunk_index not in fact_chunks:
            coverage.decision = "FAILED"
            coverage.reason = (
                "No grounded property or edge fact remained after deterministic "
                "evidence repair."
            )


def _repair_property_evidence(
    property_name: str,
    value: Any,
    evidence: list[Evidence],
    chunk_by_index: dict[int, DocumentChunk],
    validator: SourceGroundingValidator,
) -> list[Evidence]:
    attribute = validator.registry.get_attribute(property_name)
    if attribute is None:
        return _keep_only_verbatim(evidence, chunk_by_index, validator)
    exact = _keep_only_verbatim(evidence, chunk_by_index, validator)
    values = value if isinstance(value, list) else [value]
    preferred_indexes = list(dict.fromkeys(item.chunk_index for item in evidence))

    for item in values:
        if any(
            validator._value_supported(item, ev.text, attribute.range)
            for ev in exact
        ):
            continue
        replacement = _find_supporting_line(
            item,
            preferred_indexes,
            chunk_by_index,
            attribute.range,
            validator,
        )
        if replacement is not None:
            exact.append(replacement)

    return _dedupe_evidence(exact)


def _find_supporting_line(
    value: Any,
    chunk_indexes: list[int],
    chunk_by_index: dict[int, DocumentChunk],
    ranges: list[str],
    validator: SourceGroundingValidator,
) -> Evidence | None:
    for chunk_index in chunk_indexes:
        chunk = chunk_by_index.get(chunk_index)
        if chunk is None:
            continue
        for line in chunk.content.splitlines():
            if not line.strip():
                continue
            if validator._value_supported(value, line, ranges):
                return Evidence(
                    source=chunk.source,
                    chunkIndex=chunk.index,
                    section=chunk.section,
                    text=line,
                )
    return None


def _repair_node_evidence(
    evidence: list[Evidence],
    properties,
    chunk_by_index: dict[int, DocumentChunk],
    validator: SourceGroundingValidator,
) -> list[Evidence]:
    exact = _keep_only_verbatim(evidence, chunk_by_index, validator)
    if exact:
        return exact

    for prop in properties:
        for item in prop.evidence:
            chunk = chunk_by_index.get(item.chunk_index)
            if chunk is not None and validator._contains_quote(chunk.content, item.text):
                return [item.model_copy(deep=True)]

    for item in evidence:
        chunk = chunk_by_index.get(item.chunk_index)
        if chunk is None:
            continue
        for line in chunk.content.splitlines():
            if line.strip():
                return [
                    Evidence(
                        source=chunk.source,
                        chunkIndex=chunk.index,
                        section=chunk.section,
                        text=line,
                    )
                ]
    return evidence


def _repair_edge_evidence(
    edge,
    evidence: list[Evidence],
    chunk_by_index: dict[int, DocumentChunk],
    node_by_temp_id: dict,
    validator: SourceGroundingValidator,
) -> list[Evidence]:
    exact = _keep_only_verbatim(evidence, chunk_by_index, validator)
    if exact and validator._edge_supported(edge, exact, node_by_temp_id):
        return exact

    candidates = list(exact)
    preferred_indexes = list(dict.fromkeys(item.chunk_index for item in evidence))
    for chunk_index in preferred_indexes:
        chunk = chunk_by_index.get(chunk_index)
        if chunk is None:
            continue
        for line in chunk.content.splitlines():
            if not line.strip():
                continue
            candidate = Evidence(
                source=chunk.source,
                chunkIndex=chunk.index,
                section=chunk.section,
                text=line,
            )
            candidates = _dedupe_evidence([*candidates, candidate])
            if validator._edge_supported(edge, candidates, node_by_temp_id):
                return candidates
    return exact if exact else evidence


def _keep_only_verbatim(
    evidence: list[Evidence],
    chunk_by_index: dict[int, DocumentChunk],
    validator: SourceGroundingValidator,
) -> list[Evidence]:
    return [
        item.model_copy(deep=True)
        for item in evidence
        if (chunk := chunk_by_index.get(item.chunk_index)) is not None
        and item.source == chunk.source
        and (item.section is None or item.section == chunk.section)
        and validator._contains_quote(chunk.content, item.text)
    ]


def _dedupe_evidence(items: list[Evidence]) -> list[Evidence]:
    result: list[Evidence] = []
    seen: set[tuple[str, int, str | None, str]] = set()
    for item in items:
        key = (item.source, item.chunk_index, item.section, item.text)
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result
