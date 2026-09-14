"""Tests for SchemaRouter."""

from app.core.schemas.ingestion.document import DocumentChunk
from app.services.ingestion.schema.router import SchemaRouter
from app.services.ingestion.schema.skill_registry import SchemaSkillRegistry


def test_schema_router_select_for_document_matched():
    registry = SchemaSkillRegistry()
    router = SchemaRouter(registry)

    chunks = [
        DocumentChunk(
            index=0,
            content="Điều kiện đăng ký thẻ tín dụng và hồ sơ yêu cầu.",
            source="test_doc.txt",
            section="Business Rules",
        )
    ]

    candidates = router.select_for_document(chunks, registry.list_skills())
    assert "business-rules" in candidates


def test_schema_router_select_for_document_fallback():
    registry = SchemaSkillRegistry()
    router = SchemaRouter(registry)

    chunks = [
        DocumentChunk(
            index=0,
            content="Text without matching domain keywords 123456789.",
            source="test_doc.txt",
            section="Unknown",
        )
    ]

    candidates = router.select_for_document(chunks, registry.list_skills())
    assert set(candidates) == set(registry.list_skills())


def test_schema_router_select_for_batch():
    registry = SchemaSkillRegistry()
    router = SchemaRouter(registry)

    chunks = [
        DocumentChunk(
            index=0,
            content="Lãi suất sản phẩm thẻ tín dụng quốc tế.",
            source="test_doc.txt",
            section="Product Info",
        )
    ]

    selected, reasons = router.select_for_batch(chunks, ["product-catalog", "governance-versioning"])
    assert "product-catalog" in selected
    assert len(reasons) > 0


def test_schema_router_select_for_retry():
    registry = SchemaSkillRegistry()
    router = SchemaRouter(registry)

    chunks = [DocumentChunk(index=0, content="Dummy content", source="doc.txt")]
    candidate_ids = ["business-rules", "product-catalog", "governance-versioning"]

    expanded, reasons = router.select_for_retry(
        chunks,
        previous_skill_ids=["business-rules"],
        previous_error={"error": "domain_range_mismatch"},
        candidate_skill_ids=candidate_ids,
    )

    assert "business-rules" in expanded
    assert "product-catalog" in expanded
    assert "governance-versioning" in expanded
    assert len(reasons) > 0
