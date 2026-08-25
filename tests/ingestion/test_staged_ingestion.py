from app.core.schemas.ingestion.document import DocumentChunk
from app.core.schemas.ingestion.graph_patch import GraphPatchFragment
from app.core.schemas.ingestion.workspace import IngestionProvenance
from app.services.ingestion.staged_ingestion import (
    MAX_BATCH_CHARS,
    MAX_BATCH_CHUNKS,
    IngestionWorkspaceService,
    WorkspaceConflictError,
)


def chunks(count: int) -> list[DocumentChunk]:
    return [
        DocumentChunk(
            index=index,
            source="long.md",
            section=f"Section {index}",
            content=(f"Fact {index} " * 40).strip(),
        )
        for index in range(count)
    ]


def provenance(**changes) -> IngestionProvenance:
    values = {
        "artifactDigest": "artifact",
        "ontologyDigest": "ontology",
        "skillDigest": "skill",
        **changes,
    }
    return IngestionProvenance.model_validate(values)


def fragment(batch, *, value="P-1") -> GraphPatchFragment:
    first = batch.chunk_indexes[0]
    evidence = [
        {
            "source": "long.md",
            "chunkIndex": first,
            "section": f"Section {first}",
            "text": f"Fact {first}",
        }
    ]
    return GraphPatchFragment.model_validate(
        {
            "nodes": [
                {
                    "tempId": "product-1",
                    "className": "pskg:BankingProduct",
                    "properties": [
                        {
                            "propertyName": "pskg:productCode",
                            "value": value,
                            "evidence": evidence,
                        }
                    ],
                    "evidence": evidence,
                    "confidence": 0.9,
                }
            ],
            "edges": [],
            "coverage": [
                {
                    "chunkIndex": index,
                    "decision": "NOT_RELEVANT" if index != first else "MAPPED",
                    "reason": "Batch reviewed",
                }
                for index in batch.chunk_indexes
            ],
            "warnings": [],
        }
    )


def fee_fragment(batch, *, value, text) -> GraphPatchFragment:
    first = batch.chunk_indexes[0]
    evidence = [
        {
            "source": "long.md",
            "chunkIndex": first,
            "section": f"Section {first}",
            "text": text,
        }
    ]
    return GraphPatchFragment.model_validate(
        {
            "nodes": [
                {
                    "tempId": "product-1",
                    "className": "pskg:BankingProduct",
                    "properties": [
                        {
                            "propertyName": "pskg:fee",
                            "value": value,
                            "evidence": evidence,
                        }
                    ],
                    "evidence": evidence,
                    "confidence": 0.9,
                }
            ],
            "edges": [],
            "coverage": [
                {
                    "chunkIndex": index,
                    "decision": "NOT_RELEVANT" if index != first else "MAPPED",
                    "reason": "Batch reviewed",
                }
                for index in batch.chunk_indexes
            ],
            "warnings": [],
        }
    )


def attributes_fragment(batch, values) -> GraphPatchFragment:
    first = batch.chunk_indexes[0]
    evidence = [
        {
            "source": "long.md",
            "chunkIndex": first,
            "section": f"Section {first}",
            "text": f"Fact {first}",
        }
    ]
    payload = fragment(batch).model_dump(by_alias=True, mode="json")
    payload["nodes"][0]["properties"] = [
        {
            "propertyName": "pskg:productAttributes",
            "value": values,
            "evidence": evidence,
        }
    ]
    return GraphPatchFragment.model_validate(payload)


def pricing_rule_fragment(batch, *, rule_id, condition) -> GraphPatchFragment:
    first = batch.chunk_indexes[0]
    evidence = [
        {
            "source": "long.md",
            "chunkIndex": first,
            "section": f"Section {first}",
            "text": condition,
        }
    ]
    return GraphPatchFragment.model_validate(
        {
            "nodes": [
                {
                    "tempId": "product-1",
                    "className": "pskg:BankingProduct",
                    "properties": [],
                    "evidence": evidence,
                    "confidence": 0.9,
                },
                {
                    "tempId": rule_id,
                    "className": "pskg:BusinessRule",
                    "properties": [
                        {
                            "propertyName": "pskg:businessRuleCondition",
                            "value": condition,
                            "evidence": evidence,
                        }
                    ],
                    "evidence": evidence,
                    "confidence": 0.9,
                },
            ],
            "edges": [
                {
                    "edgeName": "pskg:hasSalesConditionRule",
                    "sourceTempId": "product-1",
                    "targetTempId": rule_id,
                    "evidence": evidence,
                    "confidence": 0.9,
                }
            ],
            "coverage": [
                {
                    "chunkIndex": index,
                    "decision": "NOT_RELEVANT" if index != first else "MAPPED",
                    "reason": "Batch reviewed",
                }
                for index in batch.chunk_indexes
            ],
            "warnings": [],
        }
    )


def test_begin_partitions_94_chunks_with_bounded_batch_limits():
    service = IngestionWorkspaceService()
    source = chunks(94)

    workspace = service.begin(
        artifact_name="long.md",
        provenance=provenance(),
        chunks=source,
    )

    assert len(workspace.batches) > 4
    assert [index for batch in workspace.batches for index in batch.chunk_indexes] == list(range(94))
    assert all(len(batch.chunk_indexes) <= MAX_BATCH_CHUNKS for batch in workspace.batches)
    assert all(batch.content_chars <= MAX_BATCH_CHARS for batch in workspace.batches)
    assert len(workspace.batches) == 19


def test_submit_is_idempotent_and_merges_same_temp_id():
    service = IngestionWorkspaceService()
    workspace = service.begin(
        artifact_name="long.md",
        provenance=provenance(),
        chunks=chunks(10),
    )
    first = fragment(workspace.batches[0])

    workspace = service.submit(workspace, 0, first)
    retried = service.submit(workspace, 0, first)
    second = fragment(retried.batches[1])
    merged = service.submit(retried, 1, second)

    assert retried.model_dump() == workspace.model_dump()
    patch = service.merged_patch(merged)
    assert len(patch.nodes) == 1
    assert len(patch.nodes[0].properties[0].evidence) == 2
    assert len(patch.coverage) == 10


def test_submit_accepts_an_all_not_relevant_batch_before_relevant_batches():
    service = IngestionWorkspaceService()
    workspace = service.begin(
        artifact_name="long.md",
        provenance=provenance(),
        chunks=chunks(10),
    )
    first_batch = workspace.batches[0]
    irrelevant = GraphPatchFragment.model_validate(
        {
            "nodes": [],
            "edges": [],
            "coverage": [
                {
                    "chunkIndex": index,
                    "decision": "NOT_RELEVANT",
                    "reason": "No graph facts",
                }
                for index in first_batch.chunk_indexes
            ],
        }
    )

    workspace = service.submit(workspace, 0, irrelevant)
    workspace = service.submit(workspace, 1, fragment(workspace.batches[1]))

    patch = service.merged_patch(workspace)
    assert len(patch.nodes) == 1
    assert len(patch.coverage) == 10


def test_submit_rejects_conflicting_property_and_keeps_previous_batch():
    service = IngestionWorkspaceService()
    workspace = service.begin(
        artifact_name="long.md",
        provenance=provenance(),
        chunks=chunks(30),
    )
    workspace = service.submit(workspace, 0, fragment(workspace.batches[0]))

    try:
        service.submit(workspace, 1, fragment(workspace.batches[1], value="P-2"))
    except WorkspaceConflictError as exc:
        assert "productCode" in str(exc)
    else:
        raise AssertionError("Expected a property conflict")

    assert workspace.batches[1].fragment is None


def test_conflicting_singleton_property_includes_structured_details():
    service = IngestionWorkspaceService()
    workspace = service.begin(
        artifact_name="long.md",
        provenance=provenance(),
        chunks=chunks(10),
    )
    workspace = service.submit(workspace, 0, fragment(workspace.batches[0]))

    try:
        service.submit(workspace, 1, fragment(workspace.batches[1], value="P-2"))
    except WorkspaceConflictError as exc:
        assert exc.conflict["nodeTempId"] == "product-1"
        assert exc.conflict["propertyName"] == "pskg:productCode"
        assert exc.conflict["existingValue"] == "P-1"
        assert exc.conflict["incomingValue"] == "P-2"
        assert exc.conflict["existingEvidence"]
        assert exc.conflict["incomingEvidence"]
    else:
        raise AssertionError("Expected a property conflict")


def test_list_valued_property_merges_as_semantic_collection():
    service = IngestionWorkspaceService()
    workspace = service.begin(
        artifact_name="long.md",
        provenance=provenance(),
        chunks=chunks(10),
    )
    workspace = service.submit(
        workspace,
        0,
        attributes_fragment(workspace.batches[0], ["Gold", "VND"]),
    )
    workspace = service.submit(
        workspace,
        1,
        attributes_fragment(workspace.batches[1], ["VND", "Cashback"]),
    )

    patch = service.merged_patch(workspace)
    attributes = patch.nodes[0].properties[0]
    assert attributes.property_name == "pskg:productAttributes"
    assert attributes.value == ["Gold", "VND", "Cashback"]
    assert len(attributes.evidence) == 2


def test_product_attributes_scalar_and_list_merge_without_conflict():
    service = IngestionWorkspaceService()
    workspace = service.begin(artifact_name="long.md", provenance=provenance(), chunks=chunks(10))
    workspace = service.submit(workspace, 0, attributes_fragment(workspace.batches[0], "Gold"))
    workspace = service.submit(workspace, 1, attributes_fragment(workspace.batches[1], ["VND", "Cashback"]))
    patch = service.merged_patch(workspace)
    attributes = patch.nodes[0].properties[0]
    assert attributes.value == ["Gold", "VND", "Cashback"]
    assert len(attributes.evidence) == 2


def test_raw_repeated_scalar_fee_still_conflicts_with_details():
    service = IngestionWorkspaceService()
    workspace = service.begin(
        artifact_name="long.md",
        provenance=provenance(),
        chunks=chunks(10),
    )
    workspace = service.submit(
        workspace,
        0,
        fee_fragment(workspace.batches[0], value=699000, text="Annual fee 699000"),
    )

    try:
        service.submit(
            workspace,
            1,
            fee_fragment(
                workspace.batches[1],
                value=4,
                text="Cash advance fee 4 percent",
            ),
        )
    except WorkspaceConflictError as exc:
        assert exc.conflict["propertyName"] == "pskg:fee"
        assert exc.conflict["existingValue"] == 699000
        assert exc.conflict["incomingValue"] == 4
        assert exc.conflict["existingEvidence"][0]["text"] == "Annual fee 699000"
        assert exc.conflict["incomingEvidence"][0]["text"] == "Cash advance fee 4 percent"
    else:
        raise AssertionError("Expected a fee conflict")


def test_multiple_fee_rules_merge_without_losing_fee_identity():
    service = IngestionWorkspaceService()
    workspace = service.begin(
        artifact_name="long.md",
        provenance=provenance(),
        chunks=chunks(10),
    )
    workspace = service.submit(
        workspace,
        0,
        pricing_rule_fragment(
            workspace.batches[0],
            rule_id="annual-fee-rule",
            condition="Annual fee 699000 VND",
        ),
    )
    workspace = service.submit(
        workspace,
        1,
        pricing_rule_fragment(
            workspace.batches[1],
            rule_id="cash-advance-fee-rule",
            condition="Cash advance fee 4 percent",
        ),
    )

    patch = service.merged_patch(workspace)
    rule_nodes = {
        node.temp_id: node
        for node in patch.nodes
        if node.class_name == "pskg:BusinessRule"
    }
    assert set(rule_nodes) == {"annual-fee-rule", "cash-advance-fee-rule"}
    assert len(patch.edges) == 2
    assert {edge.target_temp_id for edge in patch.edges} == set(rule_nodes)


def test_workspace_digest_change_invalidates_readiness_gate():
    service = IngestionWorkspaceService()
    workspace = service.begin(
        artifact_name="long.md",
        provenance=provenance(),
        chunks=chunks(1),
    )
    workspace.validated_fingerprint = "locked"

    assert service.is_current(
        workspace,
        provenance=provenance(),
    )
    assert not service.is_current(
        workspace,
        provenance=provenance(artifactDigest="changed"),
    )
    assert not service.is_current(
        workspace,
        provenance=provenance(ontologyDigest="changed"),
    )
    assert not service.is_current(
        workspace,
        provenance=provenance(skillDigest="changed"),
    )
