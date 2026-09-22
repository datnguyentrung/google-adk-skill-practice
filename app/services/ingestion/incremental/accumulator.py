"""Incremental Fact Accumulation and Decomposition.

Converts an ephemeral `GraphPatchFragment` into normalized entity facts,
property facts, edges, chunk coverage, and pending edges suitable for staging.
"""

import hashlib
import json
from typing import Any

from app.core.schemas.ingestion.graph_patch import GraphPatchFragment
from app.core.trace_logger import pprint, trace_pprint
from app.services.ingestion.identity.policies import PRODUCT_SALES_NATURAL_KEYS


def _stable_json(val: Any) -> str:
    return json.dumps(val, ensure_ascii=False, sort_keys=True, default=str)


def is_canonical_entity_ref(temp_id: str) -> bool:
    return (
        temp_id.startswith("entity:") or "|" in temp_id or temp_id.startswith("pskg:")
    )


def extract_canonical_key(temp_id: str) -> str:
    if temp_id.startswith("entity:"):
        return temp_id.removeprefix("entity:")
    return temp_id


def decompose_fragment(
    fragment: GraphPatchFragment,
    *,
    ingestion_id: str,
    batch_index: int,
) -> dict[str, list[dict[str, Any]]]:
    """
    Decompose a GraphPatchFragment into staged data structures.
    """
    temp_to_entity_key: dict[str, str] = {}
    entities: list[dict[str, Any]] = []
    properties: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    pending_edges: list[dict[str, Any]] = []
    coverage: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []

    # 1. Identity Resolution & Entity Extraction
    for node in fragment.nodes:
        natural_key_prop = PRODUCT_SALES_NATURAL_KEYS.get(node.class_name)
        natural_val = None
        if natural_key_prop:
            for p in node.properties:
                if p.property_name == natural_key_prop:
                    natural_val = p.value
                    break

        if natural_val is not None:
            clean_val = str(natural_val).strip().lower()
            entity_key = f"{node.class_name}|{natural_key_prop}|{clean_val}"
        elif is_canonical_entity_ref(node.temp_id):
            entity_key = extract_canonical_key(node.temp_id)
        else:
            # Hash-based key if anonymous (batch-scoped)
            h = hashlib.sha256(
                f"{ingestion_id}:batch_{batch_index}:{node.temp_id}:{node.class_name}".encode("utf-8")
            ).hexdigest()[:12]
            entity_key = f"entity:{node.class_name}:{h}"

        temp_to_entity_key[node.temp_id] = entity_key



        entities.append(
            {
                "entityKey": entity_key,
                "className": node.class_name,
                "confidence": node.confidence,
                "tempId": node.temp_id,
            }
        )

        for prop in node.properties:
            val_json = _stable_json(prop.value)
            val_hash = hashlib.sha256(val_json.encode("utf-8")).hexdigest()[:12]
            evidences = [
                {
                    "evidenceKey": f"{ev.chunk_index}:{hashlib.md5(ev.text.encode('utf-8')).hexdigest()[:8]}",
                    "chunkIndex": ev.chunk_index,
                    "quote": ev.text,
                }
                for ev in prop.evidence
            ]
            properties.append(
                {
                    "entityKey": entity_key,
                    "className": node.class_name,
                    "propertyName": prop.property_name,
                    "valueJson": val_json,
                    "valueHash": val_hash,
                    "isList": isinstance(prop.value, list),
                    "evidence": evidences,
                }
            )

    # 2. Edge & Pending Edge Resolution
    for edge in fragment.edges:
        src_key = temp_to_entity_key.get(edge.source_temp_id)
        if not src_key and is_canonical_entity_ref(edge.source_temp_id):
            src_key = extract_canonical_key(edge.source_temp_id)

        tgt_key = temp_to_entity_key.get(edge.target_temp_id)
        if not tgt_key and is_canonical_entity_ref(edge.target_temp_id):
            tgt_key = extract_canonical_key(edge.target_temp_id)

        if src_key and tgt_key:
            edge_key = f"{edge.edge_name}|{src_key}|{tgt_key}"
            edges.append(
                {
                    "edgeKey": edge_key,
                    "edgeName": edge.edge_name,
                    "sourceEntityKey": src_key,
                    "targetEntityKey": tgt_key,
                    "confidence": edge.confidence,
                }
            )
        else:
            pending_key = f"{edge.edge_name}|{src_key or edge.source_temp_id}|{tgt_key or edge.target_temp_id}"
            pending_edges.append(
                {
                    "pendingKey": pending_key,
                    "edgeName": edge.edge_name,
                    "sourceEntityKey": src_key,
                    "targetEntityKey": tgt_key,
                    "unresolvedRef": edge.source_temp_id
                    if not src_key
                    else edge.target_temp_id,
                }
            )

    # 3. Coverage
    for cov in fragment.coverage:
        coverage.append(
            {
                "chunkIndex": cov.chunk_index,
                "decision": cov.decision,
                "reason": cov.reason,
            }
        )

    decomposed = {
        "entities": entities,
        "properties": properties,
        "edges": edges,
        "pendingEdges": pending_edges,
        "coverage": coverage,
        "conflicts": conflicts,
    }

    trace_pprint(
        f"[TRACE][DECOMPOSED_FACTS] Batch {batch_index} Summary (Entities={len(entities)}, Props={len(properties)}, Edges={len(edges)}, PendingEdges={len(pending_edges)}):",
        decomposed,
    )

    return decomposed
