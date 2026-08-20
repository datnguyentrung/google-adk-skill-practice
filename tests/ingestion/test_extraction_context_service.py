from pathlib import Path

from app.services.ingestion.prepare_extraction_context import ExtractionContextService

DOCUMENT_PATH = Path(
    "docs/HƯỚNG DẪN NGHIỆP VỤ SẢN PHẨM THẺ TÍN DỤNG FLEXI REWARDS.md"
)


def test_prepare_extraction_context():
    service = ExtractionContextService()
    context = service.prepare(DOCUMENT_PATH)

    assert context.document_name == DOCUMENT_PATH.name
    assert context.chunks
    assert "pskg:BankingProduct" in context.ontology_context
    assert "pskg:productCode" in context.ontology_context
    assert "pskg:hasEligibilityRule" in context.ontology_context
    assert any(
        "CC-FLEXI-001" in chunk.content
        for chunk in context.chunks
    )


def test_prepare_uploaded_document_context():
    service = ExtractionContextService()
    data = DOCUMENT_PATH.read_bytes()

    context = service.prepare_uploaded_document(
        filename=DOCUMENT_PATH.name,
        data=data,
        mime_type="text/markdown",
    )

    assert context.document_name == DOCUMENT_PATH.name
    assert context.chunks
    assert "pskg:BankingProduct" in context.ontology_context
    assert any(
        "CC-FLEXI-001" in chunk.content
        for chunk in context.chunks
    )
