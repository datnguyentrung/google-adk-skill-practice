import json
from pathlib import Path
from types import SimpleNamespace

from app.core.schemas.ingestion.document import DocumentChunk
from app.core.schemas.ingestion.graph_patch import GraphPatchDraft
from app.core.schemas.ingestion.validation import ValidationCode, ValidationIssue
from app.services.ingestion import use_case as ingestion_use_case
from app.services.ingestion.loader import OntologyLoader
from app.services.ingestion.registry import OntologyRegistry
from app.services.ingestion.relationship_reconciliation import (
    RelationshipReconciler,
    reconciliation_triggered,
)
from app.services.ingestion.staged_ingestion import IngestionWorkspaceService
from app.services.ingestion.validate_graph_patch import GraphPatchValidationService


SOURCE = "test.md"


class _FakeArtifact:
    def __init__(self, text: str):
        self.inline_data = None
        self.text = text


class FakeToolContext:
    def __init__(self):
        self.state = {}

    async def load_artifact(self, filename: str):
        return _FakeArtifact("unused")

    async def save_artifact(self, filename, artifact, **kwargs):
        return 1


class FakeModels:
    def __init__(self, response_text: str):
        self.response_text = response_text
        self.captured = {}

    def generate_content(self, model, contents, config):
        self.captured["config"] = config
        self.captured["prompt"] = contents
        return SimpleNamespace(parsed=None, text=self.response_text)


class FakeClient:
    def __init__(self, response_text: str):
        self.models = FakeModels(response_text)


class SequenceModels:
    def __init__(self, response_texts: list[str]):
        self.response_texts = list(response_texts)
        self.captured: list[str] = []

    def generate_content(self, model, contents, config):
        self.captured.append(contents)
        text = self.response_texts.pop(0)
        return SimpleNamespace(parsed=None, text=text)


class SequenceClient:
    def __init__(self, response_texts: list[str]):
        self.models = SequenceModels(response_texts)


def _registry() -> OntologyRegistry:
    return OntologyRegistry(
        OntologyLoader.load(
            "app/data/ontology/product_sales_knowledge_graph_base_v3_1.ontology.json"
        )
    )


def _chunks() -> list[DocumentChunk]:
    return [
        DocumentChunk(
            index=0,
            source=SOURCE,
            section="Product",
            content="CC-FLEXI-001 2026-08-01",
        ),
        DocumentChunk(
            index=1,
            source=SOURCE,
            section="Eligibility",
            content="Điều kiện: thu nhập từ 10 triệu VND/tháng",
        ),
    ]


def _canonical_draft() -> GraphPatchDraft:
    evidence = {
        "source": SOURCE,
        "chunkIndex": 0,
        "section": "Product",
        "text": "CC-FLEXI-001",
    }
    date_evidence = {
        "source": SOURCE,
        "chunkIndex": 0,
        "section": "Product",
        "text": "2026-08-01",
    }
    return GraphPatchDraft(
        nodes=[
            {
                "tempId": "product-1",
                "className": "pskg:BankingProduct",
                "properties": [
                    {
                        "propertyName": "pskg:productCode",
                        "value": "CC-FLEXI-001",
                        "evidence": [evidence],
                    },
                    {
                        "propertyName": "pskg:bankingProductEffectiveFrom",
                        "value": "2026-08-01",
                        "evidence": [date_evidence],
                    },
                ],
                "evidence": [evidence],
                "confidence": 0.9,
            }
        ],
        edges=[],
        coverage=[
            {"chunkIndex": 0, "decision": "MAPPED", "reason": "Product code"},
            {
                "chunkIndex": 1,
                "decision": "NO_RELEVANT_FACT",
                "reason": "Not mapped in the first pass",
            },
        ],
        warnings=[],
    )


def _eligibility_evidence() -> dict:
    return {
        "source": SOURCE,
        "chunkIndex": 1,
        "section": "Eligibility",
        "text": "thu nhập từ 10 triệu VND/tháng",
    }


def _eligibility_candidate_json() -> str:
    evidence = _eligibility_evidence()
    return json.dumps(
        {
            "nodes": [
                {
                    "tempId": "rule-1",
                    "className": "pskg:BusinessRule",
                    "properties": [
                        {
                            "propertyName": "pskg:businessRuleCondition",
                            "value": "Thu nhập từ 10 triệu VND/tháng",
                            "evidence": [evidence],
                        }
                    ],
                    "evidence": [evidence],
                    "confidence": 0.9,
                }
            ],
            "edges": [
                {
                    "edgeName": "pskg:hasEligibilityRule",
                    "sourceTempId": "product-1",
                    "targetTempId": "rule-1",
                    "evidence": [evidence],
                    "confidence": 0.9,
                }
            ],
            "coverage": [
                {
                    "chunkIndex": 1,
                    "decision": "MAPPED",
                    "reason": "Eligibility condition grounded",
                }
            ],
            "warnings": [],
        },
        ensure_ascii=False,
    )


def _chunks_full() -> list[DocumentChunk]:
    return [
        DocumentChunk(
            index=index,
            source=SOURCE,
            section=f"Section {index}",
            content=(
                "CC-FLEXI-001 2026-08-01"
                if index == 0
                else (
                    "Điều kiện: thu nhập từ 10 triệu VND/tháng"
                    if index == 9
                    else f"Unrelated body text for section {index}."
                )
            ),
        )
        for index in range(10)
    ]


def _canonical_draft_full() -> GraphPatchDraft:
    evidence = {
        "source": SOURCE,
        "chunkIndex": 0,
        "section": "Section 0",
        "text": "CC-FLEXI-001",
    }
    date_evidence = {
        "source": SOURCE,
        "chunkIndex": 0,
        "section": "Section 0",
        "text": "2026-08-01",
    }
    return GraphPatchDraft(
        nodes=[
            {
                "tempId": "product-1",
                "className": "pskg:BankingProduct",
                "properties": [
                    {
                        "propertyName": "pskg:productCode",
                        "value": "CC-FLEXI-001",
                        "evidence": [evidence],
                    },
                    {
                        "propertyName": "pskg:bankingProductEffectiveFrom",
                        "value": "2026-08-01",
                        "evidence": [date_evidence],
                    },
                ],
                "evidence": [evidence],
                "confidence": 0.9,
            }
        ],
        edges=[],
        coverage=[
            {
                "chunkIndex": index,
                "decision": "MAPPED" if index == 0 else "NO_RELEVANT_FACT",
                "reason": (
                    "Product code" if index == 0 else "No fact in this chunk"
                ),
            }
            for index in range(10)
        ],
        warnings=[],
    )


def _full_document_candidate_json() -> str:
    evidence = {
        "source": SOURCE,
        "chunkIndex": 9,
        "section": "Section 9",
        "text": "thu nhập từ 10 triệu VND/tháng",
    }
    return json.dumps(
        {
            "nodes": [
                {
                    "tempId": "rule-1",
                    "className": "pskg:BusinessRule",
                    "properties": [
                        {
                            "propertyName": "pskg:businessRuleCondition",
                            "value": "Thu nhập từ 10 triệu VND/tháng",
                            "evidence": [evidence],
                        }
                    ],
                    "evidence": [evidence],
                    "confidence": 0.9,
                }
            ],
            "edges": [
                {
                    "edgeName": "pskg:hasEligibilityRule",
                    "sourceTempId": "product-1",
                    "targetTempId": "rule-1",
                    "evidence": [evidence],
                    "confidence": 0.9,
                }
            ],
            "coverage": [
                {
                    "chunkIndex": 9,
                    "decision": "MAPPED",
                    "reason": "Eligibility condition grounded",
                }
            ],
            "warnings": [],
        },
        ensure_ascii=False,
    )


def _empty_candidate_json() -> str:
    return json.dumps(
        {
            "nodes": [],
            "edges": [],
            "coverage": [
                {
                    "chunkIndex": 1,
                    "decision": "NO_RELEVANT_FACT",
                    "reason": "No eligibility evidence found",
                }
            ],
            "warnings": [],
        },
        ensure_ascii=False,
    )


def _workspace_with_finalized_draft():
    context = FakeToolContext()
    context.state[ingestion_use_case.ARTIFACT_DIGEST_STATE_KEY] = "digest"
    chunks = _chunks()
    workspace = IngestionWorkspaceService().begin(
        artifact_name=SOURCE,
        provenance=ingestion_use_case._current_provenance(context),
        chunks=chunks,
    )
    workspace.finalized_patch = _canonical_draft().model_dump(
        by_alias=True,
        mode="json",
    )
    ingestion_use_case._store_workspace(context, workspace)
    return context, workspace, chunks


def _reconcile_with_fake(response_text: str, **kwargs):
    service = GraphPatchValidationService()
    chunks = _chunks()
    draft = _canonical_draft()
    assessment = service.assess(draft, "digest", chunks)
    reconciler = RelationshipReconciler(
        client=FakeClient(response_text),
        max_passes=kwargs.pop("max_passes", 1),
        **kwargs,
    )
    return reconciler.reconcile(
        merged_draft=draft,
        compiled_patch=assessment.compiled_patch,
        readiness_issues=assessment.result.readiness_issues,
        chunks=chunks,
        validation_service=service,
        artifact_digest="digest",
        registry=service.validator.registry,
    )


def test_trigger_only_for_edge_rule_gaps():
    assert reconciliation_triggered([]) is False
    edge_gap = ValidationIssue(
        code=ValidationCode.ONTOLOGY_RULE_UNSATISFIED,
        message="Edge pskg:hasEligibilityRule is required",
        location="nodes.0.edges.pskg:hasEligibilityRule",
        node_temp_id="product-1",
        edge_name="pskg:hasEligibilityRule",
    )
    property_gap = ValidationIssue(
        code=ValidationCode.ONTOLOGY_RULE_UNSATISFIED,
        message="Property pskg:x must occur",
        location="nodes.0.properties.pskg:x",
        node_temp_id="product-1",
        property_name="pskg:x",
    )
    assert reconciliation_triggered([edge_gap]) is True
    assert reconciliation_triggered([property_gap]) is False
    assert reconciliation_triggered([edge_gap, property_gap]) is False


def test_a_missing_relationship_reconciled_with_evidence():
    outcome = _reconcile_with_fake(_eligibility_candidate_json())

    assert outcome.reconciled is True
    assert outcome.passes_used == 1
    assert outcome.draft is not None
    edges = [
        edge
        for edge in outcome.draft.edges
        if edge.edge_name == "pskg:hasEligibilityRule"
    ]
    assert len(edges) == 1
    assert edges[0].source_temp_id == "product-1"


def test_c_coverage_flips_from_no_relevant_fact_to_mapped():
    outcome = _reconcile_with_fake(_eligibility_candidate_json())

    assert outcome.draft is not None
    by_index = {item.chunk_index: item.decision for item in outcome.draft.coverage}
    assert by_index[1] == "MAPPED"


def test_b_no_evidence_means_no_invented_edge():
    no_op = json.dumps(
        {
            "nodes": [],
            "edges": [],
            "coverage": [
                {
                    "chunkIndex": 1,
                    "decision": "AMBIGUOUS",
                    "reason": "Evidence insufficient",
                }
            ],
            "warnings": [],
        }
    )
    outcome = _reconcile_with_fake(no_op)

    assert outcome.reconciled is False
    assert outcome.exhausted is True
    assert any(
        issue.code == ValidationCode.COVERAGE_NOT_EVIDENCED
        for issue in outcome.issues
    )


def test_e_wrong_edge_rejected_by_validator():
    evidence = _eligibility_evidence()
    wrong = json.dumps(
        {
            "nodes": [],
            "edges": [
                {
                    "edgeName": "pskg:requiresDocument",
                    "sourceTempId": "product-1",
                    "targetTempId": "rule-1",
                    "evidence": [evidence],
                    "confidence": 0.9,
                }
            ],
            "coverage": [
                {
                    "chunkIndex": 1,
                    "decision": "MAPPED",
                    "reason": "Wrong edge",
                }
            ],
            "warnings": [],
        },
        ensure_ascii=False,
    )
    outcome = _reconcile_with_fake(wrong)

    assert outcome.reconciled is False
    assert outcome.exhausted is True
    assert any(
        issue.code == ValidationCode.DANGLING_REFERENCE
        for issue in outcome.issues
    )


def test_f_budget_exhausted_with_invalid_llm_output():
    outcome = _reconcile_with_fake("not json", max_passes=1)

    assert outcome.reconciled is False
    assert outcome.exhausted is True
    assert outcome.passes_used == 1


def test_d_satisfied_constraint_does_not_call_llm():
    service = GraphPatchValidationService()
    chunks = _chunks()
    draft = _canonical_draft()
    evidence = _eligibility_evidence()
    draft = GraphPatchDraft(
        nodes=[
            *draft.nodes,
            {
                "tempId": "rule-1",
                "className": "pskg:BusinessRule",
                "properties": [
                    {
                        "propertyName": "pskg:businessRuleCondition",
                        "value": "Thu nhập từ 10 triệu VND/tháng",
                        "evidence": [evidence],
                    }
                ],
                "evidence": [evidence],
                "confidence": 0.9,
            },
        ],
        edges=[
            {
                "edgeName": "pskg:hasEligibilityRule",
                "sourceTempId": "product-1",
                "targetTempId": "rule-1",
                "evidence": [evidence],
                "confidence": 0.9,
            }
        ],
        coverage=[
            {"chunkIndex": 0, "decision": "MAPPED", "reason": "Product code"},
            {
                "chunkIndex": 1,
                "decision": "MAPPED",
                "reason": "Eligibility rule",
            },
        ],
        warnings=[],
    )
    assessment = service.assess(draft, "digest", chunks)
    assert assessment.result.valid_for_persistence is True
    fake = FakeClient("{}")
    reconciler = RelationshipReconciler(client=fake, max_passes=1)

    outcome = reconciler.reconcile(
        merged_draft=draft,
        compiled_patch=assessment.compiled_patch,
        readiness_issues=assessment.result.readiness_issues,
        chunks=chunks,
        validation_service=service,
        artifact_digest="digest",
        registry=service.validator.registry,
    )

    assert outcome.reconciled is False
    assert outcome.passes_used == 0
    assert fake.models.captured == {}


def test_g_no_hard_coded_ontology_identifiers():
    source = Path(
        "app/services/ingestion/relationship_reconciliation.py"
    ).read_text(encoding="utf-8")
    for banned in (
        "BusinessRule",
        "hasEligibilityRule",
        "hasSalesConditionRule",
        "governedByPolicy",
        "Flexi",
        "eligibility",
        "fee",
        "campaign",
        "chunk 10",
    ):
        assert banned not in source, banned


def test_use_case_helper_updates_workspace_on_success(monkeypatch):
    context, workspace, _ = _workspace_with_finalized_draft()
    reconciler = RelationshipReconciler(
        client=FakeClient(_eligibility_candidate_json()),
        max_passes=1,
    )
    monkeypatch.setattr(
        ingestion_use_case,
        "_get_relationship_reconciler",
        lambda: reconciler,
    )

    result = ingestion_use_case._reconcile_relationship_gaps(
        workspace.ingestion_id,
        context,
    )

    assert result["success"] is True
    assert result["reconciled"] is True
    updated = ingestion_use_case._load_workspace(context)
    assert updated is not None
    assert updated.validated_fingerprint is not None
    edges = [
        edge
        for edge in updated.finalized_patch["edges"]
        if edge["edgeName"] == "pskg:hasEligibilityRule"
    ]
    assert len(edges) == 1


def test_full_document_context_includes_distant_skipped_chunks():
    service = GraphPatchValidationService()
    chunks = _chunks_full()
    draft = _canonical_draft_full()
    assessment = service.assess(draft, "digest", chunks)
    client = SequenceClient([_full_document_candidate_json()])
    reconciler = RelationshipReconciler(client=client, max_passes=1)

    outcome = reconciler.reconcile(
        merged_draft=draft,
        compiled_patch=assessment.compiled_patch,
        readiness_issues=assessment.result.readiness_issues,
        chunks=chunks,
        validation_service=service,
        artifact_digest="digest",
        registry=service.validator.registry,
    )

    assert outcome.reconciled is True
    prompt = client.models.captured[0]
    assert "[CHUNK 9]" in prompt
    assert "thu nhập từ 10 triệu VND/tháng" in prompt
    assert "Unrelated body text for section 1." in prompt


def test_repair_pass_receives_previous_attempt_feedback():
    service = GraphPatchValidationService()
    chunks = _chunks()
    draft = _canonical_draft()
    assessment = service.assess(draft, "digest", chunks)
    client = SequenceClient(
        [_empty_candidate_json(), _eligibility_candidate_json()]
    )
    reconciler = RelationshipReconciler(client=client, max_passes=2)

    outcome = reconciler.reconcile(
        merged_draft=draft,
        compiled_patch=assessment.compiled_patch,
        readiness_issues=assessment.result.readiness_issues,
        chunks=chunks,
        validation_service=service,
        artifact_digest="digest",
        registry=service.validator.registry,
    )

    assert outcome.reconciled is True
    assert outcome.passes_used == 2
    assert len(client.models.captured) == 2
    assert "did not satisfy validation" in client.models.captured[1]
    assert "previous reconciliation attempt" in client.models.captured[1]
    assert "ONTOLOGY_RULE_UNSATISFIED" in client.models.captured[1]
