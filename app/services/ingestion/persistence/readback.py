"""Phase 5 — Đọc lại graph vừa ghi để xác nhận dữ liệu đã vào đúng.

Sau khi ghi, hệ thống đọc lại đúng các element ID đã commit và đối chiếu với patch:
số lượng node/relationship, giá trị thuộc tính và node đích của từng quan hệ. Kết quả
là `PersistedGraphReceipt` — bằng chứng để bước fill được coi là thành công."""

from collections import Counter
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from app.core.schemas.ingestion.persistence import (
    GraphWriteResult,
    PersistedGraphReadback,
    PersistedGraphReceipt,
)

from app.services.ingestion.persistence.mapping import Neo4jMapper


def verify_persisted_graph(
    patch,
    write_result: GraphWriteResult,
    readback: PersistedGraphReadback | dict,
    mapper: Neo4jMapper,
) -> PersistedGraphReceipt:
    """
    Đối chiếu patch với dữ liệu đọc lại từ Neo4j và trả về receipt.

    Args:
        patch: Patch đã compile và đã ghi.
        write_result: Kết quả ghi (danh sách element ID đã commit).
        readback: Dữ liệu đọc lại từ Neo4j.
        mapper: Mapper để đổi tên ontology sang định danh Neo4j.

    Returns:
        `PersistedGraphReceipt` với danh sách mismatch (rỗng nghĩa là khớp).
    """
    snapshot = PersistedGraphReadback.model_validate(readback)
    nodes = snapshot.nodes
    relationships = snapshot.relationships
    node_by_id = {node.node_id: node for node in nodes}
    relationship_by_id = {
        relationship.relationship_id: relationship
        for relationship in relationships
    }
    mismatches: list[str] = []

    if len(nodes) != len(patch.nodes):
        mismatches.append(
            f"node count expected {len(patch.nodes)}, read back {len(nodes)}"
        )
    if len(relationships) != len(patch.edges):
        mismatches.append(
            "relationship count expected "
            f"{len(patch.edges)}, read back {len(relationships)}"
        )

    for node in patch.nodes:
        node_id = write_result.node_ids.get(node.temp_id)
        actual = node_by_id.get(node_id or "")
        if actual is None:
            mismatches.append(f"node {node.temp_id} ({node_id}) missing from readback")
            continue
        written = write_result.expected_nodes.get(node.temp_id)
        expected_labels = (
            written.labels
            if written is not None
            else [mapper.class_to_label(node.class_name)]
        )
        if set(actual.labels) != set(expected_labels):
            mismatches.append(
                f"node {node.temp_id} labels expected {expected_labels!r}, "
                f"read back {actual.labels!r}"
            )
        expected_properties = (
            written.properties
            if written is not None
            else mapper.properties_to_neo4j(node.properties)
        )
        for key, expected in expected_properties.items():
            actual_value = actual.properties.get(key)
            if not _values_equal(expected, actual_value):
                mismatches.append(
                    f"node {node.temp_id} property {key} expected "
                    f"{expected!r}, read back {actual_value!r}"
                )
        expected_keys = set(expected_properties)
        actual_keys = set(actual.properties)
        if actual_keys != expected_keys:
            mismatches.append(
                f"node {node.temp_id} property keys expected "
                f"{sorted(expected_keys)!r}, read back "
                f"{sorted(actual_keys)!r}"
            )

    for index, edge in enumerate(patch.edges):
        key = relationship_key(index, edge)
        relationship_id = write_result.relationship_ids.get(key)
        actual = relationship_by_id.get(relationship_id or "")
        if actual is None:
            mismatches.append(f"relationship {key} ({relationship_id}) missing")
            continue
        expected_type = mapper.edge_to_type(edge.edge_name)
        if actual.type != expected_type:
            mismatches.append(
                f"relationship {key} type expected {expected_type}, "
                f"read back {actual.type}"
            )
        expected_source = write_result.node_ids.get(edge.source_temp_id)
        expected_target = write_result.node_ids.get(edge.target_temp_id)
        if actual.source_node_id != expected_source:
            mismatches.append(f"relationship {key} source endpoint mismatch")
        if actual.target_node_id != expected_target:
            mismatches.append(f"relationship {key} target endpoint mismatch")
        written_relationship = write_result.expected_relationships.get(key)
        expected_relationship_properties = (
            written_relationship.properties
            if written_relationship is not None
            else {}
        )
        if not _values_equal(expected_relationship_properties, actual.properties):
            mismatches.append(
                f"relationship {key} properties expected "
                f"{expected_relationship_properties!r}, "
                f"read back {actual.properties!r}"
            )

    label_distribution = Counter(
        label for node in nodes for label in node.labels
    )
    type_distribution = Counter(item.type for item in relationships)
    return PersistedGraphReceipt(
        verified=not mismatches,
        expectedNodeCount=len(patch.nodes),
        expectedRelationshipCount=len(patch.edges),
        nodeIds=write_result.node_ids,
        relationshipIds=write_result.relationship_ids,
        nodes=nodes,
        relationships=relationships,
        labelDistribution=dict(sorted(label_distribution.items())),
        relationshipTypeDistribution=dict(sorted(type_distribution.items())),
        mismatches=mismatches,
    )


def relationship_key(index: int, edge) -> str:
    """
    Tạo khoá ổn định cho một relationship (dùng khi đối chiếu readback).
    """
    return (
        f"{index}:{edge.edge_name}:"
        f"{edge.source_temp_id}->{edge.target_temp_id}"
    )


def _values_equal(expected: Any, actual: Any) -> bool:
    """
    So sánh giá trị mong đợi và giá trị đọc lại sau khi canonical hoá.
    """
    return _canonical_value(expected) == _canonical_value(actual)


def _canonical_value(value: Any) -> Any:
    """
    Canonical hoá giá trị đọc từ Neo4j để so sánh nhất quán.
    """
    if isinstance(value, Decimal):
        return str(value.normalize())
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    iso_format = getattr(value, "iso_format", None)
    if callable(iso_format):
        return iso_format()
    if isinstance(value, list):
        return [_canonical_value(item) for item in value]
    if isinstance(value, dict):
        return {
            key: _canonical_value(item)
            for key, item in sorted(value.items())
        }
    return value
