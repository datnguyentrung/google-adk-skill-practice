"""Reference-counted cleanup for superseded/deleted ingestion sources."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from neo4j import Transaction

from app.services.ingestion.identity.policies import PRODUCT_SALES_NATURAL_KEYS

logger = logging.getLogger(__name__)
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def cleanup_stale_assertions(
    tx: Transaction,
    *,
    old_version_id: str | None,
    new_version_id: str | None,
    mapper=None,
) -> dict[str, int]:
    """Remove domain facts no longer owned by any current source version."""
    if not old_version_id or old_version_id == new_version_id:
        return {"properties": 0, "edges": 0, "nodes": 0}

    result = tx.run(
        """
        MATCH (old:IngestionSourceAssertion {versionId: $old_version_id})
        WHERE $new_version_id IS NULL OR NOT EXISTS {
            MATCH (fresh:IngestionSourceAssertion {
                versionId: $new_version_id,
                assertionKey: old.assertionKey
            })
        }
        OPTIONAL MATCH (old)-[:ABOUT_NODE]->(n)
        RETURN old.kind AS kind,
               old.assertionKey AS assertion_key,
               old.payloadJson AS payload_json,
               elementId(n) AS node_id
        """,
        old_version_id=old_version_id,
        new_version_id=new_version_id,
    )
    stale = [dict(record) for record in result]
    counts = {"properties": 0, "edges": 0, "nodes": 0}
    node_candidates: set[str] = set()

    # Remove old lexical evidence links first; historical assertions remain.
    tx.run(
        """
        MATCH ()-[r:INGESTION_EVIDENCED_BY {versionId: $old_version_id}]->()
        DELETE r
        """,
        old_version_id=old_version_id,
    ).consume()

    for item in stale:
        assertion_key = item.get("assertion_key")
        if not assertion_key or _has_current_support(tx, assertion_key):
            continue
        payload = _payload(item.get("payload_json"))
        kind = item.get("kind")
        node_id = item.get("node_id")
        if node_id:
            node_candidates.add(node_id)

        if kind == "PROPERTY" and node_id:
            class_name = str(payload.get("className") or "")
            property_name = str(payload.get("propertyName") or "")
            if PRODUCT_SALES_NATURAL_KEYS.get(class_name) == property_name:
                continue
            property_key = payload.get("neo4jPropertyKey")
            if not property_key and mapper is not None and property_name:
                property_key = mapper.property_to_key(property_name)
            if _remove_property(tx, node_id, property_key):
                counts["properties"] += 1
        elif kind == "EDGE":
            relationship_id = payload.get("relationshipElementId")
            if relationship_id and _delete_relationship(tx, str(relationship_id)):
                counts["edges"] += 1

    for node_id in sorted(node_candidates):
        if _delete_orphan_node(tx, node_id):
            counts["nodes"] += 1
    return counts


def _has_current_support(tx: Transaction, assertion_key: str) -> bool:
    record = tx.run(
        """
        MATCH (support:IngestionSourceAssertion {assertionKey: $assertion_key})
        MATCH (d:IngestionSourceDocument {currentVersionId: support.versionId})
        RETURN count(support) > 0 AS supported
        """,
        assertion_key=assertion_key,
    ).single()
    return bool(record and record["supported"])


def _remove_property(tx: Transaction, node_id: str, property_key: Any) -> bool:
    if not isinstance(property_key, str) or not _SAFE_IDENTIFIER.fullmatch(property_key):
        logger.warning("Skip unsafe stale property key=%r node_id=%s", property_key, node_id)
        return False
    record = tx.run(
        f"""
        MATCH (n) WHERE elementId(n) = $node_id
        WITH n, n.`{property_key}` AS old_value
        REMOVE n.`{property_key}`
        RETURN old_value IS NOT NULL AS removed
        """,
        node_id=node_id,
    ).single()
    return bool(record and record["removed"])


def _delete_relationship(tx: Transaction, relationship_id: str) -> bool:
    record = tx.run(
        """
        MATCH ()-[r]->() WHERE elementId(r) = $relationship_id
        WITH r, true AS existed
        DELETE r
        RETURN existed
        """,
        relationship_id=relationship_id,
    ).single()
    return bool(record and record["existed"])


def _delete_orphan_node(tx: Transaction, node_id: str) -> bool:
    record = tx.run(
        """
        MATCH (n) WHERE elementId(n) = $node_id
        WHERE NOT EXISTS {
            MATCH (a:IngestionSourceAssertion)-[:ABOUT_NODE]->(n)
            MATCH (d:IngestionSourceDocument {currentVersionId: a.versionId})
        }
        AND NOT EXISTS {
            MATCH (n)-[r]-()
            WHERE NOT type(r) IN ['INGESTION_EVIDENCED_BY', 'ABOUT_NODE']
        }
        WITH n, true AS deletable
        DETACH DELETE n
        RETURN deletable
        """,
        node_id=node_id,
    ).single()
    return bool(record and record["deletable"])


def _payload(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    try:
        payload = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


__all__ = ["cleanup_stale_assertions"]
