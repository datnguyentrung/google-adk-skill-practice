import pytest
from app.core.schemas.ingestion.document import DocumentChunk
from app.core.schemas.ingestion.workspace import (
    IngestionWorkspace,
    IngestionBatch,
)
from app.core.schemas.ingestion.graph_patch import (
    ExtractedNode,
    ExtractedProperty,
    ExtractedEdge,
    ChunkCoverage,
    Evidence,
    GraphPatchFragment,
)
from app.services.ingestion.orchestration.state import (
    _current_provenance,
    _store_workspace,
    ARTIFACT_DIGEST_STATE_KEY,
    ARTIFACT_NAME_STATE_KEY,
)
from app.services.ingestion.incremental.staging_store import IngestionStagingStore
from app.tools.ingestion_tools import submit_ingestion_batch


class MockContext:
    def __init__(self, state):
        self.state = state


@pytest.fixture
def clean_staging():
    store = IngestionStagingStore()
    ingestion_id = "test_ingest_canonical_reuse"
    store.purge_staging(ingestion_id)
    yield ingestion_id, store
    store.purge_staging(ingestion_id)
    store.close()


def test_v2_canonical_reuse_across_batches(clean_staging):
    ingestion_id, store = clean_staging

    context = MockContext(
        state={
            ARTIFACT_NAME_STATE_KEY: "banking_product.md",
            ARTIFACT_DIGEST_STATE_KEY: "art_digest_test_01",
        }
    )

    prov = _current_provenance(context)

    # Create workspace with 2 batches
    workspace = IngestionWorkspace(
        ingestion_id=ingestion_id,
        artifact_name="banking_product.md",
        status="RUNNING",
        provenance=prov,
        chunks=[
            DocumentChunk(
                source="banking_product.md",
                index=0,
                content="Mã SP: TD-ONLINE-001. Sản phẩm Tiền gửi An Tâm",
                content_hash="c0",
                section="header",
                start_char=0,
                end_char=50,
            ),
            DocumentChunk(
                source="banking_product.md",
                index=1,
                content="Lãi suất 6.5%/năm áp dụng cho Tiền gửi An Tâm",
                content_hash="c1",
                section="policy",
                start_char=51,
                end_char=100,
            ),
        ],
        batches=[
            IngestionBatch(index=0, chunk_indexes=[0], content_chars=50, status="READY"),
            IngestionBatch(index=1, chunk_indexes=[1], content_chars=50, status="READY"),
        ],
    )
    _store_workspace(context, workspace)

    # Batch 0: Stages BankingProduct node
    fragment_b0 = GraphPatchFragment(
        nodes=[
            ExtractedNode(
                temp_id="prod_an_tam",
                class_name="pskg:BankingProduct",
                properties=[
                    ExtractedProperty(
                        property_name="pskg:productCode",
                        value="TD-ONLINE-001",
                        evidence=[Evidence(source="banking_product.md", chunk_index=0, text="Mã SP: TD-ONLINE-001")],
                    ),
                    ExtractedProperty(
                        property_name="pskg:bankingProductStatus",
                        value="ACTIVE",
                        evidence=[Evidence(source="banking_product.md", chunk_index=0, text="Sản phẩm Tiền gửi An Tâm")],
                    ),
                ],
                evidence=[Evidence(source="banking_product.md", chunk_index=0, text="Sản phẩm Tiền gửi An Tâm")],
                confidence=1.0,
            )
        ],
        edges=[],
        coverage=[ChunkCoverage(chunk_index=0, decision="MAPPED", reason="Product header")],
    )

    resp_b0 = submit_ingestion_batch(
        ingestion_id=ingestion_id,
        batch_index=0,
        graph_fragment=fragment_b0.model_dump(by_alias=True, mode="json"),
        tool_context=context,
    )
    assert resp_b0["success"] is True

    # Batch 1: Stages Rule node & edge referencing 'prod_an_tam' as source without re-declaring product node
    fragment_b1 = GraphPatchFragment(
        nodes=[
            ExtractedNode(
                temp_id="rule_01",
                class_name="pskg:BusinessRule",
                properties=[
                    ExtractedProperty(
                        property_name="pskg:ruleType",
                        value="ELIGIBILITY",
                        evidence=[Evidence(source="banking_product.md", chunk_index=1, text="Điều kiện mở tài khoản")],
                    ),
                    ExtractedProperty(
                        property_name="pskg:businessRuleCondition",
                        value="Đủ 18 tuổi",
                        evidence=[Evidence(source="banking_product.md", chunk_index=1, text="Đủ 18 tuổi")],
                    ),
                ],
                evidence=[Evidence(source="banking_product.md", chunk_index=1, text="Điều kiện mở tài khoản")],
                confidence=1.0,
            )
        ],
        edges=[
            ExtractedEdge(
                source_temp_id="prod_an_tam",
                edge_name="pskg:hasEligibilityRule",
                target_temp_id="rule_01",
                evidence=[Evidence(source="banking_product.md", chunk_index=1, text="Áp dụng cho An Tâm")],
                confidence=1.0,
            )
        ],
        coverage=[ChunkCoverage(chunk_index=1, decision="MAPPED", reason="Eligibility rule")],
    )

    resp_b1 = submit_ingestion_batch(
        ingestion_id=ingestion_id,
        batch_index=1,
        graph_fragment=fragment_b1.model_dump(by_alias=True, mode="json"),
        tool_context=context,
    )
    assert resp_b1["success"] is True

    # Check Staging Summary
    summary = store.get_staging_summary(ingestion_id)
    assert summary["entityCount"] == 2
    assert summary["edgeCount"] == 1
    assert summary["pendingEdgeCount"] == 0
    assert summary["conflictCount"] == 0

    # Query the staged edge in Neo4j to confirm canonical sourceEntityKey
    driver = store.client.get_driver()
    with driver.session(database=store.client.database_name) as session:
        record = session.run(
            """
            MATCH (e:IngestionStagedEdge {ingestionId: $ingestion_id})
            RETURN e.edgeName AS edgeName, e.sourceEntityKey AS sourceEntityKey, e.targetEntityKey AS targetEntityKey
            """,
            ingestion_id=ingestion_id,
        ).single()
        assert record is not None
        assert record["edgeName"] == "pskg:hasEligibilityRule"
        assert record["sourceEntityKey"] == "pskg:BankingProduct|pskg:productCode|td-online-001"
