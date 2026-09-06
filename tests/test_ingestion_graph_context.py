import asyncio
from pathlib import Path
from types import SimpleNamespace

from app.core.schemas.ingestion.document import DocumentChunk
from app.core.schemas.ingestion.graph_patch import (
    Evidence,
    ExtractedEdge,
    GraphPatchDraft,
    GraphPatchFragment,
)
from app.services.ingestion import use_case as ingestion_use_case
from app.services.ingestion.document_reader import DocumentReader
from app.services.ingestion.loader import OntologyLoader
from app.services.ingestion.registry import OntologyRegistry
from app.services.ingestion.staged_ingestion import IngestionWorkspaceService
from app.services.ingestion.use_case import IngestionUseCase
from app.services.ingestion.graph_validation import GraphValidation

SOURCE = "test.md"


def _ontology_registry() -> OntologyRegistry:
    return OntologyRegistry(
        OntologyLoader.load(
            "app/data/ontology/product_sales_knowledge_graph_base_v3_1.ontology.json"
        )
    )


class _FakeArtifact:
    def __init__(self, text: str):
        self.inline_data = None
        self.text = text


class FakeToolContext:
    def __init__(self, artifacts=None):
        self.state = {}
        self._artifacts = artifacts or {}
        self.saved_artifacts = {}

    async def load_artifact(self, filename: str):
        text = self._artifacts.get(filename)
        if text is None:
            return None
        return _FakeArtifact(text)

    async def save_artifact(self, filename: str, artifact, **kwargs):
        self.saved_artifacts[filename] = artifact
        return 1


def _workspace_context(chunk_count: int, contents=None):
    context = FakeToolContext()
    context.state[ingestion_use_case.ARTIFACT_DIGEST_STATE_KEY] = "digest"
    contents = contents or {}
    chunks = [
        DocumentChunk(
            index=i,
            source=SOURCE,
            section=f"Section {i}",
            content=contents.get(i, f"Section {i} body text {i}."),
        )
        for i in range(chunk_count)
    ]
    workspace = IngestionWorkspaceService().begin(
        artifact_name=SOURCE,
        provenance=ingestion_use_case._current_provenance(context),
        chunks=chunks,
    )
    ingestion_use_case._store_workspace(context, workspace)
    return context, workspace


def _product_fragment() -> GraphPatchFragment:
    evidence = {
        "source": SOURCE,
        "chunkIndex": 0,
        "section": "Section 0",
        "text": "CC-FLEXI-001",
    }
    return GraphPatchFragment(
        nodes=[
            {
                "tempId": "product-1",
                "className": "pskg:BankingProduct",
                "properties": [
                    {
                        "propertyName": "pskg:productCode",
                        "value": "CC-FLEXI-001",
                        "evidence": [evidence],
                    }
                ],
                "evidence": [evidence],
                "confidence": 0.9,
            }
        ],
        edges=[],
        coverage=[
            {
                "chunkIndex": i,
                "decision": "MAPPED" if i == 0 else "NO_RELEVANT_FACT",
                "reason": "Product code" if i == 0 else "No fact in this chunk",
            }
            for i in range(5)
        ],
        warnings=[],
    )


def _rule_fragment() -> GraphPatchFragment:
    evidence = {
        "source": SOURCE,
        "chunkIndex": 10,
        "section": "Section 10",
        "text": "Condition X",
    }
    return GraphPatchFragment(
        nodes=[
            {
                "tempId": "rule-1",
                "className": "pskg:BusinessRule",
                "properties": [
                    {
                        "propertyName": "pskg:businessRuleCondition",
                        "value": "Condition X",
                        "evidence": [evidence],
                    }
                ],
                "evidence": [evidence],
                "confidence": 0.9,
            }
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
            {
                "chunkIndex": 10,
                "decision": "MAPPED",
                "reason": "Eligibility condition",
            },
            {
                "chunkIndex": 11,
                "decision": "NO_RELEVANT_FACT",
                "reason": "No fact in this chunk",
            },
        ],
        warnings=[],
    )


def _generic_fragment() -> GraphPatchFragment:
    def evidence(chunk_index: int, text: str):
        return {
            "source": SOURCE,
            "chunkIndex": chunk_index,
            "section": f"Section {chunk_index}",
            "text": text,
        }

    return GraphPatchFragment(
        nodes=[
            {
                "tempId": "node-a",
                "className": "ex:Entity",
                "properties": [
                    {
                        "propertyName": "ex:p1",
                        "value": "A",
                        "evidence": [evidence(0, "A")],
                    },
                    {
                        "propertyName": "ex:p2",
                        "value": "bad",
                        "evidence": [evidence(1, "bad")],
                    },
                    {
                        "propertyName": "ex:p3",
                        "value": "C",
                        "evidence": [evidence(2, "C")],
                    },
                ],
                "evidence": [evidence(0, "A")],
                "confidence": 0.9,
            },
            {
                "tempId": "node-b",
                "className": "ex:Entity",
                "properties": [
                    {
                        "propertyName": "ex:p4",
                        "value": "D",
                        "evidence": [evidence(3, "D")],
                    },
                    {
                        "propertyName": "ex:p5",
                        "value": "E",
                        "evidence": [evidence(4, "E")],
                    },
                ],
                "evidence": [evidence(3, "D")],
                "confidence": 0.9,
            },
        ],
        edges=[
            {
                "edgeName": "ex:relatesTo",
                "sourceTempId": "node-a",
                "targetTempId": "node-b",
                "evidence": [evidence(3, "D")],
                "confidence": 0.9,
            }
        ],
        coverage=[
            {"chunkIndex": i, "decision": "MAPPED", "reason": f"Fact {i}"}
            for i in range(5)
        ],
        warnings=[],
    )


def test_canonical_graph_context_builder_is_compact():
    _, workspace = _workspace_context(12)
    assert ingestion_use_case._canonical_graph_context(workspace, 2) == ""

    workspace.batches[0].fragment = _product_fragment()
    text = ingestion_use_case._canonical_graph_context(workspace, 2)

    assert "Existing canonical graph:" in text
    assert "ref=product-1" in text
    assert "class=pskg:BankingProduct" in text
    assert "productCode" in text
    assert "CC-FLEXI-001" in text
    assert "evidence" not in text.lower()

    # Evidence length must not grow the context (no linear growth).
    frag = _product_fragment()
    frag.nodes[0].properties[0].evidence[0].text = "CC-FLEXI-001 " + "x" * 8000
    workspace.batches[0].fragment = frag
    assert ingestion_use_case._canonical_graph_context(workspace, 2) == text

    # Accepted edges are listed; unaccepted batches are ignored.
    edge_frag = _product_fragment()
    edge_frag.edges = [
        ExtractedEdge(
            edgeName="pskg:hasEligibilityRule",
            sourceTempId="product-1",
            targetTempId="rule-1",
            evidence=[
                Evidence(
                    source=SOURCE,
                    chunkIndex=0,
                    section="Section 0",
                    text="CC-FLEXI-001",
                )
            ],
            confidence=0.9,
        )
    ]
    workspace.batches[0].fragment = edge_frag
    edge_text = ingestion_use_case._canonical_graph_context(workspace, 2)
    assert "pskg:hasEligibilityRule: product-1 -> rule-1" in edge_text


LOOP_DOC = "\n\n".join(
    f"## Section {i}\n\nLine A of section {i} about product facts.\n\nLine B of section {i}."
    for i in range(8)
)


class RecordingExtractor:
    def __init__(self):
        self.calls = []
        self.repair_calls = []

    def extract_fragment(self, **kwargs):
        self.calls.append(kwargs)
        return _loop_fragment(kwargs["batch_payload"])

    def repair_fragment(self, **kwargs):
        self.repair_calls.append(kwargs)
        affected_chunks = kwargs["affected_chunks"]
        return GraphPatchFragment(
            nodes=[],
            edges=[],
            coverage=[
                {
                    "chunkIndex": chunk["index"],
                    "decision": "NO_RELEVANT_FACT",
                    "reason": "No distinct fact in this chunk",
                }
                for chunk in affected_chunks
            ],
            warnings=[],
        )


class RecordingPlanner:
    def __init__(self, extractor):
        self.extractor = extractor

    def plan_batch(self, **kwargs):
        fragment = self.extractor.extract_fragment(
            batch_payload=kwargs["batch_payload"],
            previous_error=kwargs.get("previous_error"),
            graph_context=kwargs.get("graph_context"),
        )
        return SimpleNamespace(
            fragment=fragment,
            source_audit=SimpleNamespace(passed=True),
            placement=SimpleNamespace(passed=True, issues=[]),
            completeness=SimpleNamespace(passed=True, items=[]),
            stats=SimpleNamespace(),
        )


def _loop_fragment(batch_payload) -> GraphPatchFragment:
    chunks = batch_payload["chunks"]
    if batch_payload["batchIndex"] == 0:
        first = chunks[0]
        evidence = {
            "source": first["source"],
            "chunkIndex": first["index"],
            "section": first["section"],
            "text": "Line A of section 0 about product facts.",
        }
        return GraphPatchFragment(
            nodes=[
                {
                    "tempId": "product-1",
                    "className": "pskg:BankingProduct",
                    "properties": [
                        {
                            "propertyName": "pskg:productCode",
                            "value": "CC-FLEXI-001",
                            "evidence": [evidence],
                        }
                    ],
                    "evidence": [evidence],
                    "confidence": 0.9,
                }
            ],
            edges=[],
            coverage=[
                {
                    "chunkIndex": c["index"],
                    "decision": "MAPPED" if c["index"] == 0 else "NO_RELEVANT_FACT",
                    "reason": "Product code" if c["index"] == 0 else "No fact in this chunk",
                }
                for c in chunks
            ],
            warnings=[],
        )
    return GraphPatchFragment(
        nodes=[],
        edges=[],
        coverage=[
            {
                "chunkIndex": c["index"],
                "decision": "NO_RELEVANT_FACT",
                "reason": "No fact in this chunk",
            }
            for c in chunks
        ],
        warnings=[],
    )


def _storing_submit(*, reject_first_batch1=False, reject_all_batch1=False):
    state = {"batch1_submits": 0}

    def _fake_submit(ingestion_id, batch_index, fragment, tool_context):
        if batch_index == 1:
            state["batch1_submits"] += 1
            if reject_all_batch1 or (
                reject_first_batch1 and state["batch1_submits"] == 1
            ):
                return {
                    "success": False,
                    "stage": "batch_validation",
                    "batchIndex": 1,
                    "retryRequired": True,
                    "errorSummary": {"codes": ["COVERAGE_NOT_EVIDENCED"]},
                    "errors": [
                        {
                            "code": "COVERAGE_NOT_EVIDENCED",
                            "message": (
                                "Chunk 5 is marked MAPPED but no grounded property "
                                "or edge fact references it"
                            ),
                            "location": "coverage.5",
                        }
                    ],
                    "repairInstructions": "Add a grounded fact for chunk 5.",
                    "affectedChunkIndexes": [5],
                }
        workspace = ingestion_use_case._load_workspace(tool_context)
        workspace.batches[batch_index].fragment = fragment
        ingestion_use_case._store_workspace(tool_context, workspace)
        next_batch = IngestionWorkspaceService.next_batch(workspace)
        if next_batch is None:
            return {
                "success": True,
                "processedBatches": batch_index + 1,
                "stage": "ready_to_finalize",
            }
        return {
            "success": True,
            "processedBatches": batch_index + 1,
            "stage": "batching",
            "nextBatch": ingestion_use_case._batch_payload(workspace, next_batch),
        }

    return _fake_submit


def _run_loop(monkeypatch, *, reject_first_batch1=False, reject_all_batch1=False):
    extractor = RecordingExtractor()
    context = FakeToolContext({SOURCE: LOOP_DOC})
    monkeypatch.setattr(
        ingestion_use_case,
        "_get_semantic_graph_mapper",
        lambda: RecordingPlanner(extractor),
    )
    monkeypatch.setattr(
        ingestion_use_case,
        "_get_validation_service",
        _validation_stub,
    )
    monkeypatch.setattr(
        ingestion_use_case,
        "submit_ingestion_batch",
        _storing_submit(
            reject_first_batch1=reject_first_batch1,
            reject_all_batch1=reject_all_batch1,
        ),
    )
    monkeypatch.setattr(
        ingestion_use_case,
        "finalize_ingestion",
        lambda *a, **k: {"success": True, "stage": "ready_to_fill"},
    )
    result = asyncio.run(
        ingestion_use_case.ingest_document_end_to_end(
            SOURCE,
            context,
            persist=False,
            max_retries_per_batch=2,
        )
    )
    return result, extractor


def test_cross_fragment_edge_to_existing_product_resolves(monkeypatch):
    contents = {
        0: "Section 0 CC-FLEXI-001 Flexi body text 0.",
        10: "Section 10 Condition X body text 10.",
    }
    context, workspace = _workspace_context(12, contents)

    resp0 = ingestion_use_case.submit_ingestion_batch(
        workspace.ingestion_id,
        0,
        _product_fragment(),
        context,
    )
    assert resp0["success"] is True, resp0

    resp2 = ingestion_use_case.submit_ingestion_batch(
        workspace.ingestion_id,
        2,
        _rule_fragment(),
        context,
    )
    assert resp2["success"] is True, resp2

    workspace = ingestion_use_case._load_workspace(context)
    merged = IngestionWorkspaceService().merged_patch(workspace)
    products = [
        node for node in merged.nodes if node.class_name == "pskg:BankingProduct"
    ]
    rules = [
        node for node in merged.nodes if node.class_name == "pskg:BusinessRule"
    ]
    assert len(products) == 1
    assert len(rules) == 1


def test_submit_batch_does_not_rewrite_mapper_coverage(monkeypatch):
    text = "Mã sản phẩm CC-FLEXI-001"
    chunk = DocumentChunk(index=76, source=SOURCE, section="Product", content=text)
    context = FakeToolContext()
    context.state[ingestion_use_case.ARTIFACT_DIGEST_STATE_KEY] = "digest"
    monkeypatch.setattr(
        ingestion_use_case,
        "_get_validation_service",
        _validation_stub,
    )
    workspace = IngestionWorkspaceService().begin(
        artifact_name=SOURCE,
        provenance=ingestion_use_case._current_provenance(context),
        chunks=[chunk],
    )
    ingestion_use_case._store_workspace(context, workspace)
    fragment = GraphPatchFragment(
        nodes=[
            {
                "tempId": "product-1",
                "className": "pskg:BankingProduct",
                "properties": [
                    {
                        "propertyName": "pskg:productCode",
                        "value": "CC-FLEXI-001",
                        "evidence": [
                            {
                                "source": SOURCE,
                                "chunkIndex": 76,
                                "section": "Product",
                                "text": text,
                            }
                        ],
                    }
                ],
                "evidence": [
                    {
                        "source": SOURCE,
                        "chunkIndex": 76,
                        "section": "Product",
                        "text": text,
                    }
                ],
                "confidence": 0.9,
            }
        ],
        edges=[],
        coverage=[
            {
                "chunkIndex": 76,
                "decision": "NO_RELEVANT_FACT",
                "reason": "LLM missed the retained fact",
            }
        ],
        warnings=[],
    )

    result = ingestion_use_case.submit_ingestion_batch(
        workspace.ingestion_id,
        0,
        fragment,
        context,
    )

    assert result["success"] is True, result
    stored = ingestion_use_case._load_workspace(context)
    assert stored.batches[0].fragment.coverage[0].decision == "NO_RELEVANT_FACT"


def test_flexi_minimum_persistent_graph_from_source_chunks_passes_readiness():
    doc = next(path for path in Path("docs").glob("*.md") if "FLEXI REWARDS" in path.name)
    chunks = DocumentReader().read(doc)
    metadata_chunk = chunks[1]
    eligibility_chunk = chunks[10]
    code_line = next(
        line for line in metadata_chunk.content.splitlines() if "CC-FLEXI" in line
    )
    date_line = next(
        line for line in metadata_chunk.content.splitlines() if "01/08/2026" in line
    )
    condition_line = next(
        line for line in eligibility_chunk.content.splitlines() if "10" in line
    )
    condition_value = condition_line.lstrip("- ").replace("**", "")
    draft = GraphPatchFragment(
        nodes=[
            {
                "tempId": "product-1",
                "className": "pskg:BankingProduct",
                "properties": [
                    {
                        "propertyName": "pskg:productCode",
                        "value": "CC-FLEXI-001",
                        "evidence": [
                            {
                                "source": metadata_chunk.source,
                                "chunkIndex": metadata_chunk.index,
                                "section": metadata_chunk.section,
                                "text": code_line,
                            }
                        ],
                    },
                    {
                        "propertyName": "pskg:bankingProductEffectiveFrom",
                        "value": "2026-08-01",
                        "evidence": [
                            {
                                "source": metadata_chunk.source,
                                "chunkIndex": metadata_chunk.index,
                                "section": metadata_chunk.section,
                                "text": date_line,
                            }
                        ],
                    },
                ],
                "evidence": [
                    {
                        "source": metadata_chunk.source,
                        "chunkIndex": metadata_chunk.index,
                        "section": metadata_chunk.section,
                        "text": code_line,
                    }
                ],
                "confidence": 0.9,
            },
            {
                "tempId": "rule-1",
                "className": "pskg:BusinessRule",
                "properties": [
                    {
                        "propertyName": "pskg:businessRuleCondition",
                        "value": condition_value,
                        "evidence": [
                            {
                                "source": eligibility_chunk.source,
                                "chunkIndex": eligibility_chunk.index,
                                "section": eligibility_chunk.section,
                                "text": condition_line,
                            }
                        ],
                    }
                ],
                "evidence": [
                    {
                        "source": eligibility_chunk.source,
                        "chunkIndex": eligibility_chunk.index,
                        "section": eligibility_chunk.section,
                        "text": condition_line,
                    }
                ],
                "confidence": 0.9,
            },
        ],
        edges=[
            {
                "edgeName": "pskg:hasEligibilityRule",
                "sourceTempId": "product-1",
                "targetTempId": "rule-1",
                "evidence": [
                    {
                        "source": metadata_chunk.source,
                        "chunkIndex": metadata_chunk.index,
                        "section": metadata_chunk.section,
                        "text": code_line,
                    },
                    {
                        "source": eligibility_chunk.source,
                        "chunkIndex": eligibility_chunk.index,
                        "section": eligibility_chunk.section,
                        "text": condition_line,
                    },
                ],
                "confidence": 0.9,
            }
        ],
        coverage=[
            {
                "chunkIndex": metadata_chunk.index,
                "decision": "MAPPED",
                "reason": "Product metadata",
            },
            {
                "chunkIndex": eligibility_chunk.index,
                "decision": "MAPPED",
                "reason": "Eligibility rule",
            },
        ],
        warnings=[],
    )

    service = GraphValidation()
    assessment = service.assess(
        GraphPatchDraft.model_validate(draft.model_dump(by_alias=True, mode="json")),
        "digest",
        [metadata_chunk, eligibility_chunk],
    )

    assert assessment.result.valid_for_extraction is True
    assert assessment.result.valid_for_persistence is True
    assert assessment.result.errors == []
    assert assessment.result.readiness_issues == []


def _validation_stub():
    return GraphValidation()
