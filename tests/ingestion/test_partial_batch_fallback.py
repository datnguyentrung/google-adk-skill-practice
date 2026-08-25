from app.core.schemas.ingestion.graph_patch import GraphPatchFragment
from app.services.ingestion.partial_batch_fallback import (
    can_skip_chunks_safely,
    failed_chunk_indexes,
    prune_fragment_for_skips,
)
from app.services.ingestion.validate_graph_patch import GraphPatchValidationService


def _evidence(chunk_index: int, text: str):
    return [{
        "source": "product.md",
        "chunkIndex": chunk_index,
        "section": f"S{chunk_index}",
        "text": text,
    }]


def _fragment(property_name="pskg:businessRuleCondition"):
    evidence = _evidence(23, "Điều kiện duy trì miễn lãi")
    return GraphPatchFragment.model_validate({
        "nodes": [{
            "tempId": "rule-1",
            "className": "pskg:BusinessRule",
            "properties": [{
                "propertyName": property_name,
                "value": "Điều kiện duy trì miễn lãi",
                "evidence": evidence,
            }],
            "evidence": evidence,
            "confidence": 1.0,
        }],        "edges": [],
        "coverage": [
            {"chunkIndex": 20, "decision": "NOT_RELEVANT", "reason": "reviewed"},
            {"chunkIndex": 21, "decision": "NOT_RELEVANT", "reason": "reviewed"},
            {"chunkIndex": 22, "decision": "NOT_RELEVANT", "reason": "reviewed"},
            {"chunkIndex": 23, "decision": "MAPPED", "reason": "rule"},
            {"chunkIndex": 24, "decision": "NOT_RELEVANT", "reason": "reviewed"},
        ],
        "warnings": [],
    })


def test_failed_chunk_indexes_resolves_property_evidence_chunk():
    fragment = _fragment()
    response = {
        "affectedChunkIndexes": [23],
        "errors": [{
            "code": "PROPERTY_VALUE_NOT_GROUNDED",
            "location": "nodes.0.properties.0.evidence",
            "nodeTempId": "rule-1",
            "propertyName": "pskg:businessRuleCondition",
        }],
    }
    assert failed_chunk_indexes(
        response, fragment, {"chunkIndexes": [20, 21, 22, 23, 24]}
    ) == [23]


def test_noncritical_rule_chunk_can_be_skipped():
    assert can_skip_chunks_safely(_fragment(), {23}) is True


def test_critical_product_identity_chunk_cannot_be_skipped():
    fragment = _fragment("pskg:productCode")
    assert can_skip_chunks_safely(fragment, {23}) is False


def test_prune_marks_failed_chunk_not_relevant_and_removes_fact():
    fragment = _fragment()
    validator = GraphPatchValidationService().source_grounding
    pruned = prune_fragment_for_skips(fragment, {23}, validator)

    coverage = {item.chunk_index: item for item in pruned.coverage}
    assert coverage[23].decision == "NOT_RELEVANT"
    assert pruned.nodes == []
    assert pruned.edges == []
