from app.core.schemas.ingestion.document import DocumentChunk
from app.core.schemas.ingestion.graph_patch import GraphPatchDraft, GraphPatchFragment
from app.services.ingestion.document import DocumentPreparation
from app.services.ingestion.ontology import OntologyLoader, OntologyRegistry
from app.services.ingestion.orchestration.context import _compact_ontology_context
from app.services.ingestion.validation import GraphValidation
from app.services.ingestion.workspace import IngestionWorkspaceService

SOURCE = "semantic.md"
PRODUCT = "product-flexi"


def _ev(chunk: int, text: str, section: str = "Rules") -> dict:
    return {
        "source": SOURCE,
        "chunkIndex": chunk,
        "section": section,
        "text": text,
    }


def _product() -> dict:
    return {
        "tempId": PRODUCT,
        "className": "pskg:BankingProduct",
        "properties": [
            {
                "propertyName": "pskg:productCode",
                "value": "CC-FLEXI-001",
                "evidence": [_ev(0, "CC-FLEXI-001")],
            }
        ],
        "evidence": [_ev(0, "CC-FLEXI-001")],
        "confidence": 0.99,
    }


def _rule(temp_id: str, chunk: int, condition) -> dict:
    text = condition[0] if isinstance(condition, list) else condition
    return {
        "tempId": temp_id,
        "className": "pskg:BusinessRule",
        "properties": [
            {
                "propertyName": "pskg:businessRuleCondition",
                "value": condition,
                "evidence": [_ev(chunk, text)],
            }
        ],
        "evidence": [_ev(chunk, text)],
        "confidence": 0.92,
    }


def _edge(target: str, chunk: int, text: str) -> dict:
    return {
        "edgeName": "pskg:hasSalesConditionRule",
        "sourceTempId": PRODUCT,
        "targetTempId": target,
        "evidence": [_ev(chunk, text)],
        "confidence": 0.9,
    }


def test_runtime_ontology_schema_exposes_representative_concepts():
    registry = OntologyRegistry(
        OntologyLoader.load(
            "app/data/ontology/product_sales_knowledge_graph_base_v3_1.ontology.json"
        )
    )

    for class_name in [
        "pskg:BankingProduct",
        "pskg:RequiredDocument",
        "pskg:SalesKnowledge",
        "pskg:CustomerSegment",
        "pskg:SalesScript",
        "pskg:Campaign",
    ]:
        assert registry.has_class(class_name)

    for edge_name in [
        "pskg:requiresDocument",
        "pskg:hasKnowledge",
        "pskg:targetsSegment",
        "pskg:hasScript",
        "pskg:campaignHasRule",
        "pskg:governedByPolicy",
    ]:
        assert registry.has_edge(edge_name)


def test_model_ontology_context_is_not_compacted_to_identifiers_only():
    service = DocumentPreparation()
    context = service.build_ontology_context()
    model_context = _compact_ontology_context(context)

    assert "CLASS: pskg:RequiredDocument" in model_context
    assert "pskg:requiresDocument" in model_context
    assert "definition=" in model_context
    assert "DEFINITION:" in model_context


def test_document_level_merge_preserves_distinct_rule_candidates():
    first = GraphPatchFragment.model_validate(
        {
            "nodes": [
                _product(),
                _rule(
                    "rule-fee-cash-withdrawal",
                    1,
                    "Phí rút tiền mặt: 4% số tiền rút, tối thiểu 100.000 VND/giao dịch",
                ),
            ],
            "edges": [
                _edge(
                    "rule-fee-cash-withdrawal",
                    1,
                    "Phí rút tiền mặt: 4% số tiền rút, tối thiểu 100.000 VND/giao dịch",
                )
            ],
            "coverage": [
                {"chunkIndex": 0, "decision": "MAPPED", "reason": "Product code"},
                {"chunkIndex": 1, "decision": "MAPPED", "reason": "Cash fee"},
            ],
            "warnings": [],
        }
    )
    second = GraphPatchFragment.model_validate(
        {
            "nodes": [
                _rule(
                    "rule-cash-advance-fee",
                    2,
                    "4% số tiền rút, tối thiểu 100.000 VND/giao dịch",
                )
            ],
            "edges": [
                _edge(
                    "rule-cash-advance-fee",
                    2,
                    "4% số tiền rút, tối thiểu 100.000 VND/giao dịch",
                )
            ],
            "coverage": [
                {"chunkIndex": 2, "decision": "MAPPED", "reason": "Repeated cash fee"}
            ],
            "warnings": [],
        }
    )

    merged = IngestionWorkspaceService.merge_fragments([first, second])

    rules = [node for node in merged.nodes if node.class_name == "pskg:BusinessRule"]
    assert len(rules) == 2
    assert {node.temp_id for node in rules} == {
        "rule-fee-cash-withdrawal",
        "rule-cash-advance-fee",
    }
    assert len(merged.edges) == 2


def test_document_level_merge_dedupes_same_rule_identity_evidence():
    first = GraphPatchFragment.model_validate(
        {
            "nodes": [
                _product(),
                _rule("rule-cash-fee", 1, "4% số tiền rút"),
            ],
            "edges": [_edge("rule-cash-fee", 1, "4% số tiền rút")],
            "coverage": [
                {"chunkIndex": 0, "decision": "MAPPED", "reason": "Product code"},
                {"chunkIndex": 1, "decision": "MAPPED", "reason": "Cash fee"},
            ],
            "warnings": [],
        }
    )
    second = GraphPatchFragment.model_validate(
        {
            "nodes": [_rule("rule-cash-fee", 2, "4% số tiền rút")],
            "edges": [_edge("rule-cash-fee", 2, "4% số tiền rút")],
            "coverage": [
                {"chunkIndex": 2, "decision": "MAPPED", "reason": "Repeated cash fee"}
            ],
            "warnings": [],
        }
    )

    merged = IngestionWorkspaceService.merge_fragments([first, second])

    rules = [node for node in merged.nodes if node.class_name == "pskg:BusinessRule"]
    assert len(rules) == 1
    assert {
        item.chunk_index
        for prop in rules[0].properties
        for item in prop.evidence
    } == {1, 2}
    assert len(merged.edges) == 1


def test_consolidation_does_not_merge_same_percentage_different_semantics():
    first = GraphPatchFragment.model_validate(
        {
            "nodes": [
                _product(),
                _rule("rule-cashback", 1, "Hoàn tiền 5% cho mua sắm trực tuyến"),
            ],
            "edges": [_edge("rule-cashback", 1, "Hoàn tiền 5% cho mua sắm trực tuyến")],
            "coverage": [
                {"chunkIndex": 0, "decision": "MAPPED", "reason": "Product code"},
                {"chunkIndex": 1, "decision": "MAPPED", "reason": "Cashback"},
            ],
            "warnings": [],
        }
    )
    second = GraphPatchFragment.model_validate(
        {
            "nodes": [
                _rule("rule-min-payment", 2, "Thanh toán tối thiểu 5% dư nợ sao kê")
            ],
            "edges": [_edge("rule-min-payment", 2, "Thanh toán tối thiểu 5% dư nợ sao kê")],
            "coverage": [
                {"chunkIndex": 2, "decision": "MAPPED", "reason": "Minimum payment"}
            ],
            "warnings": [],
        }
    )

    merged = IngestionWorkspaceService.merge_fragments([first, second])

    rules = [node for node in merged.nodes if node.class_name == "pskg:BusinessRule"]
    assert len(rules) == 2


def test_required_document_candidate_is_valid_extraction_concept():
    text = "Hồ sơ gồm Hợp đồng lao động và Sao kê nhận lương 3-6 tháng."
    draft = GraphPatchDraft.model_validate(
        {
            "nodes": [
                _product(),
                {
                    "tempId": "doc-labor-contract",
                    "className": "pskg:RequiredDocument",
                    "properties": [
                        {
                            "propertyName": "pskg:documentName",
                            "value": "Hợp đồng lao động",
                            "evidence": [_ev(1, "Hợp đồng lao động", "Documents")],
                        },
                        {
                            "propertyName": "pskg:requiredDocumentDescription",
                            "value": "Sao kê nhận lương 3-6 tháng",
                            "evidence": [
                                _ev(1, "Sao kê nhận lương 3-6 tháng", "Documents")
                            ],
                        },
                    ],
                    "evidence": [_ev(1, text, "Documents")],
                    "confidence": 0.94,
                },
            ],
            "edges": [
                {
                    "edgeName": "pskg:requiresDocument",
                    "sourceTempId": PRODUCT,
                    "targetTempId": "doc-labor-contract",
                    "evidence": [_ev(1, text, "Documents")],
                    "confidence": 0.9,
                }
            ],
            "coverage": [
                {"chunkIndex": 0, "decision": "MAPPED", "reason": "Product code"},
                {"chunkIndex": 1, "decision": "MAPPED", "reason": "Documents"},
            ],
            "warnings": [],
        }
    )
    chunks = [
        DocumentChunk(index=0, source=SOURCE, section="Rules", content="CC-FLEXI-001"),
        DocumentChunk(index=1, source=SOURCE, section="Documents", content=text),
    ]

    assessment = GraphValidation().assess(draft, "digest", chunks)

    assert assessment.result.valid_for_extraction is True
    assert not assessment.result.errors


def test_customer_segment_candidate_is_valid_extraction_concept():
    text = "Khách hàng nhận lương và khách hàng thường xuyên mua sắm online."
    draft = GraphPatchDraft.model_validate(
        {
            "nodes": [
                _product(),
                {
                    "tempId": "segment-salary",
                    "className": "pskg:CustomerSegment",
                    "properties": [
                        {
                            "propertyName": "pskg:segmentName",
                            "value": "Khách hàng nhận lương",
                            "evidence": [_ev(1, "Khách hàng nhận lương", "Segments")],
                        }
                    ],
                    "evidence": [_ev(1, "Khách hàng nhận lương", "Segments")],
                    "confidence": 0.9,
                },
            ],
            "edges": [
                {
                    "edgeName": "pskg:targetsSegment",
                    "sourceTempId": PRODUCT,
                    "targetTempId": "segment-salary",
                    "evidence": [_ev(1, text, "Segments")],
                    "confidence": 0.86,
                }
            ],
            "coverage": [
                {"chunkIndex": 0, "decision": "MAPPED", "reason": "Product code"},
                {"chunkIndex": 1, "decision": "MAPPED", "reason": "Segment"},
            ],
            "warnings": [],
        }
    )
    chunks = [
        DocumentChunk(index=0, source=SOURCE, section="Rules", content="CC-FLEXI-001"),
        DocumentChunk(index=1, source=SOURCE, section="Segments", content=text),
    ]

    assessment = GraphValidation().assess(draft, "digest", chunks)

    assert assessment.result.valid_for_extraction is True


def test_sales_script_candidate_uses_dialogue_evidence():
    text = (
        "Nhân viên: Chào chị, chị đang mua sắm online thường xuyên phải không?\n"
        "Khách hàng: Tôi muốn tìm hiểu hoàn tiền.\n"
        "Nhân viên: Flexi Rewards có thể phù hợp nếu chị thanh toán đúng hạn."
    )
    draft = GraphPatchDraft.model_validate(
        {
            "nodes": [
                _product(),
                {
                    "tempId": "script-dialogue",
                    "className": "pskg:SalesScript",
                    "properties": [
                        {
                            "propertyName": "pskg:scenario",
                            "value": "mua sắm online",
                            "evidence": [_ev(1, "mua sắm online", "Dialogue")],
                        },
                        {
                            "propertyName": "pskg:openingLine",
                            "value": "Chào chị",
                            "evidence": [_ev(1, "Nhân viên: Chào chị", "Dialogue")],
                        },
                    ],
                    "evidence": [_ev(1, "Nhân viên: Chào chị", "Dialogue")],
                    "confidence": 0.9,
                },
            ],
            "edges": [
                {
                    "edgeName": "pskg:hasScript",
                    "sourceTempId": PRODUCT,
                    "targetTempId": "script-dialogue",
                    "evidence": [_ev(1, text, "Dialogue")],
                    "confidence": 0.86,
                }
            ],
            "coverage": [
                {"chunkIndex": 0, "decision": "MAPPED", "reason": "Product code"},
                {"chunkIndex": 1, "decision": "MAPPED", "reason": "Dialogue"},
            ],
            "warnings": [],
        }
    )
    chunks = [
        DocumentChunk(index=0, source=SOURCE, section="Rules", content="CC-FLEXI-001"),
        DocumentChunk(index=1, source=SOURCE, section="Dialogue", content=text),
    ]

    assessment = GraphValidation().assess(draft, "digest", chunks)

    assert assessment.result.valid_for_extraction is True


def test_utf8_document_reader_preserves_vietnamese_text():
    chunks = DocumentPreparation().reader.read(
        "docs/HƯỚNG DẪN NGHIỆP VỤ SẢN PHẨM THẺ TÍN DỤNG FLEXI REWARDS.md"
    )

    assert any("Hợp đồng lao động" in chunk.content for chunk in chunks)
    assert all("HÆ" not in chunk.source for chunk in chunks)


def test_failed_relevant_coverage_is_not_success():
    draft = GraphPatchDraft.model_validate(
        {
            "nodes": [_product()],
            "edges": [],
            "coverage": [
                {"chunkIndex": 0, "decision": "MAPPED", "reason": "Product code"},
                {
                    "chunkIndex": 1,
                    "decision": "FAILED",
                    "reason": "Relevant document facts were not represented",
                },
            ],
            "warnings": [],
        }
    )
    chunks = [
        DocumentChunk(index=0, source=SOURCE, section="Rules", content="CC-FLEXI-001"),
        DocumentChunk(
            index=1,
            source=SOURCE,
            section="Documents",
            content="Hợp đồng lao động",
        ),
    ]

    assessment = GraphValidation().assess(draft, "digest", chunks)

    assert assessment.result.valid_for_extraction is False
    assert {issue.code for issue in assessment.result.errors} == {
        "COVERAGE_NOT_EVIDENCED"
    }
