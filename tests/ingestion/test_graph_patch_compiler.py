from copy import deepcopy

from app.core.schemas.ingestion.graph_patch import GraphPatchDraft
from app.services.ingestion.graph_patch_compiler import GraphPatchCompiler


def draft_payload() -> dict:
    return {
        "nodes": [
            {
                "tempId": "product-1",
                "className": "pskg:BankingProduct",
                "properties": [
                    {"propertyName": "pskg:productCode", "value": "P-1"},
                    {
                        "propertyName": "pskg:productAttributes",
                        "value": ["first", "second"],
                    },
                ],
                "evidence": [{"source": "source.md", "text": "Product P-1"}],
                "confidence": 1.0,
            },
            {
                "tempId": "rule-1",
                "className": "pskg:BusinessRule",
                "properties": [],
                "evidence": [{"source": "source.md", "text": "Age >= 20"}],
                "confidence": 0.9,
            },
        ],
        "edges": [
            {
                "edgeName": "pskg:hasEligibilityRule",
                "sourceTempId": "product-1",
                "targetTempId": "rule-1",
                "evidence": [{"source": "source.md", "text": "Eligible at 20"}],
                "confidence": 0.9,
            }
        ],
        "warnings": [],
    }


def test_exact_duplicate_property_is_deduplicated():
    payload = draft_payload()
    payload["nodes"][0]["properties"].append(
        {"propertyName": "pskg:productCode", "value": "P-1"}
    )

    result = GraphPatchCompiler().compile(GraphPatchDraft.model_validate(payload))

    assert result.errors == ()
    assert result.compiled_patch is not None
    assert result.compiled_patch.nodes[0].properties["pskg:productCode"] == "P-1"


def test_conflicting_duplicate_property_fails_compilation():
    payload = draft_payload()
    payload["nodes"][0]["properties"].append(
        {"propertyName": "pskg:productCode", "value": "P-2"}
    )

    result = GraphPatchCompiler().compile(GraphPatchDraft.model_validate(payload))

    assert result.compiled_patch is None
    assert {issue.code for issue in result.errors} == {"DUPLICATE_PROPERTY"}


def test_duplicate_comparison_is_json_type_exact():
    payload = draft_payload()
    payload["nodes"][0]["properties"].extend(
        [
            {"propertyName": "pskg:fee", "value": 1},
            {"propertyName": "pskg:fee", "value": 1.0},
        ]
    )

    result = GraphPatchCompiler().compile(GraphPatchDraft.model_validate(payload))

    assert result.compiled_patch is None
    assert {issue.code for issue in result.errors} == {"DUPLICATE_PROPERTY"}


def test_duplicate_temp_id_and_dangling_reference_fail_compilation():
    payload = draft_payload()
    payload["nodes"][1]["tempId"] = "product-1"
    payload["edges"][0]["targetTempId"] = "missing"

    result = GraphPatchCompiler().compile(GraphPatchDraft.model_validate(payload))

    assert result.compiled_patch is None
    assert {issue.code for issue in result.errors} == {
        "DUPLICATE_TEMP_ID",
        "DANGLING_REFERENCE",
    }


def test_rule_type_is_derived_and_conflict_is_rejected():
    compiler = GraphPatchCompiler()
    derived = compiler.compile(GraphPatchDraft.model_validate(draft_payload()))
    assert derived.compiled_patch is not None
    assert (
        derived.compiled_patch.nodes[1].properties["pskg:ruleType"]
        == "ELIGIBILITY"
    )

    payload = draft_payload()
    payload["nodes"][1]["properties"] = [
        {"propertyName": "pskg:ruleType", "value": "POLICY"}
    ]
    conflict = compiler.compile(GraphPatchDraft.model_validate(payload))
    assert conflict.compiled_patch is None
    assert {issue.code for issue in conflict.errors} == {"SEMANTIC_CONFLICT"}


def test_fingerprint_is_stable_for_unordered_parts_but_preserves_value_order():
    compiler = GraphPatchCompiler()
    payload = draft_payload()
    first = compiler.compile(GraphPatchDraft.model_validate(payload)).compiled_patch
    assert first is not None

    reordered = deepcopy(payload)
    reordered["nodes"].reverse()
    reordered["nodes"][1]["properties"].reverse()
    second = compiler.compile(
        GraphPatchDraft.model_validate(reordered)
    ).compiled_patch
    assert second is not None
    assert compiler.fingerprint(first, "artifact-a") == compiler.fingerprint(
        second, "artifact-a"
    )

    changed_value_order = deepcopy(payload)
    changed_value_order["nodes"][0]["properties"][1]["value"].reverse()
    third = compiler.compile(
        GraphPatchDraft.model_validate(changed_value_order)
    ).compiled_patch
    assert third is not None
    assert compiler.fingerprint(first, "artifact-a") != compiler.fingerprint(
        third, "artifact-a"
    )


def test_fingerprint_binds_artifact_ontology_and_compiler_version(tmp_path):
    ontology_path = GraphPatchCompiler().ontology_path
    copied_ontology = tmp_path / "ontology.json"
    copied_ontology.write_bytes(ontology_path.read_bytes())
    payload = GraphPatchDraft.model_validate(draft_payload())

    compiler_v1 = GraphPatchCompiler(copied_ontology, schema_version="1")
    patch = compiler_v1.compile(payload).compiled_patch
    assert patch is not None
    digest_a = compiler_v1.fingerprint(patch, "artifact-a")
    assert digest_a != compiler_v1.fingerprint(patch, "artifact-b")

    compiler_v2 = GraphPatchCompiler(copied_ontology, schema_version="2")
    assert digest_a != compiler_v2.fingerprint(patch, "artifact-a")

    copied_ontology.write_bytes(copied_ontology.read_bytes() + b"\n")
    changed_ontology = GraphPatchCompiler(copied_ontology, schema_version="1")
    assert digest_a != changed_ontology.fingerprint(patch, "artifact-a")
