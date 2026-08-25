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


def attribute_patch(value, ev, coverage=None) -> dict:
    return {
        "nodes": [
            {
                "tempId": "product-1",
                "className": "pskg:BankingProduct",
                "properties": [
                    {
                        "propertyName": "pskg:productAttributes",
                        "value": value,
                        "evidence": ev,
                    }
                ],
                "evidence": [ev[0]],
                "confidence": 1.0,
            }
        ],
        "edges": [],
        "coverage": coverage
        or [{"chunkIndex": item["chunkIndex"], "decision": "MAPPED", "reason": "Product attribute"} for item in ev],
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


def test_markdown_table_evidence_must_preserve_source_delimiters():
    source = [chunk(0, "| Ma san pham | CC-FLEXI-001 |")]
    patch = product_patch()
    ev = evidence(0, "Ma san pham | CC-FLEXI-001")
    patch["nodes"][0]["evidence"] = ev
    patch["nodes"][0]["properties"] = [
        {"propertyName": "pskg:productCode", "value": "CC-FLEXI-001", "evidence": ev}
    ]

    assessment = GraphPatchValidationService().assess(patch, "artifact", source)

    assert assessment.result.valid_for_extraction is False
    assert "EVIDENCE_TEXT_NOT_IN_SOURCE" in {
        issue.code for issue in assessment.result.errors
    }


def test_evidence_quote_is_case_and_whitespace_sensitive():
    source = [chunk(0, "Product code P-1; effective 01/08/2026")]
    patch = product_patch()
    ev = evidence(0, "product code P-1;  effective 01/08/2026")
    patch["nodes"][0]["evidence"] = ev
    for prop in patch["nodes"][0]["properties"]:
        prop["evidence"] = ev

    assessment = GraphPatchValidationService().assess(patch, "artifact", source)

    assert assessment.result.valid_for_extraction is False
    assert "EVIDENCE_TEXT_NOT_IN_SOURCE" in {
        issue.code for issue in assessment.result.errors
    }


def test_iso_date_value_can_be_supported_by_verbatim_local_date_evidence():
    source = [chunk(0, "Product code P-1; effective 01/08/2026")]

    assessment = GraphPatchValidationService().assess(
        product_patch(),
        "artifact",
        source,
    )

    assert "EVIDENCE_TEXT_NOT_IN_SOURCE" not in {
        issue.code for issue in assessment.result.errors
    }
    assert "PROPERTY_VALUE_NOT_GROUNDED" not in {
        issue.code for issue in assessment.result.errors
    }


def test_markdown_table_rows_support_list_attribute_across_evidence_rows():
    source = [
        chunk(
            0,
            "\n".join(
                [
                    "| Loai the | The tin dung ca nhan |",
                    "| Hang the | Gold |",
                    "| Dong tien thanh toan chinh | VND |",
                ]
            ),
        )
    ]
    ev = [
        {
            "source": "source.md",
            "chunkIndex": 0,
            "section": "Fixture",
            "text": "| Loai the | The tin dung ca nhan |",
        },
        {
            "source": "source.md",
            "chunkIndex": 0,
            "section": "Fixture",
            "text": "| Hang the | Gold |",
        },
        {
            "source": "source.md",
            "chunkIndex": 0,
            "section": "Fixture",
            "text": "| Dong tien thanh toan chinh | VND |",
        },
    ]

    assessment = GraphPatchValidationService().assess(
        attribute_patch(["The tin dung ca nhan", "Gold", "VND"], ev),
        "artifact",
        source,
    )

    assert "EVIDENCE_TEXT_NOT_IN_SOURCE" not in {
        issue.code for issue in assessment.result.errors
    }
    assert "PROPERTY_VALUE_NOT_GROUNDED" not in {
        issue.code for issue in assessment.result.errors
    }
    assert "COVERAGE_NOT_EVIDENCED" not in {
        issue.code for issue in assessment.result.errors
    }


def test_markdown_table_list_attribute_still_requires_verbatim_rows():
    source = [chunk(0, "| Hang the | Gold |")]
    ev = evidence(0, "Hang the | Gold")

    assessment = GraphPatchValidationService().assess(
        attribute_patch(["Gold"], ev),
        "artifact",
        source,
    )

    assert "EVIDENCE_TEXT_NOT_IN_SOURCE" in {
        issue.code for issue in assessment.result.errors
    }


def test_scalar_attribute_can_be_supported_by_combined_valid_evidence_rows():
    source = [
        chunk(
            0,
            "\n".join(
                [
                    "| Loai the | The tin dung ca nhan |",
                    "| Hang the | Gold |",
                    "| Dong tien thanh toan chinh | VND |",
                ]
            ),
        )
    ]
    ev = [
        evidence(0, "| Loai the | The tin dung ca nhan |")[0],
        evidence(0, "| Hang the | Gold |")[0],
        evidence(0, "| Dong tien thanh toan chinh | VND |")[0],
    ]

    assessment = GraphPatchValidationService().assess(
        attribute_patch("The tin dung ca nhan Gold VND", ev),
        "artifact",
        source,
    )

    assert "PROPERTY_VALUE_NOT_GROUNDED" not in {
        issue.code for issue in assessment.result.errors
    }


def test_combined_evidence_does_not_accept_unsupported_paraphrase():
    source = [
        chunk(
            0,
            "\n".join(
                [
                    "| Loai the | The tin dung ca nhan |",
                    "| Hang the | Gold |",
                    "| Dong tien thanh toan chinh | VND |",
                ]
            ),
        )
    ]
    ev = [
        evidence(0, "| Loai the | The tin dung ca nhan |")[0],
        evidence(0, "| Hang the | Gold |")[0],
        evidence(0, "| Dong tien thanh toan chinh | VND |")[0],
    ]

    assessment = GraphPatchValidationService().assess(
        attribute_patch("The tin dung ca nhan Platinum USD", ev),
        "artifact",
        source,
    )

    assert "PROPERTY_VALUE_NOT_GROUNDED" in {
        issue.code for issue in assessment.result.errors
    }


def test_coverage_counts_chunks_that_contribute_to_multi_evidence_property():
    source = [
        chunk(0, "| Hang the | Gold |"),
        chunk(1, "| Dong tien thanh toan chinh | VND |"),
    ]
    ev = [
        evidence(0, "| Hang the | Gold |")[0],
        evidence(1, "| Dong tien thanh toan chinh | VND |")[0],
    ]

    assessment = GraphPatchValidationService().assess(
        attribute_patch(["Gold", "VND"], ev),
        "artifact",
        source,
    )

    assert "COVERAGE_NOT_EVIDENCED" not in {
        issue.code for issue in assessment.result.errors
    }


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


def test_node_level_evidence_cannot_map_all_94_chunks():
    source = [
        chunk(index, f"Chunk {index} generic guidance")
        for index in range(94)
    ]
    source[0]["content"] = "Product code P-1; effective 01/08/2026"
    patch = product_patch(
        coverage=[
            {
                "chunkIndex": index,
                "decision": "MAPPED",
                "reason": "Generic product document",
            }
            for index in range(94)
        ]
    )
    patch["nodes"][0]["evidence"] = [
        {
            "source": "source.md",
            "chunkIndex": index,
            "section": "Fixture",
            "text": source[index]["content"],
        }
        for index in range(94)
    ]

    assessment = GraphPatchValidationService().assess(patch, "artifact", source)

    assert assessment.result.valid_for_extraction is False
    not_evidenced = {
        issue.location
        for issue in assessment.result.errors
        if issue.code == "COVERAGE_NOT_EVIDENCED"
    }
    assert "coverage.1" in not_evidenced
    assert "coverage.93" in not_evidenced


def test_edge_evidence_cannot_map_arbitrary_chunks_without_both_endpoint_facts():
    source = [chunk(0, "Product P-1 has eligibility rule Income >= 10 million")]
    source.extend(chunk(index, f"Unrelated guidance {index}") for index in range(1, 94))
    all_evidence = [
        {
            "source": "source.md",
            "chunkIndex": index,
            "section": "Fixture",
            "text": item["content"],
        }
        for index, item in enumerate(source)
    ]
    patch = product_patch(
        coverage=[
            {"chunkIndex": index, "decision": "MAPPED", "reason": "Claimed edge"}
            for index in range(94)
        ]
    )
    patch["nodes"].append(
        {
            "tempId": "rule-1",
            "className": "pskg:BusinessRule",
            "properties": [
                {
                    "propertyName": "pskg:businessRuleCondition",
                    "value": "Income >= 10 million",
                    "evidence": evidence(0, "Income >= 10 million"),
                }
            ],
            "evidence": evidence(0, "Income >= 10 million"),
            "confidence": 1.0,
        }
    )
    patch["edges"] = [
        {
            "edgeName": "pskg:hasEligibilityRule",
            "sourceTempId": "product-1",
            "targetTempId": "rule-1",
            "evidence": all_evidence,
            "confidence": 1.0,
        }
    ]

    assessment = GraphPatchValidationService().assess(patch, "artifact", source)

    assert assessment.result.valid_for_extraction is False
    assert "EDGE_RELATION_NOT_GROUNDED" in {
        issue.code for issue in assessment.result.errors
    }
    assert any(
        issue.code == "COVERAGE_NOT_EVIDENCED" and issue.location == "coverage.93"
        for issue in assessment.result.errors
    )


def test_edge_endpoint_cooccurrence_without_predicate_is_not_grounded():
    source_text = (
        "Product code P-1; effective 01/08/2026; Income >= 10 million VND/month"
    )
    source = [chunk(0, source_text)]
    patch = product_patch()
    fact_evidence = evidence(0, source_text)
    patch["nodes"].append(
        {
            "tempId": "rule-1",
            "className": "pskg:BusinessRule",
            "properties": [
                {
                    "propertyName": "pskg:businessRuleCondition",
                    "value": "Income >= 10 million VND/month",
                    "evidence": fact_evidence,
                }
            ],
            "evidence": fact_evidence,
            "confidence": 1.0,
        }
    )
    patch["edges"] = [
        {
            "edgeName": "pskg:hasEligibilityRule",
            "sourceTempId": "product-1",
            "targetTempId": "rule-1",
            "evidence": fact_evidence,
            "confidence": 1.0,
        }
    ]

    assessment = GraphPatchValidationService().assess(patch, "artifact", source)

    assert assessment.result.valid_for_extraction is False
    assert any(
        issue.code == "EDGE_RELATION_NOT_GROUNDED"
        for issue in assessment.result.errors
    )


def test_fee_sales_condition_edge_can_be_grounded_by_fee_predicate_text():
    source_text = (
        "Product code P-1; effective 01/08/2026; fee annual fee 699000 VND"
    )
    source = [chunk(0, source_text)]
    patch = product_patch()
    fact_evidence = evidence(0, source_text)
    patch["nodes"][0]["evidence"] = fact_evidence
    for prop in patch["nodes"][0]["properties"]:
        prop["evidence"] = fact_evidence
    patch["nodes"].append(
        {
            "tempId": "annual-fee-rule",
            "className": "pskg:BusinessRule",
            "properties": [
                {
                    "propertyName": "pskg:businessRuleCondition",
                    "value": "annual fee 699000 VND",
                    "evidence": fact_evidence,
                }
            ],
            "evidence": fact_evidence,
            "confidence": 1.0,
        }
    )
    patch["edges"] = [
        {
            "edgeName": "pskg:hasSalesConditionRule",
            "sourceTempId": "product-1",
            "targetTempId": "annual-fee-rule",
            "evidence": fact_evidence,
            "confidence": 1.0,
        }
    ]

    assessment = GraphPatchValidationService().assess(patch, "artifact", source)

    assert "EDGE_RELATION_NOT_GROUNDED" not in {
        issue.code for issue in assessment.result.errors
    }


def test_each_property_evidence_must_support_fee_value():
    source = [chunk(0, "Product code P-1; effective 01/08/2026; fee applies")]
    patch = product_patch()
    patch["nodes"][0]["properties"].append(
        {
            "propertyName": "pskg:fee",
            "value": 500000,
            "evidence": evidence(0, "fee applies"),
        }
    )

    assessment = GraphPatchValidationService().assess(patch, "artifact", source)

    assert assessment.result.valid_for_extraction is False
    assert "PROPERTY_VALUE_NOT_GROUNDED" in {
        issue.code for issue in assessment.result.errors
    }


def test_each_property_evidence_must_support_rule_condition():
    source = [
        chunk(
            0,
            "Product code P-1; effective 01/08/2026; eligibility rule applies",
        )
    ]
    patch = product_patch()
    rule_evidence = evidence(0, "eligibility rule applies")
    patch["nodes"].append(
        {
            "tempId": "rule-1",
            "className": "pskg:BusinessRule",
            "properties": [
                {
                    "propertyName": "pskg:ruleType",
                    "value": "ELIGIBILITY",
                    "evidence": rule_evidence,
                },
                {
                    "propertyName": "pskg:businessRuleCondition",
                    "value": "Income >= 10 million VND/month",
                    "evidence": rule_evidence,
                },
            ],
            "evidence": rule_evidence,
            "confidence": 1.0,
        }
    )
    patch["edges"].append(
        {
            "edgeName": "pskg:hasEligibilityRule",
            "sourceTempId": "product-1",
            "targetTempId": "rule-1",
            "evidence": rule_evidence,
            "confidence": 1.0,
        }
    )

    assessment = GraphPatchValidationService().assess(patch, "artifact", source)

    assert assessment.result.valid_for_extraction is False
    assert any(
        issue.code == "PROPERTY_VALUE_NOT_GROUNDED"
        and issue.property_name == "pskg:businessRuleCondition"
        for issue in assessment.result.errors
    )
