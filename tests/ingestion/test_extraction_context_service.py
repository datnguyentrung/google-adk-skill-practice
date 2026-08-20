from app.services.ingestion.prepare_extraction_context import ExtractionContextService
from app.tools.ingestion_tools import prepare_extraction_context

DOCUMENT_PATH = "docs/HƯỚNG DẪN NGHIỆP VỤ SẢN PHẨM THẺ TÍN DỤNG FLEXI REWARDS.md"


def test_prepare_extraction_context():
    service = ExtractionContextService()

    context = service.prepare(DOCUMENT_PATH)

    assert context.document_name == (
        "HƯỚNG DẪN NGHIỆP VỤ SẢN PHẨM THẺ TÍN DỤNG FLEXI REWARDS.md"
    )

    assert len(context.chunks) > 0

    assert "pskg:BankingProduct" in (context.ontology_context)

    assert "pskg:productCode" in (context.ontology_context)

    assert "pskg:hasEligibilityRule" in (context.ontology_context)

    assert any("CC-FLEXI-001" in chunk.content for chunk in context.chunks)


def test_prepare_extraction_context_tool():
    result = prepare_extraction_context(DOCUMENT_PATH)

    assert result["document_name"] == (
        "HƯỚNG DẪN NGHIỆP VỤ SẢN PHẨM THẺ TÍN DỤNG FLEXI REWARDS.md"
    )

    assert len(result["chunks"]) > 0

    assert "pskg:BankingProduct" in result["ontology_context"]
