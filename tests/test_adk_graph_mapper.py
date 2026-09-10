import pytest

from app.core.schemas.ingestion.document import DocumentChunk
from app.services.ingestion.mapping import AdkGraphMapper, DirectGraphMappingError
from app.services.ingestion.validation import GraphValidation


SOURCE = "test.md"


def _chunk(index: int = 0, content: str = "Product code is P-001.") -> DocumentChunk:
    return DocumentChunk(index=index, source=SOURCE, section="Product", content=content)


def _payload(chunks: list[DocumentChunk], batch_index: int = 0) -> dict:
    return {
        "batchIndex": batch_index,
        "chunkIndexes": [chunk.index for chunk in chunks],
        "chunks": [chunk.model_dump(by_alias=True, mode="json") for chunk in chunks],
    }


class _StructuredExecutor:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def run(self, **kwargs):
        self.calls.append(kwargs)
        return self.payload


def _mapper(payload):
    validation = GraphValidation()
    executor = _StructuredExecutor(payload)
    mapper = AdkGraphMapper(
        registry=validation.validator.registry,
        compiler=validation.compiler,
        ontology_validator=validation.validator,
        structured_executor=executor,
    )
    return mapper, executor


def _evidence(index: int, text: str) -> dict:
    return {"source": SOURCE, "chunkIndex": index, "section": "Product", "text": text}


def test_direct_mapper_uses_dynamic_native_json_schema():
    text = "Product code is P-001. Effective from 2026-07-01."
    payload = {
        "nodes": [{
            "tempId": "product-p001",
            "className": "pskg:BankingProduct",
            "properties": [
                {"propertyName": "pskg:productCode", "value": "P-001", "evidence": [_evidence(0, text)]},
                {"propertyName": "pskg:bankingProductEffectiveFrom", "value": "2026-07-01", "evidence": [_evidence(0, text)]},
            ],
            "evidence": [_evidence(0, text)],
            "confidence": 1.0,
        }],
        "edges": [],
        "coverage": [{"chunkIndex": 0, "decision": "MAPPED", "reason": "Product code mapped"}],
        "warnings": [],
    }
    mapper, client = _mapper(payload)
    fragment = mapper.map_batch(batch_payload=_payload([_chunk(content=text)]), chunks=[_chunk(content=text)])
    assert fragment.nodes[0].class_name == "pskg:BankingProduct"
    schema = client.calls[0]["output_schema"]
    assert "pskg:BankingProduct" in schema["$defs"]["ExtractedNode"]["properties"]["className"]["enum"]
    assert "pskg:requiresDocument" in schema["$defs"]["ExtractedEdge"]["properties"]["edgeName"]["enum"]
    assert schema["$defs"]["ChunkCoverage"]["properties"]["chunkIndex"]["enum"] == [0]
    assert client.calls[0]["operation"] == "direct_graph_mapping"


def test_context_edge_reference_is_hydrated_without_duplicate_semantics():
    text = "The customer must provide a valid identification document."
    payload = {
        "nodes": [{
            "tempId": "doc-id",
            "className": "pskg:RequiredDocument",
            "properties": [{"propertyName": "pskg:documentName", "value": "valid identification document", "evidence": [_evidence(0, text)]}],
            "evidence": [_evidence(0, text)],
            "confidence": 1.0,
        }],
        "edges": [{
            "edgeName": "pskg:requiresDocument",
            "sourceTempId": "product-existing",
            "targetTempId": "doc-id",
            "evidence": [_evidence(0, text)],
            "confidence": 1.0,
        }],
        "coverage": [{"chunkIndex": 0, "decision": "MAPPED", "reason": "Document requirement mapped"}],
        "warnings": [],
    }
    mapper, _ = _mapper(payload)
    context = '- ref=product-existing\n  class=pskg:BankingProduct\n  identity={"pskg:productCode":"P-001"}'
    fragment = mapper.map_batch(batch_payload=_payload([_chunk(content=text)]), chunks=[_chunk(content=text)], graph_context=context)
    hydrated = {node.temp_id: node for node in fragment.nodes}["product-existing"]
    assert hydrated.class_name == "pskg:BankingProduct"
    assert hydrated.properties == []
    assert fragment.edges[0].source_temp_id == "product-existing"


def test_non_verbatim_evidence_is_rejected_for_retry():
    text = "Product code is P-001."
    payload = {
        "nodes": [{
            "tempId": "product-p001",
            "className": "pskg:BankingProduct",
            "properties": [{"propertyName": "pskg:productCode", "value": "P-001", "evidence": [_evidence(0, "paraphrased evidence")]}],
            "evidence": [_evidence(0, "paraphrased evidence")],
            "confidence": 1.0,
        }],
        "edges": [],
        "coverage": [{"chunkIndex": 0, "decision": "MAPPED", "reason": "Mapped"}],
        "warnings": [],
    }
    mapper, _ = _mapper(payload)
    with pytest.raises(DirectGraphMappingError) as exc:
        mapper.map_batch(batch_payload=_payload([_chunk(content=text)]), chunks=[_chunk(content=text)])
    assert any(item["code"] == "EVIDENCE_NOT_VERBATIM" for item in exc.value.summary["errors"])


def test_empty_graph_requires_non_mapped_coverage():
    payload = {
        "nodes": [],
        "edges": [],
        "coverage": [{"chunkIndex": 0, "decision": "NOT_RELEVANT", "reason": "No ontology counterpart"}],
        "warnings": [],
    }
    mapper, _ = _mapper(payload)
    fragment = mapper.map_batch(batch_payload=_payload([_chunk()]), chunks=[_chunk()])
    assert fragment.nodes == []


def test_compact_ontology_is_smaller_than_raw_catalog_and_keeps_semantics():
    mapper, _ = _mapper({"nodes": [], "edges": [], "coverage": [{"chunkIndex": 0, "decision": "NOT_RELEVANT", "reason": "none"}], "warnings": []})
    skeleton = mapper._ontology_projection
    assert len(skeleton) < 20_000
    assert "pskg:BankingProduct" in skeleton
    assert "pskg:BusinessRule" in skeleton
    assert "pskg:hasEligibilityRule" in skeleton
    assert "pskg:requiresDocument" in skeleton
    assert "SOURCE PROPERTIES" in skeleton

def test_conflicting_context_ref_becomes_batch_local_node():
    text = "The interest-free period is up to 50 days."
    payload = {
        "nodes": [{
            "tempId": "rule-existing",
            "className": "pskg:BusinessRule",
            "properties": [{"propertyName": "pskg:businessRuleCondition", "value": "Up to 50 days.", "evidence": [_evidence(0, text)]}],
            "evidence": [_evidence(0, text)],
            "confidence": 1.0,
        }],
        "edges": [{
            "edgeName": "pskg:hasSalesConditionRule",
            "sourceTempId": "product-existing",
            "targetTempId": "rule-existing",
            "evidence": [_evidence(0, text)],
            "confidence": 1.0,
        }],
        "coverage": [{"chunkIndex": 0, "decision": "MAPPED", "reason": "Mapped rule"}],
        "warnings": [],
    }
    mapper, _ = _mapper(payload)
    context = (
        '- ref=product-existing\n  class=pskg:BankingProduct\n  identity={"pskg:productCode":"P-001"}\n'
        '- ref=rule-existing\n  class=pskg:BusinessRule\n  identity={"pskg:businessRuleCondition":"Pay the full statement balance by the due date."}'
    )
    fragment = mapper.map_batch(
        batch_payload=_payload([_chunk(content=text)], batch_index=13),
        chunks=[_chunk(content=text)],
        graph_context=context,
    )
    assert any(node.temp_id == "b13__rule-existing" for node in fragment.nodes)
    assert fragment.edges[0].source_temp_id == "product-existing"
    assert fragment.edges[0].target_temp_id == "b13__rule-existing"

def test_new_banking_product_missing_required_source_fact_is_rejected_for_retry():
    text = "Related product code is CASA-FLEX-001."
    payload = {
        "nodes": [{
            "tempId": "related-product",
            "className": "pskg:BankingProduct",
            "properties": [{
                "propertyName": "pskg:productCode",
                "value": "CASA-FLEX-001",
                "evidence": [_evidence(0, text)],
            }],
            "evidence": [_evidence(0, text)],
            "confidence": 1.0,
        }],
        "edges": [],
        "coverage": [{"chunkIndex": 0, "decision": "MAPPED", "reason": "Related product mapped"}],
        "warnings": [],
    }
    mapper, _ = _mapper(payload)

    with pytest.raises(DirectGraphMappingError) as exc:
        mapper.map_batch(
            batch_payload=_payload([_chunk(content=text)]),
            chunks=[_chunk(content=text)],
        )

    assert any(
        item["code"] == "MISSING_REQUIRED_SOURCE_FACT"
        and item.get("propertyName") == "pskg:bankingProductEffectiveFrom"
        for item in exc.value.summary["errors"]
    )


def test_new_sales_knowledge_missing_knowledge_type_is_rejected_for_retry():
    text = "Frequently Asked Questions"
    payload = {
        "nodes": [{
            "tempId": "faq-knowledge",
            "className": "pskg:SalesKnowledge",
            "properties": [{
                "propertyName": "pskg:title",
                "value": text,
                "evidence": [_evidence(0, text)],
            }],
            "evidence": [_evidence(0, text)],
            "confidence": 1.0,
        }],
        "edges": [],
        "coverage": [{"chunkIndex": 0, "decision": "MAPPED", "reason": "FAQ knowledge mapped"}],
        "warnings": [],
    }
    mapper, _ = _mapper(payload)

    with pytest.raises(DirectGraphMappingError) as exc:
        mapper.map_batch(
            batch_payload=_payload([_chunk(content=text)]),
            chunks=[_chunk(content=text)],
        )

    assert any(
        item["code"] == "MISSING_REQUIRED_SOURCE_FACT"
        and item.get("propertyName") == "pskg:knowledgeType"
        for item in exc.value.summary["errors"]
    )


def test_markdown_table_evidence_is_canonicalized_to_full_row():
    text = "| Document Name | Passport |"
    payload = {
        "nodes": [{
            "tempId": "document-passport",
            "className": "pskg:RequiredDocument",
            "properties": [{"propertyName": "pskg:documentName", "value": "Passport", "evidence": [_evidence(0, "Document Name | Passport")]}],
            "evidence": [_evidence(0, "Document Name | Passport")],
            "confidence": 1.0,
        }],
        "edges": [],
        "coverage": [{"chunkIndex": 0, "decision": "MAPPED", "reason": "Mapped product"}],
        "warnings": [],
    }
    mapper, _ = _mapper(payload)
    fragment = mapper.map_batch(batch_payload=_payload([_chunk(content=text)]), chunks=[_chunk(content=text)])
    assert fragment.nodes[0].properties[0].evidence[0].text == text


def test_same_batch_repair_temp_ids_are_idempotent_for_rule_edges():
    source_text = (
        "Product code is CC-FLEXI-001.\n"
        "Campaign cashback applies when the cardholder spends at least 10,000,000 VND.\n"
    )
    chunks = [_chunk(index=30, content=source_text)]
    rule_condition = (
        "Campaign cashback applies when the cardholder spends at least 10,000,000 VND."
    )
    edge_text = (
        "Product code is CC-FLEXI-001.\n"
        "Campaign cashback applies when the cardholder spends at least 10,000,000 VND."
    )

    for rule_id in (
        "rule_campaign_cashback",
        "b6__rule_campaign_cashback",
        "b6__b6__rule_campaign_cashback",
    ):
        payload = {
            "nodes": [
                {
                    "tempId": "product-flexi",
                    "className": "pskg:BankingProduct",
                    "properties": [
                        {
                            "propertyName": "pskg:productCode",
                            "value": "CC-FLEXI-001",
                            "evidence": [_evidence(30, "CC-FLEXI-001")],
                        }
                    ],
                    "evidence": [_evidence(30, "CC-FLEXI-001")],
                    "confidence": 1.0,
                },
                {
                    "tempId": rule_id,
                    "className": "pskg:BusinessRule",
                    "properties": [
                        {
                            "propertyName": "pskg:businessRuleCondition",
                            "value": rule_condition,
                            "evidence": [_evidence(30, rule_condition)],
                        }
                    ],
                    "evidence": [_evidence(30, rule_condition)],
                    "confidence": 1.0,
                },
            ],
            "edges": [
                {
                    "edgeName": "pskg:hasSalesConditionRule",
                    "sourceTempId": "product-flexi",
                    "targetTempId": rule_id,
                    "evidence": [_evidence(30, edge_text)],
                    "confidence": 1.0,
                }
            ],
            "coverage": [
                {
                    "chunkIndex": 30,
                    "decision": "MAPPED",
                    "reason": "Campaign cashback rule mapped",
                }
            ],
            "warnings": [],
        }
        mapper, _ = _mapper(payload)

        fragment = mapper.map_batch(
            batch_payload=_payload(chunks, batch_index=6),
            chunks=chunks,
            graph_context=(
                '- ref=product-flexi\n'
                '  class=pskg:BankingProduct\n'
                '  identity={"pskg:productCode":"CC-FLEXI-001"}'
            ),
        )

        temp_ids = [node.temp_id for node in fragment.nodes]
        assert all(not temp_id.startswith("b6__b6__") for temp_id in temp_ids)
        assert temp_ids.count("b6__rule_campaign_cashback") == 1
        assert any(
            edge.edge_name == "pskg:hasSalesConditionRule"
            and edge.target_temp_id == "b6__rule_campaign_cashback"
            for edge in fragment.edges
        )

        assessment = GraphValidation().assess(
            fragment.model_dump(by_alias=True, mode="json"),
            "digest",
            chunks,
        )

        assert "DERIVED_PROPERTY_REQUIRES_EDGE_EVIDENCE" not in {
            issue.code for issue in assessment.result.errors
        }
        assert assessment.compiled_patch is not None
        rule = next(
            node
            for node in assessment.compiled_patch.nodes
            if node.temp_id == "b6__rule_campaign_cashback"
        )
        assert rule.properties["pskg:ruleType"] == "SALES_CONDITION"



def test_new_business_rule_without_deriving_edge_is_rejected_for_retry():
    text = "Cashback applies to eligible restaurant transactions at 5%."
    payload = {
        "nodes": [
            {
                "tempId": "rule_cashback_details",
                "className": "pskg:BusinessRule",
                "properties": [
                    {
                        "propertyName": "pskg:businessRuleCondition",
                        "value": text,
                        "evidence": [_evidence(0, text)],
                    }
                ],
                "evidence": [_evidence(0, text)],
                "confidence": 1.0,
            }
        ],
        "edges": [],
        "coverage": [
            {
                "chunkIndex": 0,
                "decision": "MAPPED",
                "reason": "Cashback policy mapped",
            }
        ],
        "warnings": [],
    }
    mapper, _ = _mapper(payload)

    with pytest.raises(DirectGraphMappingError) as exc:
        mapper.map_batch(
            batch_payload=_payload([_chunk(content=text)], batch_index=6),
            chunks=[_chunk(content=text)],
        )

    assert any(
        item["code"] == "DERIVED_PROPERTY_REQUIRES_EDGE_EVIDENCE"
        and item.get("nodeTempId") == "b6__rule_cashback_details"
        for item in exc.value.summary["errors"]
    )


def test_existing_context_business_rule_does_not_require_reemitting_deriving_edge():
    text = "The identification document is subject to the existing policy."
    payload = {
        "nodes": [
            {
                "tempId": "doc-id",
                "className": "pskg:RequiredDocument",
                "properties": [
                    {
                        "propertyName": "pskg:documentName",
                        "value": "identification document",
                        "evidence": [_evidence(0, text)],
                    }
                ],
                "evidence": [_evidence(0, text)],
                "confidence": 1.0,
            }
        ],
        "edges": [
            {
                "edgeName": "pskg:documentHasRule",
                "sourceTempId": "doc-id",
                "targetTempId": "rule-existing",
                "evidence": [_evidence(0, text)],
                "confidence": 1.0,
            }
        ],
        "coverage": [
            {
                "chunkIndex": 0,
                "decision": "MAPPED",
                "reason": "Document policy relationship mapped",
            }
        ],
        "warnings": [],
    }
    mapper, _ = _mapper(payload)
    context = (
        '- ref=rule-existing\n'
        '  class=pskg:BusinessRule\n'
        '  identity={"pskg:businessRuleCondition":"Existing policy condition."}'
    )

    fragment = mapper.map_batch(
        batch_payload=_payload([_chunk(content=text)], batch_index=6),
        chunks=[_chunk(content=text)],
        graph_context=context,
    )

    assert any(node.temp_id == "rule-existing" for node in fragment.nodes)
    assert any(
        edge.edge_name == "pskg:documentHasRule"
        and edge.target_temp_id == "rule-existing"
        for edge in fragment.edges
    )
