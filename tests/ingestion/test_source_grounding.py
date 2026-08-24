from pathlib import Path

from app.services.ingestion.prepare_extraction_context import ExtractionContextService
from app.services.ingestion.validate_graph_patch import GraphPatchValidationService


def chunk(index: int, content: str, section: str = "Fixture") -> dict:
    return {"index": index, "source": "source.md", "section": section, "content": content}


def evidence(index: int, text: str, section: str = "Fixture") -> list[dict]:
    return [{"source": "source.md", "chunkIndex": index, "section": section, "text": text}]


def product_patch(*, status: str | None = None, coverage=None) -> dict:
    product_ev = evidence(0, "Product code P-1; effective 01/08/2026")
    properties = [
        {"propertyName": "pskg:productCode", "value": "P-1", "evidence": product_ev},
        {"propertyName": "pskg:bankingProductEffectiveFrom", "value": "2026-08-01", "evidence": product_ev},
    ]
    if status is not None:
        properties.append({"propertyName": "pskg:bankingProductStatus", "value": status, "evidence": product_ev})
    return {
        "nodes": [{"tempId": "product-1", "className": "pskg:BankingProduct", "properties": properties, "evidence": product_ev, "confidence": 1.0}],
        "edges": [],
        "coverage": coverage or [{"chunkIndex": 0, "decision": "MAPPED", "reason": "Product facts"}],
        "warnings": [],
    }


def test_missing_chunk_coverage_blocks_extraction():
    source = [chunk(0, "Product code P-1; effective 01/08/2026"), chunk(1, "Additional business knowledge") ]
    assessment = GraphPatchValidationService().assess(product_patch(), "artifact", source)
    assert assessment.result.valid_for_extraction is False
    assert "COVERAGE_MISSING" in {issue.code for issue in assessment.result.errors}


def test_hallucinated_published_is_rejected_even_when_ontology_allows_it():
    source = [chunk(0, "Product code P-1; effective 01/08/2026")]
    assessment = GraphPatchValidationService().assess(product_patch(status="Published"), "artifact", source)
    assert assessment.result.valid_for_extraction is False
    assert "PROPERTY_VALUE_NOT_GROUNDED" in {issue.code for issue in assessment.result.errors}


def test_grounded_published_passes_source_grounding():
    source = [chunk(0, "Product code P-1; effective 01/08/2026; status Published")]
    patch = product_patch(status="Published")
    ev = evidence(0, "Product code P-1; effective 01/08/2026; status Published")
    patch["nodes"][0]["evidence"] = ev
    for prop in patch["nodes"][0]["properties"]:
        prop["evidence"] = ev
    assessment = GraphPatchValidationService().assess(patch, "artifact", source)
    assert assessment.result.valid_for_extraction is True
    assert "PROPERTY_VALUE_NOT_GROUNDED" not in {issue.code for issue in assessment.result.errors}


def test_not_relevant_chunk_cannot_be_used_as_evidence():
    source = [chunk(0, "Product code P-1; effective 01/08/2026")]
    patch = product_patch(coverage=[{"chunkIndex": 0, "decision": "NOT_RELEVANT", "reason": "Claimed irrelevant"}])
    assessment = GraphPatchValidationService().assess(patch, "artifact", source)
    assert assessment.result.valid_for_extraction is False
    assert "COVERAGE_CONFLICT" in {issue.code for issue in assessment.result.errors}


def test_flexi_regression_partial_graph_cannot_ignore_rest_of_document():
    document_path = next(Path("docs").glob("*FLEXI REWARDS.md"))
    context = ExtractionContextService().prepare(document_path)
    assert len(context.chunks) > 10
    info = context.chunks[1]
    product_text = "MÃ£ sáº£n pháº©m | CC-FLEXI-001"
    if product_text not in info.content:
        product_text = "CC-FLEXI-001"
    ev = [{"source": info.source, "chunkIndex": info.index, "section": info.section, "text": product_text}]
    patch = {
        "nodes": [{
            "tempId": "product-1",
            "className": "pskg:BankingProduct",
            "properties": [{"propertyName": "pskg:productCode", "value": "CC-FLEXI-001", "evidence": ev}],
            "evidence": ev,
            "confidence": 1.0,
        }],
        "edges": [],
        "coverage": [{"chunkIndex": info.index, "decision": "MAPPED", "reason": "Product metadata"}],
        "warnings": [],
    }
    assessment = GraphPatchValidationService().assess(patch, "artifact", context.chunks)
    assert assessment.result.valid_for_extraction is False
    assert "COVERAGE_MISSING" in {issue.code for issue in assessment.result.errors}
