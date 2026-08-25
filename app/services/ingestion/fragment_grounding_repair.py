from __future__ import annotations

from typing import Any

from app.core.schemas.ingestion.document import DocumentChunk
from app.core.schemas.ingestion.graph_patch import Evidence, GraphPatchFragment
from app.services.ingestion.source_grounding import SourceGroundingValidator

PRODUCT_ATTRIBUTES = "pskg:productAttributes"
PRODUCT_NAME = "pskg:bankingProductName"


def repair_fragment_grounding(
    fragment: GraphPatchFragment,
    chunks: list[DocumentChunk],
    validator: SourceGroundingValidator,
) -> GraphPatchFragment:
    """Repair safe, deterministic grounding defects before validation."""
    repaired = fragment.model_copy(deep=True)
    chunk_by_index = {chunk.index: chunk for chunk in chunks}

    for node in repaired.nodes:
        kept_properties = []
        for prop in node.properties:
            if _is_document_title_product_name(prop):
                continue
            if prop.property_name == PRODUCT_ATTRIBUTES:
                values = prop.value if isinstance(prop.value, list) else [prop.value]
                values = [value for value in values if not _is_customer_audience(value)]
                values = _dedupe_values(values)
                if not values:
                    continue
                prop.value = values

            prop.evidence = _repair_property_evidence(
                prop.property_name,
                prop.value,
                prop.evidence,
                chunk_by_index,
                validator,
            )
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
    return repaired


def _is_document_title_product_name(prop) -> bool:
    if prop.property_name != PRODUCT_NAME:
        return False
    texts = [validator_text(item.text) for item in prop.evidence]
    sections = [validator_text(item.section or "") for item in prop.evidence]
    if any("tên sản phẩm" in text for text in texts):
        return False
    return any("tên tài liệu" in text for text in texts) or any(
        "thông tin tài liệu" in section for section in sections
    )


def _is_customer_audience(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    normalized = validator_text(value)
    return normalized.startswith("dành cho khách hàng")


def validator_text(value: str) -> str:
    return SourceGroundingValidator._normalize(value)


def _dedupe_values(values: list[Any]) -> list[Any]:
    result: list[Any] = []
    seen: set[str] = set()
    for value in values:
        key = repr(value)
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


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

    return _dedupe_evidence(exact) if exact else evidence


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
                return [Evidence(
                    source=chunk.source,
                    chunkIndex=chunk.index,
                    section=chunk.section,
                    text=line,
                )]
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
                source=chunk.source, chunkIndex=chunk.index,
                section=chunk.section, text=line,
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
