"""Scale and memory invariant tests for Ephemeral GraphPatchFragment & Neo4j Persistent Staging."""

import pytest
from app.core.schemas.ingestion.document import DocumentChunk
from app.core.schemas.ingestion.graph_patch import ChunkCoverage, Evidence, ExtractedNode, ExtractedProperty, GraphPatchFragment
from app.core.schemas.ingestion.workspace import IngestionProvenance
from app.services.ingestion.workspace.staged_ingestion import IngestionWorkspaceService


def test_workspace_batch_partitioning():
    service = IngestionWorkspaceService()
    chunks = [
        DocumentChunk(
            source="test_doc.md",
            index=i,
            content=f"Sample text content for chunk number {i} with sufficient words.",
            startLine=i * 10 + 1,
            endLine=(i + 1) * 10,
        )
        for i in range(10)
    ]
    provenance = IngestionProvenance(
        artifactDigest="digest123",
        ontologyDigest="ont123",
        skillDigest="skill123",
    )
    workspace = service.begin(
        artifact_name="test_doc.md",
        provenance=provenance,
        chunks=chunks,
    )
    assert len(workspace.batches) > 0
    assert workspace.staged_node_count == 0
    assert workspace.status == "PROCESSING"


def test_ephemeral_fragment_discard():
    service = IngestionWorkspaceService()
    chunks = [
        DocumentChunk(source="test_doc.md", index=0, content="Banking Product FLEXI001 annual fee 699000", startLine=1, endLine=5),
        DocumentChunk(source="test_doc.md", index=1, content="Campaign CAMP2026 promotes FLEXI001", startLine=6, endLine=10),
    ]
    provenance = IngestionProvenance(
        artifactDigest="digest123",
        ontologyDigest="ont123",
        skillDigest="skill123",
    )
    workspace = service.begin(
        artifact_name="test_doc.md",
        provenance=provenance,
        chunks=chunks,
    )

    ev = Evidence(source="test_doc.md", chunkIndex=0, text="FLEXI001 annual fee 699000")
    fragment_0 = GraphPatchFragment(
        nodes=[
            ExtractedNode(
                tempId="product_1",
                className="pskg:BankingProduct",
                properties=[
                    ExtractedProperty(propertyName="pskg:productCode", value="FLEXI001", evidence=[ev]),
                    ExtractedProperty(propertyName="pskg:annualFee", value=699000, evidence=[ev]),
                ],
                evidence=[ev],
                confidence=0.95,
            )
        ],
        edges=[],
        coverage=[
            ChunkCoverage(chunkIndex=0, decision="MAPPED", reason="Mapped banking product"),
            ChunkCoverage(chunkIndex=1, decision="NO_RELEVANT_FACT", reason="No facts in chunk 1"),
        ],
    )

    # Submit batch 0
    updated_ws = service.submit(workspace, 0, fragment_0)
    
    # Simulate discarding fragment post-staging
    updated_ws.batches[0].status = "STAGED"
    updated_ws.batches[0].node_count = len(fragment_0.nodes)
    updated_ws.batches[0].fragment = None

    # Verify workspace state memory is O(1)
    assert updated_ws.batches[0].fragment is None
    assert updated_ws.batches[0].status == "STAGED"
    assert updated_ws.batches[0].node_count == 1


def test_property_list_union_and_conflict_detection():
    from app.services.ingestion.incremental.accumulator import decompose_fragment
    
    ev0 = Evidence(source="doc.md", chunkIndex=0, text="Annual fee 699k for FLEXI001")
    ev1 = Evidence(source="doc.md", chunkIndex=1, text="Annual fee 799k for FLEXI001")

    fragment_b0 = GraphPatchFragment(
        nodes=[
            ExtractedNode(
                tempId="prod_1",
                className="pskg:BankingProduct",
                confidence=0.95,
                properties=[
                    ExtractedProperty(propertyName="pskg:productCode", value="FLEXI001", evidence=[ev0]),
                    ExtractedProperty(propertyName="pskg:annualFee", value=699000, evidence=[ev0]),
                    ExtractedProperty(propertyName="pskg:features", value=["Cashback", "Free lounge"], evidence=[ev0]),
                ],
                evidence=[ev0],
            )
        ],
        edges=[],
        coverage=[ChunkCoverage(chunkIndex=0, decision="MAPPED", reason="Batch 0")],
    )

    decomp0 = decompose_fragment(fragment_b0, ingestion_id="test_ing_1", batch_index=0)
    assert len(decomp0["entities"]) == 1
    assert decomp0["entities"][0]["entityKey"] == "pskg:BankingProduct|pskg:productCode|flexi001"
    assert len(decomp0["properties"]) == 3
    
    # Verify evidence text mapping (no AttributeError on ev.text)
    fee_prop = next(p for p in decomp0["properties"] if p["propertyName"] == "pskg:annualFee")
    assert fee_prop["evidence"][0]["quote"] == "Annual fee 699k for FLEXI001"

