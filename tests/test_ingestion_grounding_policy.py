import json
from types import SimpleNamespace

from app.core.schemas.ingestion.document import DocumentChunk
from app.core.schemas.ingestion.graph_patch import GraphPatchDraft, GraphPatchFragment
from app.core.schemas.ingestion.semantic_placement import AtomicFactBatch
from app.services.ingestion import use_case as ingestion_use_case
from app.services.ingestion.graph_patch_compiler import GraphPatchCompiler
from app.services.ingestion.loader import OntologyLoader
from app.services.ingestion.registry import OntologyRegistry
from app.services.ingestion.semantic_grounding import SemanticGroundingDecision
from app.services.ingestion.semantic_placement import GeminiAtomicFactExtractor
from app.services.ingestion.source_grounding import SourceGroundingValidator

SOURCE = "test.md"


def _registry() -> OntologyRegistry:
    return OntologyRegistry(
        OntologyLoader.load(
            "app/data/ontology/product_sales_knowledge_graph_base_v3_1.ontology.json"
        )
    )


def _validator(semantic_value_judge=None) -> SourceGroundingValidator:
    return SourceGroundingValidator(
        _registry(),
        semantic_value_judge=semantic_value_judge,
    )


def _chunks(content_by_index: dict[int, str]) -> list[DocumentChunk]:
    return [
        DocumentChunk(
            index=index,
            source=SOURCE,
            section="Rules",
            content=content,
        )
        for index, content in content_by_index.items()
    ]


def _ev(chunk_index: int, text: str) -> dict:
    return {
        "source": SOURCE,
        "chunkIndex": chunk_index,
        "section": "Rules",
        "text": text,
    }


def _coverage(chunk_indexes: list[int]) -> list[dict]:
    return [
        {"chunkIndex": index, "decision": "MAPPED", "reason": "Test fact"}
        for index in chunk_indexes
    ]


def _product_node(*properties) -> dict:
    return {
        "tempId": "product-1",
        "className": "pskg:BankingProduct",
        "properties": list(properties),
        "evidence": [properties[0]["evidence"][0]],
        "confidence": 0.9,
    }


def _rule_node(*properties) -> dict:
    return {
        "tempId": "rule-1",
        "className": "pskg:BusinessRule",
        "properties": list(properties),
        "evidence": [properties[0]["evidence"][0]],
        "confidence": 0.9,
    }


def _validate(validator, nodes, edges, chunks) -> list:
    fragment = GraphPatchFragment(
        nodes=nodes,
        edges=edges,
        coverage=_coverage([chunk.index for chunk in chunks]),
        warnings=[],
    )
    return validator.validate(fragment, chunks)


def _codes(issues) -> set[str]:
    return {issue.code for issue in issues}


def test_property_grounding_classification():
    registry = _registry()
    validator = SourceGroundingValidator(registry)

    def classify(name: str) -> str:
        attribute = registry.get_attribute(name)
        assert attribute is not None
        return validator._property_grounding(name, attribute)

    assert classify("pskg:businessRuleStatus") == "derived"
    assert classify("pskg:requiredDocumentStatus") == "derived"
    assert classify("pskg:ruleType") == "derived"
    assert classify("pskg:businessRuleCondition") == "normalized"
    assert classify("pskg:validityCondition") == "normalized"
    assert classify("pskg:productCode") == "literal"
    assert classify("pskg:fee") == "literal"


def test_runtime_status_requires_no_grounding_and_compiler_overrides():
    condition_text = (
        "Phí rút tiền mặt: 4% số tiền rút, tối thiểu 100.000 VND/giao dịch"
    )
    chunks = _chunks({0: condition_text + "\nRule is published"})
    node = _rule_node(
        {
            "propertyName": "pskg:businessRuleCondition",
            "value": condition_text,
            "evidence": [_ev(0, condition_text)],
        },
        {
            "propertyName": "pskg:businessRuleStatus",
            "value": "Active",
            "evidence": [_ev(0, "Rule is published")],
        },
    )

    issues = _validate(_validator(), [node], [], chunks)
    codes = _codes(issues)
    assert "PROPERTY_VALUE_NOT_GROUNDED" not in codes
    assert "EVIDENCE_TEXT_NOT_IN_SOURCE" not in codes

    draft = GraphPatchDraft.model_validate(
        {
            "nodes": [node],
            "edges": [],
            "coverage": _coverage([0]),
            "warnings": [],
        }
    )
    compiled = GraphPatchCompiler().compile(draft).compiled_patch
    assert compiled is not None
    rule = compiled.nodes[0]
    assert rule.properties["pskg:businessRuleStatus"] == "Draft"
    assert rule.property_evidence["pskg:businessRuleStatus"] == ()


def test_literal_property_fails_when_value_not_in_evidence():
    chunks = _chunks({0: "Thẻ tín dụng Flexi Rewards"})
    node = _product_node(
        {
            "propertyName": "pskg:productCode",
            "value": "CC-FLEXI-001",
            "evidence": [_ev(0, "Thẻ tín dụng Flexi Rewards")],
        }
    )

    issues = _validate(_validator(), [node], [], chunks)

    assert "PROPERTY_VALUE_NOT_GROUNDED" in _codes(issues)


def test_normalized_numeric_value_passes_and_unsupported_fails():
    node_template = {
        "propertyName": "pskg:fee",
        "value": 100000,
        "evidence": [],
    }
    chunks_pass = _chunks({0: "Phí rút tiền mặt 100.000 VND"})
    node_pass = dict(node_template)
    node_pass["evidence"] = [_ev(0, "Phí rút tiền mặt 100.000 VND")]
    issues = _validate(_validator(), [_product_node(node_pass)], [], chunks_pass)
    assert "PROPERTY_VALUE_NOT_GROUNDED" not in _codes(issues)

    chunks_fail = _chunks({0: "Phí rút tiền mặt 50.000 VND"})
    node_fail = dict(node_template)
    node_fail["evidence"] = [_ev(0, "Phí rút tiền mặt 50.000 VND")]
    issues = _validate(_validator(), [_product_node(node_fail)], [], chunks_fail)
    assert "PROPERTY_VALUE_NOT_GROUNDED" in _codes(issues)


def test_normalized_deterministic_pass():
    text = "Phí rút tiền mặt: 4% số tiền rút, tối thiểu 100.000 VND/giao dịch"
    chunks = _chunks({0: text})
    node = _rule_node(
        {
            "propertyName": "pskg:businessRuleCondition",
            "value": text,
            "evidence": [_ev(0, text)],
        }
    )

    issues = _validate(_validator(), [node], [], chunks)

    assert "PROPERTY_VALUE_NOT_GROUNDED" not in _codes(issues)


def test_unrelated_paraphrase_with_verbatim_evidence_fails():
    chunks = _chunks({0: "Phí thường niên 500.000 VND"})
    node = _rule_node(
        {
            "propertyName": "pskg:businessRuleCondition",
            "value": "Hoàn toàn miễn phí thường niên",
            "evidence": [_ev(0, "Phí thường niên 500.000 VND")],
        }
    )

    issues = _validate(_validator(), [node], [], chunks)

    assert "PROPERTY_VALUE_NOT_GROUNDED" in _codes(issues)


def test_numeric_overlap_alone_does_not_prove_equivalence():
    value = "4% số tiền rút, tối thiểu 100.000 VND/giao dịch"
    evidence = "Phí chậm thanh toán 4%/tháng, tối thiểu 100.000đ"
    chunks = _chunks({0: evidence})
    node = _rule_node(
        {
            "propertyName": "pskg:businessRuleCondition",
            "value": value,
            "evidence": [_ev(0, evidence)],
        }
    )

    issues = _validate(_validator(), [node], [], chunks)

    assert "PROPERTY_VALUE_NOT_GROUNDED" in _codes(issues)


class _FakeValueJudge:
    def __init__(self, verdict: str):
        self.verdict = verdict

    def judge_value(self, **kwargs):
        return SemanticGroundingDecision(verdict=self.verdict, reason="fake judge")


def test_normalized_paraphrase_requires_judge_verdict_supported():
    value = "Hoàn toàn miễn phí thường niên"
    evidence = "Phí thường niên 500.000 VND"
    chunks = _chunks({0: evidence})
    node = _rule_node(
        {
            "propertyName": "pskg:businessRuleCondition",
            "value": value,
            "evidence": [_ev(0, evidence)],
        }
    )

    issues = _validate(_validator(_FakeValueJudge("supported")), [node], [], chunks)
    assert "PROPERTY_VALUE_NOT_GROUNDED" not in _codes(issues)

    for verdict in ("unsupported", "unknown"):
        issues = _validate(
            _validator(_FakeValueJudge(verdict)),
            [node],
            [],
            chunks,
        )
        assert "PROPERTY_VALUE_NOT_GROUNDED" in _codes(issues)


def test_fee_string_value_still_rejects():
    text = "Phí rút tiền mặt: 4% số tiền rút, tối thiểu 100.000 VND/giao dịch"
    chunks = _chunks({0: text})
    node = _product_node(
        {
            "propertyName": "pskg:fee",
            "value": "4% số tiền rút, tối thiểu 100.000 VND/giao dịch",
            "evidence": [_ev(0, text)],
        }
    )

    issues = _validate(_validator(), [node], [], chunks)

    assert "PROPERTY_VALUE_NOT_GROUNDED" in _codes(issues)


def test_fee_condition_as_business_rule_passes():
    text = "Phí rút tiền mặt: 4% số tiền rút, tối thiểu 100.000 VND/giao dịch"
    chunks = _chunks({0: "CC-FLEXI-001\n" + text})
    product = _product_node(
        {
            "propertyName": "pskg:productCode",
            "value": "CC-FLEXI-001",
            "evidence": [_ev(0, "CC-FLEXI-001")],
        }
    )
    rule = _rule_node(
        {
            "propertyName": "pskg:businessRuleCondition",
            "value": text,
            "evidence": [_ev(0, text)],
        }
    )
    edge = {
        "edgeName": "pskg:hasSalesConditionRule",
        "sourceTempId": "product-1",
        "targetTempId": "rule-1",
        "evidence": [_ev(0, text)],
        "confidence": 0.9,
    }

    issues = _validate(_validator(), [product, rule], [edge], chunks)
    codes = _codes(issues)

    assert "PROPERTY_VALUE_NOT_GROUNDED" not in codes
    assert "DERIVED_PROPERTY_REQUIRES_EDGE_EVIDENCE" not in codes


def test_atomic_prompt_is_representation_blind():
    prompt = GeminiAtomicFactExtractor._prompt(
        batch_payload={
            "batchIndex": 0,
            "chunkIndexes": [0],
            "contentChars": 10,
            "chunks": [],
        },
        ontology_scope="CLASS: pskg:BankingProduct ...",
        previous_error=None,
    )
    for banned in (
        "pskg:fee",
        "hasSalesConditionRule",
        "hasEligibilityRule",
        "governedByPolicy",
        "BusinessRule",
        "GraphPatchFragment",
    ):
        assert banned not in prompt, banned
    assert "ontology-aware for relevance" in prompt
    assert "representation-blind for placement" in prompt


class _FakeModels:
    def __init__(self):
        self.captured = {}

    def generate_content(self, model, contents, config):
        self.captured["config"] = config
        return SimpleNamespace(
            parsed=None,
            text=json.dumps({"facts": [], "coverage": {"0": "NO_RELEVANT_FACT"}, "warnings": []}),
        )


class _FakeClient:
    def __init__(self):
        self.models = _FakeModels()


def test_extractor_config_uses_native_response_json_schema():
    fake = _FakeClient()
    extractor = GeminiAtomicFactExtractor(client=fake)

    facts = extractor.extract_facts(
        batch_payload={
            "batchIndex": 0,
            "chunkIndexes": [0],
            "contentChars": 10,
            "chunks": [],
        },
        ontology_scope="catalog",
    )

    assert facts is not None
    config = fake.models.captured["config"]
    assert config.response_schema is None
    assert config.response_json_schema == AtomicFactBatch.model_json_schema()
    assert config.response_mime_type == "application/json"


def test_response_schema_fidelity():
    schema = GraphPatchFragment.model_json_schema()
    assert set(schema) >= {
        "$defs",
        "additionalProperties",
        "description",
        "properties",
        "required",
        "title",
        "type",
    }
    assert schema["required"] == ["coverage"]

    defs = schema["$defs"]
    assert set(defs) == {
        "ChunkCoverage",
        "Evidence",
        "ExtractedEdge",
        "ExtractedNode",
        "ExtractedProperty",
    }
    decision = defs["ChunkCoverage"]["properties"]["decision"]
    assert set(decision["enum"]) == {
        "MAPPED",
        "NOT_RELEVANT",
        "NO_RELEVANT_FACT",
        "DUPLICATE_EVIDENCE",
        "UNSUPPORTED_BY_ONTOLOGY",
        "AMBIGUOUS",
        "FAILED",
    }
    assert schema["properties"]["warnings"]["items"]["type"] == "string"
    assert schema["properties"]["warnings"]["items"].get("enum") is None
    evidence_props = defs["Evidence"]["properties"]
    assert set(evidence_props) == {"chunkIndex", "section", "source", "text"}
    value_schema = defs["ExtractedProperty"]["properties"]["value"]
    assert "type" not in value_schema


def test_repair_instruction_is_policy_aware():
    text = ingestion_use_case._repair_instructions(
        {
            "codes": ["PROPERTY_VALUE_NOT_GROUNDED"],
            "coverageNotEvidencedChunkIndexes": [],
            "evidenceTextNotInSourceLocations": [],
            "schemaErrorLocations": [],
        }
    )
    assert "ontology policy" in text.lower()
    assert "copy the exact source wording" not in text.lower()
