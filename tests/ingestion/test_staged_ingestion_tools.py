import asyncio
import json
from types import SimpleNamespace

from google.adk.tools import FunctionTool

from app.core.schemas.ingestion.document import DocumentChunk
from app.core.schemas.ingestion.extraction import ExtractionContext
from app.tools import ingestion_tools


class FakeToolContext:
    def __init__(self):
        self.state = {}
        self.saved = []
        self.artifact = SimpleNamespace(
            inline_data=SimpleNamespace(
                data=b"# Product\nProduct code P-1; effective 01/08/2026",
                mime_type="text/markdown",
            ),
            text=None,
        )

    async def load_artifact(self, filename: str):
        return self.artifact

    async def save_artifact(self, filename: str, artifact, custom_metadata=None):
        self.saved.append((filename, artifact, custom_metadata))
        return 1


class FakeContextService:
    def prepare_uploaded_document(self, **kwargs):
        return ExtractionContext(
            document_name="product.md",
            chunks=[
                DocumentChunk(
                    index=0,
                    source="product.md",
                    section="Product",
                    content="Product code P-1; effective 01/08/2026",
                )
            ],
            ontology_context="ONTOLOGY",
        )


class MultiChunkContextService:
    def prepare_uploaded_document(self, **kwargs):
        chunks = [
            DocumentChunk(
                index=index,
                source="flexi.md",
                section=f"Section {index}",
                content=f"Business guidance chunk {index}",
            )
            for index in range(25)
        ]
        chunks[1] = DocumentChunk(
            index=1,
            source="flexi.md",
            section="1. Thong tin tai lieu",
            content=(
                "| Noi dung | Thong tin |\n"
                "|---|---|\n"
                "| Ma san pham | CC-FLEXI-001 |"
            ),
        )
        return ExtractionContext(
            document_name="flexi.md",
            chunks=chunks,
            ontology_context="ONTOLOGY",
        )


class FeeConflictContextService:
    def prepare_uploaded_document(self, **kwargs):
        return ExtractionContext(
            document_name="fees.md",
            chunks=[
                DocumentChunk(
                    index=0,
                    source="fees.md",
                    section="Fees",
                    content="Annual fee 699000 VND",
                ),
                *[
                    DocumentChunk(
                        index=index,
                        source="fees.md",
                        section=f"Section {index}",
                        content=f"No fee fact chunk {index}",
                    )
                    for index in range(1, 5)
                ],
                DocumentChunk(
                    index=5,
                    source="fees.md",
                    section="Fees",
                    content="Cash advance fee 4 percent",
                ),
            ],
            ontology_context="ONTOLOGY",
        )


def fragment_without_authoritative_status():
    evidence = [
        {
            "source": "product.md",
            "chunkIndex": 0,
            "section": "Product",
            "text": "Product code P-1; effective 01/08/2026",
        }
    ]
    return {
        "nodes": [
            {
                "tempId": "product-1",
                "className": "pskg:BankingProduct",
                "properties": [
                    {
                        "propertyName": "pskg:productCode",
                        "value": "P-1",
                        "evidence": evidence,
                    },
                    {
                        "propertyName": "pskg:bankingProductEffectiveFrom",
                        "value": "2026-08-01",
                        "evidence": evidence,
                    },
                ],
                "evidence": evidence,
                "confidence": 1.0,
            }
        ],
        "edges": [],
        "coverage": [
            {"chunkIndex": 0, "decision": "MAPPED", "reason": "Product facts"}
        ],
        "warnings": [],
    }


def ready_fragment():
    product = fragment_without_authoritative_status()
    product_evidence = product["nodes"][0]["evidence"]
    product["nodes"][0]["properties"].append(
        {
            "propertyName": "pskg:bankingProductStatus",
            "value": "Published",
            "evidence": [
                {
                    **product_evidence[0],
                    "text": "Product code P-1; effective 01/08/2026; Published",
                }
            ],
        }
    )
    product["nodes"][0]["evidence"] = [
        {
            **product_evidence[0],
            "text": "Product code P-1; effective 01/08/2026; Published; has eligibility rule",
        }
    ]
    rule_evidence = [
        {
            **product_evidence[0],
            "text": "Product code P-1; effective 01/08/2026; Published; has eligibility rule",
        }
    ]
    product["nodes"].append(
        {
            "tempId": "rule-1",
            "className": "pskg:BusinessRule",
            "properties": [
                {
                    "propertyName": "pskg:businessRuleStatus",
                    "value": "Published",
                    "evidence": rule_evidence,
                }
            ],
            "evidence": rule_evidence,
            "confidence": 1.0,
        }
    )
    product["edges"].append(
        {
            "edgeName": "pskg:hasEligibilityRule",
            "sourceTempId": "product-1",
            "targetTempId": "rule-1",
            "evidence": rule_evidence,
            "confidence": 1.0,
        }
    )
    return product


def scalar_fee_fragment(*, chunk_index, value, text, coverage_indexes):
    evidence = [
        {
            "source": "fees.md",
            "chunkIndex": chunk_index,
            "section": "Fees",
            "text": text,
        }
    ]
    return {
        "nodes": [
            {
                "tempId": "product-fees",
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
                "decision": "MAPPED" if index == chunk_index else "NOT_RELEVANT",
                "reason": "Batch reviewed",
            }
            for index in coverage_indexes
        ],
        "warnings": [],
    }


class ReadyContextService(FakeContextService):
    def prepare_uploaded_document(self, **kwargs):
        context = super().prepare_uploaded_document(**kwargs)
        context.chunks[0].content += "; Published; has eligibility rule"
        return context


class ReceiptFillService:
    def __init__(self):
        self.closed = False

    def fill(self, *args, **kwargs):
        return {
            "status": "success",
            "commitStatus": "committed",
            "nodes": 2,
            "edges": 1,
            "nodeIds": {"product-1": "n1", "rule-1": "n2"},
            "relationshipIds": {"edge": "r1"},
            "receipt": {
                "version": "1",
                "commitStatus": "committed",
                "verified": True,
                "expectedNodeCount": 2,
                "expectedRelationshipCount": 1,
                "nodeIds": {"product-1": "n1", "rule-1": "n2"},
                "relationshipIds": {"edge": "r1"},
                "nodes": [
                    {
                        "nodeId": "n1",
                        "labels": ["BankingProduct"],
                        "properties": {"productCode": "P-1"},
                    },
                    {
                        "nodeId": "n2",
                        "labels": ["BusinessRule"],
                        "properties": {"ruleType": "ELIGIBILITY"},
                    },
                ],
                "relationships": [
                    {
                        "relationshipId": "r1",
                        "type": "HAS_ELIGIBILITY_RULE",
                        "sourceNodeId": "n1",
                        "targetNodeId": "n2",
                        "properties": {},
                    }
                ],
                "labelDistribution": {"BankingProduct": 1, "BusinessRule": 1},
                "relationshipTypeDistribution": {"HAS_ELIGIBILITY_RULE": 1},
                "mismatches": [],
            },
        }

    def close(self):
        self.closed = True


class MismatchFillService(ReceiptFillService):
    def fill(self, *args, **kwargs):
        result = super().fill(*args, **kwargs)
        result["status"] = "readback_mismatch"
        result["receipt"]["verified"] = False
        result["receipt"]["mismatches"] = ["node productCode mismatch"]
        return result


def test_staged_workflow_finishes_extraction_but_blocks_fill(monkeypatch):
    context = FakeToolContext()
    monkeypatch.setattr(
        ingestion_tools,
        "_get_context_service",
        lambda: FakeContextService(),
    )
    begin = asyncio.run(ingestion_tools.begin_ingestion("product.md", context))
    ingestion_id = begin["ingestionId"]

    submitted = ingestion_tools.submit_ingestion_batch(
        ingestion_id,
        0,
        fragment_without_authoritative_status(),
        context,
    )
    finalized = ingestion_tools.finalize_ingestion(ingestion_id, context)

    assert submitted["processedBatches"] == 1
    assert submitted["remainingBatches"] == 0
    assert finalized["validForExtraction"] is True
    assert finalized["validForPersistence"] is False
    assert "ONTOLOGY_RULE_UNSATISFIED" in {
        item["code"] for item in finalized["readinessIssues"]
    }

    monkeypatch.setattr(
        ingestion_tools,
        "create_fill_service",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("must not fill")),
    )
    blocked = asyncio.run(ingestion_tools.fill_ingestion(ingestion_id, context))
    assert blocked["stage"] == "validation_precondition"


def test_staged_tool_schemas_do_not_accept_a_full_patch_for_fill():
    end_to_end_schema = FunctionTool(
        ingestion_tools.ingest_document_end_to_end
    )._get_declaration().parameters_json_schema
    begin_schema = FunctionTool(
        ingestion_tools.begin_ingestion
    )._get_declaration().parameters_json_schema
    submit_schema = FunctionTool(
        ingestion_tools.submit_ingestion_batch
    )._get_declaration().parameters_json_schema
    fill_schema = FunctionTool(
        ingestion_tools.fill_ingestion
    )._get_declaration().parameters_json_schema

    assert set(end_to_end_schema["properties"]) == {
        "artifact_name",
        "persist",
        "max_retries_per_batch",
    }
    assert set(begin_schema["properties"]) == {"artifact_name"}
    assert set(submit_schema["properties"]) == {
        "ingestion_id",
        "batch_index",
        "graph_fragment",
    }
    assert set(fill_schema["properties"]) == {"ingestion_id"}


def test_batch_zero_success_is_non_terminal_when_batches_remain(monkeypatch):
    context = FakeToolContext()
    monkeypatch.setattr(
        ingestion_tools,
        "_get_context_service",
        lambda: MultiChunkContextService(),
    )
    begin = asyncio.run(ingestion_tools.begin_ingestion("flexi.md", context))
    row = "| Ma san pham | CC-FLEXI-001 |"

    result = ingestion_tools.submit_ingestion_batch(
        begin["ingestionId"],
        0,
        {
            "nodes": [
                {
                    "tempId": "cc-flexi-001",
                    "className": "pskg:BankingProduct",
                    "properties": [
                        {
                            "propertyName": "pskg:productCode",
                            "value": "CC-FLEXI-001",
                            "evidence": [
                                {
                                    "source": "flexi.md",
                                    "chunkIndex": 1,
                                    "section": "1. Thong tin tai lieu",
                                    "text": row,
                                }
                            ],
                        }
                    ],
                    "evidence": [
                        {
                            "source": "flexi.md",
                            "chunkIndex": 1,
                            "section": "1. Thong tin tai lieu",
                            "text": row,
                        }
                    ],
                    "confidence": 1.0,
                }
            ],
            "edges": [],
            "coverage": [
                {
                    "chunkIndex": index,
                    "decision": "MAPPED" if index == 1 else "NOT_RELEVANT",
                    "reason": "Batch reviewed",
                }
                for index in begin["nextBatch"]["chunkIndexes"]
            ],
            "warnings": [],
        },
        context,
    )

    assert result["success"] is True
    assert result["terminal"] is False
    assert result["processedBatches"] == 1
    assert result["remainingBatches"] > 0
    assert result["stage"] == "batching"
    assert context.saved == []


def test_fill_saves_full_receipt_artifact_and_returns_only_summary(monkeypatch):
    context = FakeToolContext()
    monkeypatch.setattr(
        ingestion_tools,
        "_get_context_service",
        lambda: ReadyContextService(),
    )
    begin = asyncio.run(ingestion_tools.begin_ingestion("product.md", context))
    ingestion_id = begin["ingestionId"]
    ingestion_tools.submit_ingestion_batch(
        ingestion_id,
        0,
        ready_fragment(),
        context,
    )
    finalized = ingestion_tools.finalize_ingestion(ingestion_id, context)
    assert finalized["validForPersistence"] is True
    service = ReceiptFillService()
    monkeypatch.setattr(
        ingestion_tools,
        "create_fill_service",
        lambda **kwargs: service,
    )

    result = asyncio.run(ingestion_tools.fill_ingestion(ingestion_id, context))

    assert result["success"] is True
    assert result["verificationStatus"] == "verified"
    assert result["labelDistribution"] == {
        "BankingProduct": 1,
        "BusinessRule": 1,
    }
    assert "receipt" not in result
    assert "nodeIds" not in result
    assert result["artifactVersion"] == 1
    assert len(context.saved) == 1
    artifact_name, artifact, metadata = context.saved[0]
    assert artifact_name == result["artifactName"]
    assert json.loads(artifact.text)["nodes"][0]["properties"] == {
        "productCode": "P-1"
    }
    assert metadata["receiptVersion"] == "1"
    assert service.closed is True


def test_fill_reports_committed_readback_mismatch_without_rollback_claim(monkeypatch):
    context = FakeToolContext()
    monkeypatch.setattr(
        ingestion_tools,
        "_get_context_service",
        lambda: ReadyContextService(),
    )
    begin = asyncio.run(ingestion_tools.begin_ingestion("product.md", context))
    ingestion_id = begin["ingestionId"]
    ingestion_tools.submit_ingestion_batch(
        ingestion_id,
        0,
        ready_fragment(),
        context,
    )
    assert ingestion_tools.finalize_ingestion(
        ingestion_id,
        context,
    )["validForPersistence"]
    monkeypatch.setattr(
        ingestion_tools,
        "create_fill_service",
        lambda **kwargs: MismatchFillService(),
    )

    result = asyncio.run(ingestion_tools.fill_ingestion(ingestion_id, context))

    assert result["success"] is False
    assert result["stage"] == "readback"
    assert result["commitStatus"] == "committed"
    assert result["verificationStatus"] == "mismatch"
    assert result["mismatchCount"] == 1
    assert result["artifactName"]


def test_submit_rejects_mapped_chunk_without_fact_evidence_and_returns_retry_batch(monkeypatch):
    context = FakeToolContext()
    monkeypatch.setattr(
        ingestion_tools,
        "_get_context_service",
        lambda: FakeContextService(),
    )
    begin = asyncio.run(ingestion_tools.begin_ingestion("product.md", context))
    ingestion_id = begin["ingestionId"]

    result = ingestion_tools.submit_ingestion_batch(
        ingestion_id,
        0,
        {
            "nodes": [],
            "edges": [],
            "coverage": [
                {"chunkIndex": 0, "decision": "MAPPED", "reason": "Product facts"}
            ],
            "warnings": [],
        },
        context,
    )

    assert result["success"] is False
    assert result["stage"] == "batch_validation"
    assert result["retryRequired"] is True
    assert result["nextAction"] == "correct_and_resubmit_same_batch"
    assert list(result).index("errors") < list(result).index("nextBatch")
    assert list(result).index("errorSummary") < list(result).index("nextBatch")
    assert list(result).index("repairInstructions") < list(result).index("nextBatch")
    assert result["errorSummary"]["codes"] == ["COVERAGE_NOT_EVIDENCED"]
    assert result["errorSummary"]["coverageNotEvidencedChunkIndexes"] == [0]
    assert result["nextBatch"]["batchIndex"] == 0
    assert result["nextBatch"]["chunkIndexes"] == [0]
    assert [chunk["index"] for chunk in result["affectedChunks"]] == [0]
    assert "COVERAGE_NOT_EVIDENCED" in {item["code"] for item in result["errors"]}
    workspace = ingestion_tools._load_workspace(context)
    assert workspace is not None
    assert sum(batch.fragment is not None for batch in workspace.batches) == 0


def test_submit_schema_error_summarizes_missing_evidence_source(monkeypatch):
    context = FakeToolContext()
    monkeypatch.setattr(
        ingestion_tools,
        "_get_context_service",
        lambda: FakeContextService(),
    )
    begin = asyncio.run(ingestion_tools.begin_ingestion("product.md", context))

    result = ingestion_tools.submit_ingestion_batch(
        begin["ingestionId"],
        0,
        {
            "nodes": [
                {
                    "tempId": "product-1",
                    "className": "pskg:BankingProduct",
                    "properties": [
                        {
                            "propertyName": "pskg:productCode",
                            "value": "P-1",
                            "evidence": [
                                {
                                    "chunkIndex": 0,
                                    "section": "Product",
                                    "text": "Product code P-1; effective 01/08/2026",
                                }
                            ],
                        }
                    ],
                    "evidence": [
                        {
                            "chunkIndex": 0,
                            "section": "Product",
                            "text": "Product code P-1; effective 01/08/2026",
                        }
                    ],
                    "confidence": 1.0,
                }
            ],
            "edges": [],
            "coverage": [
                {"chunkIndex": 0, "decision": "MAPPED", "reason": "Product facts"}
            ],
            "warnings": [],
        },
        context,
    )

    assert result["success"] is False
    assert result["stage"] == "batch_validation"
    assert result["retryRequired"] is True
    assert result["errorSummary"]["codes"] == ["BATCH_CONFLICT"]
    assert result["errorSummary"]["coverageNotEvidencedChunkIndexes"] == []
    assert result["errorSummary"]["schemaErrorLocations"] == [
        "nodes.0.properties.0.evidence.0.source",
        "nodes.0.evidence.0.source",
    ]
    assert "evidence objects must include source" in result["repairInstructions"]
    assert result["affectedChunks"] == []
    assert list(result).index("errors") < list(result).index("nextBatch")
    assert result["nextBatch"]["chunkIndexes"] == [0]


def test_submit_batch_conflict_returns_existing_and_incoming_fee_context(monkeypatch):
    context = FakeToolContext()
    monkeypatch.setattr(
        ingestion_tools,
        "_get_context_service",
        lambda: FeeConflictContextService(),
    )
    begin = asyncio.run(ingestion_tools.begin_ingestion("fees.md", context))
    first = ingestion_tools.submit_ingestion_batch(
        begin["ingestionId"],
        0,
        scalar_fee_fragment(
            chunk_index=0,
            value=699000,
            text="Annual fee 699000 VND",
            coverage_indexes=[0, 1, 2, 3, 4],
        ),
        context,
    )
    assert first["success"] is True

    result = ingestion_tools.submit_ingestion_batch(
        begin["ingestionId"],
        1,
        scalar_fee_fragment(
            chunk_index=5,
            value=4,
            text="Cash advance fee 4 percent",
            coverage_indexes=[5],
        ),
        context,
    )

    assert result["success"] is False
    assert result["stage"] == "batch_validation"
    assert result["retryRequired"] is True
    assert result["errorSummary"]["codes"] == ["BATCH_CONFLICT"]
    assert list(result).index("conflict") < list(result).index("nextBatch")
    assert result["conflict"]["batchIndex"] == 1
    assert result["conflict"]["nodeTempId"] == "product-fees"
    assert result["conflict"]["propertyName"] == "pskg:fee"
    assert result["conflict"]["existingValue"] == 699000
    assert result["conflict"]["incomingValue"] == 4
    assert result["conflict"]["existingEvidence"][0]["text"] == "Annual fee 699000 VND"
    assert result["conflict"]["incomingEvidence"][0]["text"] == "Cash advance fee 4 percent"
    assert "pskg:BusinessRule" in result["repairInstructions"]
    assert "pskg:hasSalesConditionRule" in result["repairInstructions"]


def test_submit_rejects_flexi_batch_with_broad_coverage_but_only_one_fact(monkeypatch):
    context = FakeToolContext()
    monkeypatch.setattr(
        ingestion_tools,
        "_get_context_service",
        lambda: MultiChunkContextService(),
    )
    begin = asyncio.run(ingestion_tools.begin_ingestion("flexi.md", context))
    row = "| Ma san pham | CC-FLEXI-001 |"
    batch_indexes = begin["nextBatch"]["chunkIndexes"]
    result = ingestion_tools.submit_ingestion_batch(
        begin["ingestionId"],
        0,
        {
            "nodes": [
                {
                    "tempId": "cc-flexi-001",
                    "className": "pskg:BankingProduct",
                    "properties": [
                        {
                            "propertyName": "pskg:productCode",
                            "value": "CC-FLEXI-001",
                            "evidence": [
                                {
                                    "source": "flexi.md",
                                    "chunkIndex": 1,
                                    "section": "1. Thong tin tai lieu",
                                    "text": row,
                                }
                            ],
                        }
                    ],
                    "evidence": [
                        {
                            "source": "flexi.md",
                            "chunkIndex": 1,
                            "section": "1. Thong tin tai lieu",
                            "text": row,
                        }
                    ],
                    "confidence": 1.0,
                }
            ],
            "edges": [],
            "coverage": [
                {
                    "chunkIndex": index,
                    "decision": "MAPPED",
                    "reason": "Claimed batch fact",
                }
                for index in batch_indexes
            ],
            "warnings": [],
        },
        context,
    )

    assert result["success"] is False
    assert result["stage"] == "batch_validation"
    assert result["retryRequired"] is True
    assert result["nextBatch"]["chunkIndexes"] == batch_indexes
    assert result["errorSummary"]["codes"] == ["COVERAGE_NOT_EVIDENCED"]
    assert result["errorSummary"]["coverageNotEvidencedChunkIndexes"] == [
        index for index in batch_indexes if index != 1
    ]
    not_evidenced = {
        item["location"]
        for item in result["errors"]
        if item["code"] == "COVERAGE_NOT_EVIDENCED"
    }
    assert "coverage.0" in not_evidenced
    assert "coverage.4" in not_evidenced
    assert "coverage.1" not in not_evidenced
    assert [chunk["index"] for chunk in result["affectedChunks"]] == [
        index for index in batch_indexes if index != 1
    ]
    assert all(chunk["index"] != 1 for chunk in result["affectedChunks"])
    assert "Do not resubmit unchanged coverage" in result["repairInstructions"]
    assert list(result).index("errorSummary") < list(result).index("nextBatch")
    workspace = ingestion_tools._load_workspace(context)
    assert workspace is not None
    assert sum(batch.fragment is not None for batch in workspace.batches) == 0


def test_submit_returns_unchanged_retry_when_unsupported_coverage_does_not_improve(monkeypatch):
    context = FakeToolContext()
    monkeypatch.setattr(
        ingestion_tools,
        "_get_context_service",
        lambda: MultiChunkContextService(),
    )
    begin = asyncio.run(ingestion_tools.begin_ingestion("flexi.md", context))
    row = "| Ma san pham | CC-FLEXI-001 |"
    payload = {
        "nodes": [
            {
                "tempId": "cc-flexi-001",
                "className": "pskg:BankingProduct",
                "properties": [
                    {
                        "propertyName": "pskg:productCode",
                        "value": "CC-FLEXI-001",
                        "evidence": [
                            {
                                "source": "flexi.md",
                                "chunkIndex": 1,
                                "section": "1. Thong tin tai lieu",
                                "text": row,
                            }
                        ],
                    }
                ],
                "evidence": [
                    {
                        "source": "flexi.md",
                        "chunkIndex": 1,
                        "section": "1. Thong tin tai lieu",
                        "text": row,
                    }
                ],
                "confidence": 1.0,
            }
        ],
        "edges": [],
        "coverage": [
            {"chunkIndex": index, "decision": "MAPPED", "reason": "Claimed batch fact"}
            for index in begin["nextBatch"]["chunkIndexes"]
        ],
        "warnings": [],
    }

    first = ingestion_tools.submit_ingestion_batch(
        begin["ingestionId"],
        0,
        payload,
        context,
    )
    second = ingestion_tools.submit_ingestion_batch(
        begin["ingestionId"],
        0,
        payload,
        context,
    )

    assert first["retryRequired"] is True
    assert second["success"] is False
    assert second["retryRequired"] is False
    assert second["nextAction"] == "explicit_extraction_failure"
    assert "UNCHANGED_RETRY" in {item["code"] for item in second["errors"]}
    assert second["errorSummary"]["coverageNotEvidencedChunkIndexes"] == [
        index for index in begin["nextBatch"]["chunkIndexes"] if index != 1
    ]
    assert "nextBatch" not in second


def test_submit_repeated_batch_conflict_stops_retry_loop(monkeypatch):
    context = FakeToolContext()
    monkeypatch.setattr(
        ingestion_tools,
        "_get_context_service",
        lambda: FeeConflictContextService(),
    )
    begin = asyncio.run(ingestion_tools.begin_ingestion("fees.md", context))
    accepted = ingestion_tools.submit_ingestion_batch(
        begin["ingestionId"],
        0,
        scalar_fee_fragment(
            chunk_index=0,
            value=699000,
            text="Annual fee 699000 VND",
            coverage_indexes=[0, 1, 2, 3, 4],
        ),
        context,
    )
    assert accepted["success"] is True

    conflicting = scalar_fee_fragment(
        chunk_index=5,
        value=4,
        text="Cash advance fee 4 percent",
        coverage_indexes=[5],
    )
    first_conflict = ingestion_tools.submit_ingestion_batch(
        begin["ingestionId"], 1, conflicting, context
    )
    repeated_conflict = ingestion_tools.submit_ingestion_batch(
        begin["ingestionId"], 1, conflicting, context
    )

    assert first_conflict["retryRequired"] is True
    assert repeated_conflict["success"] is False
    assert repeated_conflict["retryRequired"] is False
    assert repeated_conflict["nextAction"] == "explicit_extraction_failure"
    assert "UNCHANGED_RETRY" in {
        item["code"] for item in repeated_conflict["errors"]
    }
    assert "nextBatch" not in repeated_conflict
