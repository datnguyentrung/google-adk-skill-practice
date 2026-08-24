from copy import deepcopy

from app.core.schemas.ingestion.graph_patch import GraphPatchDraft
from app.services.ingestion.graph_patch_compiler import GraphPatchCompiler


def draft_payload() -> dict:
    ev_product = [{"source": "source.md", "chunkIndex": 0, "text": "Product P-1"}]
    ev_rule = [{"source": "source.md", "chunkIndex": 0, "text": "Age >= 20"}]
    ev_edge = [{"source": "source.md", "chunkIndex": 0, "text": "Eligible at 20"}]
    return {
        "nodes": [
            {
                "tempId": "product-1",
                "className": "pskg:BankingProduct",
                "properties": [
                    {"propertyName": "pskg:productCode", "value": "P-1", "evidence": ev_product},
                    {"propertyName": "pskg:productAttributes", "value": ["first", "second"], "evidence": ev_product},
                ],
                "evidence": ev_product,
                "confidence": 1.0,
            },
            {
                "tempId": "rule-1",
                "className": "pskg:BusinessRule",
                "properties": [],
                "evidence": ev_rule,
                "confidence": 0.9,
            },
        ],
        "edges": [
            {
                "edgeName": "pskg:hasEligibilityRule",
                "sourceTempId": "product-1",
                "targetTempId": "rule-1",
                "evidence": ev_edge,
                "confidence": 0.9,
            }
        ],
        "coverage": [{"chunkIndex": 0, "decision": "MAPPED", "reason": "Fixture facts"}],
        "warnings": [],
    }


def test_exact_duplicate_property_is_deduplicated():
    payload = draft_payload()
    payload["nodes"][0]["properties"].append(
        {"propertyName": "pskg:productCode", "value": "P-1", "evidence": [{"source": "source.md", "chunkIndex": 0, "text": "Product P-1"}]}
    )

    result = GraphPatchCompiler().compile(GraphPatchDraft.model_validate(payload))

    assert result.errors == ()
    assert result.compiled_patch is not None
    assert result.compiled_patch.nodes[0].properties["pskg:productCode"] == "P-1"


def test_conflicting_duplicate_property_fails_compilation():
    payload = draft_payload()
    payload["nodes"][0]["properties"].append(
        {"propertyName": "pskg:productCode", "value": "P-2", "evidence": [{"source": "source.md", "chunkIndex": 0, "text": "Product P-1"}]}
    )

    result = GraphPatchCompiler().compile(GraphPatchDraft.model_validate(payload))

    assert result.compiled_patch is None
    assert {issue.code for issue in result.errors} == {"DUPLICATE_PROPERTY"}


def test_duplicate_comparison_is_json_type_exact():
    payload = draft_payload()
    payload["nodes"][0]["properties"].extend(
        [
            {"propertyName": "pskg:fee", "value": 1, "evidence": [{"source": "source.md", "chunkIndex": 0, "text": "Product P-1"}]},
            {"propertyName": "pskg:fee", "value": 1.0, "evidence": [{"source": "source.md", "chunkIndex": 0, "text": "Product P-1"}]},
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
        {"propertyName": "pskg:ruleType", "value": "POLICY", "evidence": [{"source": "source.md", "chunkIndex": 0, "text": "Age >= 20"}]}
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


def test_exact_duplicate_property_merges_distinct_evidence():
    payload = draft_payload()
    payload["nodes"][0]["properties"].append(
        {
            "propertyName": "pskg:productCode",
            "value": "P-1",
            "evidence": [{"source": "source.md", "chunkIndex": 1, "text": "P-1 repeated"}],
        }
    )
    payload["coverage"].append(
        {"chunkIndex": 1, "decision": "MAPPED", "reason": "Repeated product code"}
    )
    result = GraphPatchCompiler().compile(GraphPatchDraft.model_validate(payload))
    assert result.compiled_patch is not None
    evidence = result.compiled_patch.nodes[0].property_evidence["pskg:productCode"]
    assert {item.chunk_index for item in evidence} == {0, 1}


def test_fingerprint_changes_when_evidence_chunk_index_changes():
    compiler = GraphPatchCompiler()
    first_payload = draft_payload()
    first = compiler.compile(GraphPatchDraft.model_validate(first_payload)).compiled_patch
    assert first is not None

    second_payload = deepcopy(first_payload)
    for node in second_payload["nodes"]:
        for ev in node["evidence"]:
            ev["chunkIndex"] = 1
        for prop in node["properties"]:
            for ev in prop["evidence"]:
                ev["chunkIndex"] = 1
    for edge in second_payload["edges"]:
        for ev in edge["evidence"]:
            ev["chunkIndex"] = 1
    second_payload["coverage"] = [
        {"chunkIndex": 1, "decision": "MAPPED", "reason": "Fixture facts"}
    ]
    second = compiler.compile(GraphPatchDraft.model_validate(second_payload)).compiled_patch
    assert second is not None
    assert compiler.fingerprint(first, "artifact") != compiler.fingerprint(second, "artifact")
