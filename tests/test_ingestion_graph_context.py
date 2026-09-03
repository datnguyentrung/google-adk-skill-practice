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
from app.services.ingestion.fragment_grounding_repair import (
    align_coverage_with_grounded_facts,
    repair_fragment_grounding,
)
from app.services.ingestion.loader import OntologyLoader
from app.services.ingestion.registry import OntologyRegistry
from app.services.ingestion.source_grounding import SourceGroundingValidator
from app.services.ingestion.staged_ingestion import IngestionWorkspaceService
from app.services.ingestion.use_case import IngestionUseCase
from app.services.ingestion.validate_graph_patch import GraphPatchValidationService

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


def test_targeted_merge_repairs_one_fact_and_preserves_other_facts():
    original = _generic_fragment()
    repair = GraphPatchFragment(
        nodes=[
            {
                "tempId": "node-a",
                "className": "ex:Entity",
                "properties": [
                    {
                        "propertyName": "ex:p2",
                        "value": "fixed",
                        "evidence": [
                            {
                                "source": SOURCE,
                                "chunkIndex": 1,
                                "section": "Section 1",
                                "text": "fixed",
                            }
                        ],
                    },
                    {
                        "propertyName": "ex:p1",
                        "value": "changed",
                        "evidence": [
                            {
                                "source": SOURCE,
                                "chunkIndex": 0,
                                "section": "Section 0",
                                "text": "changed",
                            }
                        ],
                    },
                ],
                "evidence": [
                    {
                        "source": SOURCE,
                        "chunkIndex": 1,
                        "section": "Section 1",
                        "text": "fixed",
                    }
                ],
                "confidence": 0.1,
            }
        ],
        edges=[],
        coverage=[{"chunkIndex": 1, "decision": "MAPPED", "reason": "Fixed"}],
        warnings=[],
    )
    issues = [
        ingestion_use_case.ValidationIssue(
            code="PROPERTY_VALUE_NOT_GROUNDED",
            message="bad",
            location="nodes.0.properties.1.evidence",
            nodeTempId="node-a",
            propertyName="ex:p2",
        )
    ]

    scope = ingestion_use_case._rejected_scope_from_issues(original, issues)
    merged = ingestion_use_case._merge_targeted_repair(original, repair, scope)

    props = {
        prop.property_name: prop.model_dump(by_alias=True, mode="json")
        for prop in merged.nodes[0].properties
    }
    assert props["ex:p2"]["value"] == "fixed"
    assert props["ex:p1"] == original.nodes[0].properties[0].model_dump(
        by_alias=True, mode="json"
    )
    assert props["ex:p3"] == original.nodes[0].properties[2].model_dump(
        by_alias=True, mode="json"
    )
    assert merged.nodes[1].model_dump(by_alias=True, mode="json") == original.nodes[
        1
    ].model_dump(by_alias=True, mode="json")


def test_targeted_merge_does_not_change_valid_edge_for_property_repair():
    original = _generic_fragment()
    repair = GraphPatchFragment(
        nodes=[
            {
                "tempId": "node-a",
                "className": "ex:Entity",
                "properties": [
                    {
                        "propertyName": "ex:p2",
                        "value": "fixed",
                        "evidence": [
                            {
                                "source": SOURCE,
                                "chunkIndex": 1,
                                "section": "Section 1",
                                "text": "fixed",
                            }
                        ],
                    }
                ],
                "evidence": [
                    {
                        "source": SOURCE,
                        "chunkIndex": 1,
                        "section": "Section 1",
                        "text": "fixed",
                    }
                ],
                "confidence": 0.9,
            }
        ],
        edges=[],
        coverage=[{"chunkIndex": 1, "decision": "MAPPED", "reason": "Fixed"}],
        warnings=[],
    )
    scope = ingestion_use_case.TargetedRepairScope(
        rejected_nodes=frozenset(),
        rejected_node_evidence=frozenset(),
        rejected_properties=frozenset({("node-a", "ex:p2")}),
        rejected_edges=frozenset(),
        rejected_coverage_chunks=frozenset(),
    )

    merged = ingestion_use_case._merge_targeted_repair(original, repair, scope)

    assert merged.edges[0].model_dump(by_alias=True, mode="json") == original.edges[
        0
    ].model_dump(by_alias=True, mode="json")


def test_targeted_merge_replaces_rejected_property_without_duplicate():
    original = _generic_fragment()
    repair = GraphPatchFragment(
        nodes=[
            {
                "tempId": "node-a",
                "className": "ex:Entity",
                "properties": [
                    {
                        "propertyName": "ex:p2",
                        "value": "fixed",
                        "evidence": [
                            {
                                "source": SOURCE,
                                "chunkIndex": 1,
                                "section": "Section 1",
                                "text": "fixed",
                            }
                        ],
                    }
                ],
                "evidence": [
                    {
                        "source": SOURCE,
                        "chunkIndex": 1,
                        "section": "Section 1",
                        "text": "fixed",
                    }
                ],
                "confidence": 0.9,
            }
        ],
        edges=[],
        coverage=[
            {
                "chunkIndex": 1,
                "decision": "MAPPED",
                "reason": "Repaired property evidence",
            }
        ],
        warnings=[],
    )
    scope = ingestion_use_case.TargetedRepairScope(
        rejected_nodes=frozenset(),
        rejected_node_evidence=frozenset(),
        rejected_properties=frozenset({("node-a", "ex:p2")}),
        rejected_edges=frozenset(),
        rejected_coverage_chunks=frozenset(),
    )

    merged = ingestion_use_case._merge_targeted_repair(original, repair, scope)

    repaired_properties = [
        prop for prop in merged.nodes[0].properties if prop.property_name == "ex:p2"
    ]
    assert [prop.value for prop in repaired_properties] == ["fixed"]
    assert len(merged.nodes[0].properties) == len(original.nodes[0].properties)


def test_targeted_merge_repairs_one_coverage_item_only():
    original = _generic_fragment()
    repair = GraphPatchFragment(
        nodes=[
            {
                "tempId": "node-c",
                "className": "ex:Entity",
                "properties": [
                    {
                        "propertyName": "ex:p6",
                        "value": "F",
                        "evidence": [
                            {
                                "source": SOURCE,
                                "chunkIndex": 2,
                                "section": "Section 2",
                                "text": "F",
                            }
                        ],
                    }
                ],
                "evidence": [
                    {
                        "source": SOURCE,
                        "chunkIndex": 2,
                        "section": "Section 2",
                        "text": "F",
                    }
                ],
                "confidence": 0.9,
            }
        ],
        edges=[],
        coverage=[
            {
                "chunkIndex": 2,
                "decision": "MAPPED",
                "reason": "Repaired grounded fact",
            },
            {
                "chunkIndex": 3,
                "decision": "FAILED",
                "reason": "Should be ignored",
            },
        ],
        warnings=[],
    )
    scope = ingestion_use_case.TargetedRepairScope(
        rejected_nodes=frozenset(),
        rejected_node_evidence=frozenset(),
        rejected_properties=frozenset(),
        rejected_edges=frozenset(),
        rejected_coverage_chunks=frozenset({2}),
    )

    merged = ingestion_use_case._merge_targeted_repair(original, repair, scope)

    coverage = {item.chunk_index: item.decision for item in merged.coverage}
    assert coverage[2] == "MAPPED"
    assert coverage[3] == original.coverage[3].decision
    assert [node.temp_id for node in merged.nodes] == ["node-a", "node-b", "node-c"]
    assert merged.nodes[0].model_dump(by_alias=True, mode="json") == original.nodes[
        0
    ].model_dump(by_alias=True, mode="json")


def test_targeted_merge_preserves_facts_across_multiple_attempts():
    original = _generic_fragment()
    scope_a = ingestion_use_case.TargetedRepairScope(
        rejected_nodes=frozenset(),
        rejected_node_evidence=frozenset(),
        rejected_properties=frozenset({("node-a", "ex:p2")}),
        rejected_edges=frozenset(),
        rejected_coverage_chunks=frozenset(),
    )
    repair_a = GraphPatchFragment(
        nodes=[
            {
                "tempId": "node-a",
                "className": "ex:Entity",
                "properties": [
                    {
                        "propertyName": "ex:p2",
                        "value": "fixed-a",
                        "evidence": [
                            {
                                "source": SOURCE,
                                "chunkIndex": 1,
                                "section": "Section 1",
                                "text": "fixed-a",
                            }
                        ],
                    }
                ],
                "evidence": [
                    {
                        "source": SOURCE,
                        "chunkIndex": 1,
                        "section": "Section 1",
                        "text": "fixed-a",
                    }
                ],
                "confidence": 0.9,
            }
        ],
        edges=[],
        coverage=[{"chunkIndex": 1, "decision": "MAPPED", "reason": "Fixed"}],
        warnings=[],
    )
    after_a = ingestion_use_case._merge_targeted_repair(original, repair_a, scope_a)
    scope_b = ingestion_use_case.TargetedRepairScope(
        rejected_nodes=frozenset(),
        rejected_node_evidence=frozenset(),
        rejected_properties=frozenset({("node-b", "ex:p5")}),
        rejected_edges=frozenset(),
        rejected_coverage_chunks=frozenset(),
    )
    repair_b = GraphPatchFragment(
        nodes=[
            {
                "tempId": "node-b",
                "className": "ex:Entity",
                "properties": [
                    {
                        "propertyName": "ex:p5",
                        "value": "fixed-b",
                        "evidence": [
                            {
                                "source": SOURCE,
                                "chunkIndex": 4,
                                "section": "Section 4",
                                "text": "fixed-b",
                            }
                        ],
                    }
                ],
                "evidence": [
                    {
                        "source": SOURCE,
                        "chunkIndex": 4,
                        "section": "Section 4",
                        "text": "fixed-b",
                    }
                ],
                "confidence": 0.9,
            }
        ],
        edges=[],
        coverage=[{"chunkIndex": 4, "decision": "MAPPED", "reason": "Fixed"}],
        warnings=[],
    )

    after_b = ingestion_use_case._merge_targeted_repair(after_a, repair_b, scope_b)

    node_a_props = {prop.property_name: prop.value for prop in after_b.nodes[0].properties}
    assert node_a_props["ex:p1"] == "A"
    assert node_a_props["ex:p2"] == "fixed-a"
    assert node_a_props["ex:p3"] == "C"
    assert {prop.property_name: prop.value for prop in after_b.nodes[1].properties}[
        "ex:p5"
    ] == "fixed-b"


def test_grounding_repair_replaces_section_title_edge_evidence_with_body_excerpt():
    chunks = [
        DocumentChunk(
            index=55,
            source=SOURCE,
            section="21.1. Customer concern",
            content=(
                "Interest can be avoided for eligible purchases when the full "
                "statement balance is paid on time."
            ),
        )
    ]
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
                                "chunkIndex": 55,
                                "section": "21.1. Customer concern",
                                "text": (
                                    "Interest can be avoided for eligible purchases "
                                    "when the full statement balance is paid on time."
                                ),
                            }
                        ],
                    }
                ],
                "evidence": [
                    {
                        "source": SOURCE,
                        "chunkIndex": 55,
                        "section": "21.1. Customer concern",
                        "text": (
                            "Interest can be avoided for eligible purchases when "
                            "the full statement balance is paid on time."
                        ),
                    }
                ],
                "confidence": 0.9,
            },
            {
                "tempId": "script-1",
                "className": "pskg:SalesScript",
                "properties": [
                    {
                        "propertyName": "pskg:objectionHandling",
                        "value": (
                            "Interest can be avoided for eligible purchases when "
                            "the full statement balance is paid on time."
                        ),
                        "evidence": [
                            {
                                "source": SOURCE,
                                "chunkIndex": 55,
                                "section": "21.1. Customer concern",
                                "text": (
                                    "Interest can be avoided for eligible purchases "
                                    "when the full statement balance is paid on time."
                                ),
                            }
                        ],
                    }
                ],
                "evidence": [
                    {
                        "source": SOURCE,
                        "chunkIndex": 55,
                        "section": "21.1. Customer concern",
                        "text": (
                            "Interest can be avoided for eligible purchases when "
                            "the full statement balance is paid on time."
                        ),
                    }
                ],
                "confidence": 0.9,
            },
        ],
        edges=[
            {
                "edgeName": "pskg:hasScript",
                "sourceTempId": "product-1",
                "targetTempId": "script-1",
                "evidence": [
                    {
                        "source": SOURCE,
                        "chunkIndex": 55,
                        "section": "21.1. Customer concern",
                        "text": "21.1. Customer concern",
                    }
                ],
                "confidence": 0.9,
            }
        ],
        coverage=[
            {
                "chunkIndex": 55,
                "decision": "MAPPED",
                "reason": "Script edge",
            }
        ],
        warnings=[],
    )

    repaired = repair_fragment_grounding(
        fragment,
        chunks,
        SourceGroundingValidator(_ontology_registry()),
    )

    assert len(repaired.edges) == 1
    assert repaired.edges[0].evidence[0].text == (
        "Interest can be avoided for eligible purchases when the full statement "
        "balance is paid on time."
    )
    assert repaired.coverage[0].decision == "MAPPED"
    assert all(
        evidence.text != "21.1. Customer concern"
        for node in repaired.nodes
        for prop in node.properties
        for evidence in prop.evidence
    )


def test_grounding_repair_converts_unbacked_mapped_coverage_to_no_relevant_fact():
    chunks = [
        DocumentChunk(
            index=55,
            source=SOURCE,
            section="21.1. Customer concern",
            content="Body text that does not support the emitted value.",
        )
    ]
    fragment = GraphPatchFragment(
        nodes=[],
        edges=[
            {
                "edgeName": "pskg:hasScript",
                "sourceTempId": "product-1",
                "targetTempId": "script-1",
                "evidence": [
                    {
                        "source": SOURCE,
                        "chunkIndex": 55,
                        "section": "21.1. Customer concern",
                        "text": SOURCE,
                    }
                ],
                "confidence": 0.9,
            }
        ],
        coverage=[
            {
                "chunkIndex": 55,
                "decision": "MAPPED",
                "reason": "Script edge",
            }
        ],
        warnings=[],
    )

    repaired = repair_fragment_grounding(
        fragment,
        chunks,
        SourceGroundingValidator(_ontology_registry()),
    )

    assert repaired.edges == []
    assert repaired.coverage[0].decision == "NO_RELEVANT_FACT"


def test_coverage_alignment_maps_chunk_with_grounded_property():
    text = "Mã sản phẩm CC-FLEXI-001"
    chunks = [
        DocumentChunk(index=76, source=SOURCE, section="Product", content=text)
    ]
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
                "decision": "NOT_RELEVANT",
                "reason": "LLM marked it irrelevant",
            }
        ],
        warnings=[],
    )

    aligned = align_coverage_with_grounded_facts(
        fragment,
        chunks,
        SourceGroundingValidator(_ontology_registry()),
    )

    assert aligned.coverage[0].decision == "MAPPED"


def test_coverage_alignment_maps_chunk_with_grounded_edge():
    product_text = "Mã sản phẩm CC-FLEXI-001"
    rule_text = "CC-FLEXI-001 áp dụng cho khách hàng từ 20 đến 60 tuổi."
    chunks = [
        DocumentChunk(index=76, source=SOURCE, section="Rule", content=rule_text)
    ]
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
                                "section": "Rule",
                                "text": product_text,
                            }
                        ],
                    }
                ],
                "evidence": [
                    {
                        "source": SOURCE,
                        "chunkIndex": 76,
                        "section": "Rule",
                        "text": product_text,
                    }
                ],
                "confidence": 0.9,
            },
            {
                "tempId": "rule-1",
                "className": "pskg:EligibilityRule",
                "properties": [
                    {
                        "propertyName": "pskg:businessRuleCondition",
                        "value": "khách hàng từ 20 đến 60 tuổi",
                        "evidence": [
                            {
                                "source": SOURCE,
                                "chunkIndex": 76,
                                "section": "Rule",
                                "text": rule_text,
                            }
                        ],
                    }
                ],
                "evidence": [
                    {
                        "source": SOURCE,
                        "chunkIndex": 76,
                        "section": "Rule",
                        "text": rule_text,
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
                        "source": SOURCE,
                        "chunkIndex": 76,
                        "section": "Rule",
                        "text": rule_text,
                    }
                ],
                "confidence": 0.9,
            }
        ],
        coverage=[
            {
                "chunkIndex": 76,
                "decision": "NO_RELEVANT_FACT",
                "reason": "LLM missed the retained edge",
            }
        ],
        warnings=[],
    )

    aligned = align_coverage_with_grounded_facts(
        fragment,
        chunks,
        SourceGroundingValidator(_ontology_registry()),
    )

    assert aligned.coverage[0].decision == "MAPPED"


def test_coverage_alignment_downgrades_mapped_chunk_without_grounded_fact():
    chunks = [
        DocumentChunk(
            index=76,
            source=SOURCE,
            section="Product",
            content="Nội dung không chứa mã sản phẩm.",
        )
    ]
    fragment = GraphPatchFragment(
        nodes=[],
        edges=[],
        coverage=[
            {
                "chunkIndex": 76,
                "decision": "MAPPED",
                "reason": "LLM claimed a fact",
            }
        ],
        warnings=[],
    )

    aligned = align_coverage_with_grounded_facts(
        fragment,
        chunks,
        SourceGroundingValidator(_ontology_registry()),
    )

    assert aligned.coverage[0].decision == "NO_RELEVANT_FACT"


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
        "_get_semantic_placement_planner",
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


def test_loop_passes_graph_context_from_accepted_batch(monkeypatch):
    result, extractor = _run_loop(monkeypatch)

    assert result["success"] is True
    assert len(extractor.calls) == 2
    assert extractor.calls[0]["graph_context"] == ""
    context = extractor.calls[1]["graph_context"]
    assert "ref=product-1" in context
    assert "class=pskg:BankingProduct" in context
    assert "CC-FLEXI-001" in context


def test_retry_receives_same_graph_context_and_previous_error(monkeypatch):
    result, extractor = _run_loop(monkeypatch, reject_first_batch1=True)

    assert result["success"] is True
    assert len(extractor.calls) == 3
    assert len(extractor.repair_calls) == 0
    assert extractor.calls[1]["graph_context"] == extractor.calls[2]["graph_context"]
    previous_error = extractor.calls[2]["previous_error"]
    assert previous_error is not None
    assert (
        previous_error["repairInstructions"]
        == "Add a grounded fact for chunk 5."
    )


def test_cross_fragment_edge_to_existing_product_resolves(monkeypatch):
    contents = {
        0: "Section 0 CC-FLEXI-001 Flexi body text 0.",
        10: "Section 10 Condition X body text 10.",
    }
    context, workspace = _workspace_context(12, contents)

    resp0 = IngestionUseCase().submit_batch(
        workspace.ingestion_id,
        0,
        _product_fragment(),
        context,
    )
    assert resp0["success"] is True, resp0

    resp2 = IngestionUseCase().submit_batch(
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


def test_no_coverage_downgrade_fallback(monkeypatch):
    class MappedNoFactExtractor(RecordingExtractor):
        def extract_fragment(self, **kwargs):
            self.calls.append(kwargs)
            payload = kwargs["batch_payload"]
            if payload["batchIndex"] == 1:
                return GraphPatchFragment(
                    nodes=[],
                    edges=[],
                    coverage=[
                        {
                            "chunkIndex": c["index"],
                            "decision": "MAPPED",
                            "reason": "Claimed mapped without a fact",
                        }
                        for c in payload["chunks"]
                    ],
                    warnings=[],
                )
            return _loop_fragment(payload)

    extractor = MappedNoFactExtractor()
    context = FakeToolContext({SOURCE: LOOP_DOC})
    monkeypatch.setattr(
        ingestion_use_case,
        "_get_semantic_placement_planner",
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
        _storing_submit(reject_all_batch1=True),
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

    assert result["success"] is False
    assert result["terminal"] is True
    assert result["failureReason"] == "SEMANTIC_REPAIR_EXHAUSTED"
    assert {error["code"] for error in result["errors"]} == {
        "COVERAGE_NOT_EVIDENCED"
    }
    assert len(extractor.calls) == 3
    assert len(extractor.repair_calls) == 0
    assert extractor.calls[2]["previous_error"]["affectedChunkIndexes"] == [5]


def test_submit_batch_aligns_conflicting_coverage_before_validation(monkeypatch):
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
    assert stored.batches[0].fragment.coverage[0].decision == "MAPPED"


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

    service = GraphPatchValidationService()
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
    return GraphPatchValidationService()
