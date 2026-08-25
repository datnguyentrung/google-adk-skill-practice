from pathlib import Path

from app.core.schemas.ingestion.graph_patch import GraphPatchFragment
from app.services.ingestion.document_reader import DocumentReader
from app.services.ingestion.fragment_grounding_repair import repair_fragment_grounding
from app.services.ingestion.staged_ingestion import IngestionWorkspaceService
from app.services.ingestion.validate_graph_patch import GraphPatchValidationService

DOC = Path("docs/HƯỚNG DẪN NGHIỆP VỤ SẢN PHẨM THẺ TÍN DỤNG FLEXI REWARDS.md")


def _chunk(index: int):
    chunks = DocumentReader().read(DOC)
    return next(chunk for chunk in chunks if chunk.index == index)


def _evidence(chunk, text: str):
    return {
        "source": chunk.source,
        "chunkIndex": chunk.index,
        "section": chunk.section,
        "text": text,
    }


def test_repairs_synthesized_table_evidence_in_flexi_chunk_6():
    chunk = _chunk(6)
    fragment = GraphPatchFragment.model_validate({
        "nodes": [{
            "tempId": "product-cc-flexi-001",
            "className": "pskg:BankingProduct",
            "properties": [
                {
                    "propertyName": "pskg:bankingProductName",
                    "value": "Flexi Rewards",
                    "evidence": [_evidence(chunk, "Tên sản phẩm: Flexi Rewards")],
                },
                {
                    "propertyName": "pskg:productAttributes",
                    "value": ["Gold", "VND", "Có"],
                    "evidence": [_evidence(
                        chunk,
                        "Hạng thẻ: Gold; Đồng tiền thanh toán chính: VND; Thanh toán trực tuyến: Có",
                    )],
                },
            ],
            "evidence": [_evidence(chunk, "Sản phẩm Flexi Rewards hạng Gold")],
            "confidence": 0.95,
        }],
        "edges": [],
        "coverage": [{
            "chunkIndex": chunk.index,
            "decision": "MAPPED",
            "reason": "Product attributes",
        }],
        "warnings": [],
    })

    service = GraphPatchValidationService()
    repaired = repair_fragment_grounding(fragment, [chunk], service.source_grounding)
    issues = service.source_grounding.validate(repaired, [chunk])
    assert issues == []
    properties = {
        item.property_name: item for item in repaired.nodes[0].properties
    }
    attribute_evidence = {
        item.text for item in properties["pskg:productAttributes"].evidence
    }
    assert "| Hạng thẻ | Gold |" in attribute_evidence
    assert "| Đồng tiền thanh toán chính | VND |" in attribute_evidence
    assert any("| Có |" in text for text in attribute_evidence)
    assert properties["pskg:bankingProductName"].evidence[0].text == (
        "| Tên sản phẩm | Flexi Rewards |"
    )


def test_drops_product_name_derived_only_from_document_title():
    chunk = _chunk(1)
    fragment = GraphPatchFragment.model_validate({
        "nodes": [{
            "tempId": "product-cc-flexi-001",
            "className": "pskg:BankingProduct",
            "properties": [
                {
                    "propertyName": "pskg:bankingProductName",
                    "value": "Thẻ tín dụng Flexi Rewards",
                    "evidence": [_evidence(
                        chunk,
                        "| Tên tài liệu | Hướng dẫn nghiệp vụ sản phẩm Thẻ tín dụng Flexi Rewards |",
                    )],
                },
                {
                    "propertyName": "pskg:productCode",
                    "value": "CC-FLEXI-001",
                    "evidence": [_evidence(
                        chunk,
                        "| Mã sản phẩm | CC-FLEXI-001 |",
                    )],
                },
            ],
            "evidence": [_evidence(
                chunk,
                "| Mã sản phẩm | CC-FLEXI-001 |",
            )],
            "confidence": 0.95,
        }],
        "edges": [],
        "coverage": [{
            "chunkIndex": chunk.index,
            "decision": "MAPPED",
            "reason": "Product code",
        }],
        "warnings": [],
    })

    service = GraphPatchValidationService()
    repaired = repair_fragment_grounding(fragment, [chunk], service.source_grounding)
    names = [
        prop.property_name for prop in repaired.nodes[0].properties
    ]
    assert "pskg:bankingProductName" not in names
    assert service.source_grounding.validate(repaired, [chunk]) == []


def test_product_attributes_remove_customer_audience_phrase():
    chunk = _chunk(6)
    fragment = GraphPatchFragment.model_validate({
        "nodes": [{
            "tempId": "product-cc-flexi-001",
            "className": "pskg:BankingProduct",
            "properties": [{
                "propertyName": "pskg:productAttributes",
                "value": ["Dành cho khách hàng cá nhân", "Gold"],
                "evidence": [_evidence(chunk, "| Hạng thẻ | Gold |")],
            }],
            "evidence": [_evidence(chunk, "| Hạng thẻ | Gold |")],
            "confidence": 0.95,
        }],
        "edges": [],
        "coverage": [{
            "chunkIndex": chunk.index,
            "decision": "MAPPED",
            "reason": "Card tier",
        }],
        "warnings": [],
    })
    service = GraphPatchValidationService()
    repaired = repair_fragment_grounding(fragment, [chunk], service.source_grounding)
    prop = repaired.nodes[0].properties[0]
    assert prop.value == ["Gold"]
    assert service.source_grounding.validate(repaired, [chunk]) == []


def test_merge_prefers_explicit_product_name_over_document_title_name():
    metadata = _chunk(1)
    product = _chunk(6)

    def name_fragment(chunk, value, evidence_text):
        return GraphPatchFragment.model_validate({
            "nodes": [{
                "tempId": "product-cc-flexi-001",
                "className": "pskg:BankingProduct",
                "properties": [{
                    "propertyName": "pskg:bankingProductName",
                    "value": value,
                    "evidence": [_evidence(chunk, evidence_text)],
                }],
                "evidence": [_evidence(chunk, evidence_text)],
                "confidence": 0.9,
            }],
            "edges": [],
            "coverage": [{
                "chunkIndex": chunk.index,
                "decision": "MAPPED",
                "reason": "Name evidence",
            }],
            "warnings": [],
        })

    merged = IngestionWorkspaceService.merge_fragments([
        name_fragment(metadata, "Thẻ tín dụng Flexi Rewards",
                      "| Tên tài liệu | Hướng dẫn nghiệp vụ sản phẩm Thẻ tín dụng Flexi Rewards |"),
        name_fragment(product, "Flexi Rewards", "| Tên sản phẩm | Flexi Rewards |"),
    ])
    name = next(
        prop for prop in merged.nodes[0].properties
        if prop.property_name == "pskg:bankingProductName"
    )
    assert name.value == "Flexi Rewards"
    assert [item.text for item in name.evidence] == [
        "| Tên sản phẩm | Flexi Rewards |"
    ]
