from app.core.schemas.ingestion.identity import NodeIdentity
from app.services.ingestion.validate_graph_patch import GraphPatchValidationService


def product_patch(*, published: bool = True, date_value: str = "2026-08-01"):
    product_properties = [
        {"propertyName": "pskg:productCode", "value": "CC-FLEXI-001"},
        {
            "propertyName": "pskg:bankingProductEffectiveFrom",
            "value": date_value,
        },
    ]
    rule_properties = [
        {
            "propertyName": "pskg:businessRuleCondition",
            "value": "Customer is at least 20 years old",
        }
    ]
    if published:
        product_properties.append(
            {"propertyName": "pskg:bankingProductStatus", "value": "Published"}
        )
        rule_properties.append(
            {"propertyName": "pskg:businessRuleStatus", "value": "Published"}
        )
    return {
        "nodes": [
            {
                "tempId": "product-1",
                "className": "pskg:BankingProduct",
                "properties": product_properties,
                "evidence": [{"source": "flexi.md", "text": "CC-FLEXI-001"}],
                "confidence": 1.0,
            },
            {
                "tempId": "rule-1",
                "className": "pskg:BusinessRule",
                "properties": rule_properties,
                "evidence": [{"source": "flexi.md", "text": "Age 20"}],
                "confidence": 0.9,
            },
        ],
        "edges": [
            {
                "edgeName": "pskg:hasEligibilityRule",
                "sourceTempId": "product-1",
                "targetTempId": "rule-1",
                "evidence": [{"source": "flexi.md", "text": "Eligibility"}],
                "confidence": 0.9,
            }
        ],
        "warnings": [],
    }


def test_missing_published_is_readiness_issue_not_extraction_error():
    assessment = GraphPatchValidationService().assess(
        product_patch(published=False),
        "artifact",
    )

    assert assessment.result.valid_for_extraction is True
    assert assessment.result.valid_for_persistence is False
    assert assessment.result.errors == []
    assert {
        issue.code for issue in assessment.result.readiness_issues
    } == {"ONTOLOGY_RULE_UNSATISFIED"}


def test_non_iso_date_fails_extraction_without_locale_parsing():
    assessment = GraphPatchValidationService().assess(
        product_patch(date_value="01/08/2026"),
        "artifact",
    )

    assert assessment.result.valid_for_extraction is False
    assert "PROPERTY_DATATYPE_MISMATCH" in {
        issue.code for issue in assessment.result.errors
    }


def test_unknown_emitted_fact_fails_extraction():
    patch = product_patch()
    patch["nodes"][0]["properties"].append(
        {"propertyName": "pskg:notReal", "value": "invented"}
    )
    assessment = GraphPatchValidationService().assess(patch, "artifact")

    assert assessment.result.valid_for_extraction is False
    assert "UNKNOWN_PROPERTY" in {issue.code for issue in assessment.result.errors}


def test_source_scoped_identity_fallback_is_ready():
    assessment = GraphPatchValidationService().assess(product_patch(), "artifact")

    assert assessment.result.valid_for_persistence is True
    assert "IDENTITY_UNRESOLVED" not in {
        issue.code for issue in assessment.result.readiness_issues
    }


def test_unresolved_identity_is_reported_after_preflight():
    class AlwaysUnresolvedResolver:
        def resolve(self, **kwargs):
            return NodeIdentity(
                class_name=kwargs["class_name"],
                strategy="unresolved",
                reason="All configured identity policies were exhausted",
            )

    service = GraphPatchValidationService()
    service.identity_resolver = AlwaysUnresolvedResolver()
    assessment = service.assess(product_patch(), "artifact")

    assert assessment.result.valid_for_extraction is True
    assert assessment.result.valid_for_persistence is False
    assert "IDENTITY_UNRESOLVED" in {
        issue.code for issue in assessment.result.readiness_issues
    }


def test_emitted_null_value_fails_extraction_datatype_validation():
    patch = product_patch()
    patch["nodes"][0]["properties"][0]["value"] = None
    assessment = GraphPatchValidationService().assess(patch, "artifact")

    assert assessment.result.valid_for_extraction is False
    assert "PROPERTY_DATATYPE_MISMATCH" in {
        issue.code for issue in assessment.result.errors
    }


def test_technical_names_are_strict_and_extra_fields_are_forbidden():
    patch = product_patch()
    patch["nodes"][0]["className"] = "pskg"
    patch["nodes"][0]["unexpected"] = True
    assessment = GraphPatchValidationService().assess(patch, None)

    assert assessment.result.valid_for_extraction is False
    assert {issue.code for issue in assessment.result.errors} == {
        "TECHNICAL_NAME_INVALID",
        "SCHEMA_INVALID",
    }


def test_compile_failure_never_produces_internal_patch_or_fingerprint():
    patch = product_patch()
    patch["nodes"][0]["properties"].append(
        {"propertyName": "pskg:productCode", "value": "CONFLICT"}
    )
    assessment = GraphPatchValidationService().assess(patch, "artifact")

    assert assessment.compiled_patch is None
    assert assessment.fingerprint is None
    assert {issue.code for issue in assessment.result.errors} == {
        "DUPLICATE_PROPERTY"
    }
