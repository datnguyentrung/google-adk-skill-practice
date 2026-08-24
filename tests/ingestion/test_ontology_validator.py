from app.services.ingestion.validate_graph_patch import GraphPatchValidationService


def node_patch(class_name: str, properties: list[dict]) -> dict:
    evidence = [{"source": "source.md", "chunkIndex": 0, "text": "Evidence"}]
    normalized_properties = [
        {**item, "evidence": item.get("evidence", evidence)}
        for item in properties
    ]
    return {
        "nodes": [
            {
                "tempId": "node-1",
                "className": class_name,
                "properties": normalized_properties,
                "evidence": evidence,
                "confidence": 1.0,
            }
        ],
        "edges": [],
        "coverage": [{"chunkIndex": 0, "decision": "MAPPED", "reason": "Fixture evidence"}],
        "warnings": [],
    }


def test_unknown_class_is_extraction_error():
    assessment = GraphPatchValidationService().assess(
        node_patch("pskg:SomethingDoesNotExist", []),
        None,
    )
    assert {issue.code for issue in assessment.result.errors} == {"UNKNOWN_CLASS"}


def test_unknown_property_and_domain_mismatch_are_extraction_errors():
    service = GraphPatchValidationService()
    unknown = service.assess(
        node_patch(
            "pskg:BankingProduct",
            [{"propertyName": "pskg:notRealProperty", "value": "hello"}],
        ),
        None,
    )
    wrong_domain = service.assess(
        node_patch(
            "pskg:BankingProduct",
            [{"propertyName": "pskg:priority", "value": 10}],
        ),
        None,
    )
    assert {issue.code for issue in unknown.result.errors} == {"UNKNOWN_PROPERTY"}
    assert {issue.code for issue in wrong_domain.result.errors} == {
        "PROPERTY_DOMAIN_MISMATCH"
    }


def test_missing_required_fact_is_persistence_readiness_issue():
    assessment = GraphPatchValidationService().assess(
        node_patch("pskg:BankingProduct", []),
        None,
    )
    assert assessment.result.valid_for_extraction is True
    assert assessment.result.valid_for_persistence is False
    assert "ONTOLOGY_RULE_UNSATISFIED" in {
        issue.code for issue in assessment.result.readiness_issues
    }
