import asyncio
import json
from types import SimpleNamespace

from app.core.schemas.ingestion.document import DocumentChunk
from app.core.schemas.ingestion.extraction import ExtractionContext
from app.core.schemas.ingestion.graph_patch import GraphPatchFragment
from app.services.ingestion.orchestrator import (
    GeminiBatchExtractor,
    InvalidGraphPatchFragmentError,
)
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


class ReadyContextService:
    def prepare_uploaded_document(self, **kwargs):
        return ExtractionContext(
            document_name="product.md",
            chunks=[
                DocumentChunk(
                    index=0,
                    source="product.md",
                    section="Product",
                    content=(
                        "Product code P-1; effective 01/08/2026; Published; "
                        "has eligibility rule"
                    ),
                )
            ],
            ontology_context="ONTOLOGY",
        )


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
                "labelDistribution": {"BankingProduct": 1, "BusinessRule": 1},
                "relationshipTypeDistribution": {"HAS_ELIGIBILITY_RULE": 1},
                "mismatches": [],
            },
        }

    def close(self):
        self.closed = True


class TwoBatchReadyContextService:
    def prepare_uploaded_document(self, **kwargs):
        content = "Product code P-1; effective 01/08/2026; Published; has eligibility rule"
        return ExtractionContext(
            document_name="product.md",
            chunks=[
                DocumentChunk(
                    index=0,
                    source="product.md",
                    section="Product",
                    content=content,
                ),
                *[
                    DocumentChunk(
                        index=index,
                        source="product.md",
                        section=f"Section {index}",
                        content=f"Repeated reference chunk {index}",
                    )
                    for index in range(1, 6)
                ],
            ],
            ontology_context="ONTOLOGY",
        )


class QueueExtractor:
    def __init__(self, fragments):
        self.fragments = list(fragments)
        self.calls = []

    def extract_fragment(self, **kwargs):
        self.calls.append(kwargs)
        item = self.fragments.pop(0)
        if isinstance(item, Exception):
            raise item
        return GraphPatchFragment.model_validate(item)


class ShapeRepairExtractor:
    def __init__(self, repaired_fragment):
        self.calls = []
        self.repaired_fragment = repaired_fragment

    def extract_fragment(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            raise InvalidGraphPatchFragmentError(
                "LLM returned invalid GraphPatchFragment structure: list payload",
                summary={
                    "kind": "list",
                    "firstItemKeys": ["chunkStatus", "coverage", "entities"],
                    "forbiddenKeysPresent": ["entities", "chunkStatus"],
                },
            )
        return GraphPatchFragment.model_validate(self.repaired_fragment)


class RecordingGeminiModels:
    def __init__(self, response_text):
        self.response_text = response_text
        self.calls = []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(text=self.response_text)


class RecordingGeminiClient:
    def __init__(self, response_text):
        self.models = RecordingGeminiModels(response_text)


def batch_fragment(batch_payload, *, relevant_chunk=0):
    base = ready_fragment()
    base["coverage"] = [
        {
            "chunkIndex": index,
            "decision": "MAPPED" if index == relevant_chunk else "NOT_RELEVANT",
            "reason": "Batch reviewed",
        }
        for index in batch_payload["chunkIndexes"]
    ]
    if relevant_chunk not in batch_payload["chunkIndexes"]:
        base["nodes"] = []
        base["edges"] = []
    return base


def invalid_broad_fragment(batch_payload):
    return {
        "nodes": [],
        "edges": [],
        "coverage": [
            {"chunkIndex": index, "decision": "MAPPED", "reason": "Claimed facts"}
            for index in batch_payload["chunkIndexes"]
        ],
        "warnings": [],
    }


def test_gemini_extractor_uses_json_mode_without_response_schema():
    client = RecordingGeminiClient(json.dumps(batch_fragment({"chunkIndexes": [0]})))
    extractor = GeminiBatchExtractor(model="test-model", client=client)

    fragment = extractor.extract_fragment(
        batch_payload={"batchIndex": 0, "chunkIndexes": [0], "chunks": []},
        ontology_catalog="ONTOLOGY",
    )

    assert isinstance(fragment, GraphPatchFragment)
    call = client.models.calls[0]
    assert call["model"] == "test-model"
    assert call["config"].response_mime_type == "application/json"
    assert getattr(call["config"], "response_schema", None) is None


def test_gemini_extractor_accepts_fenced_json_response():
    payload = json.dumps(batch_fragment({"chunkIndexes": [0]}))
    client = RecordingGeminiClient(f"```json\n{payload}\n```")
    extractor = GeminiBatchExtractor(model="test-model", client=client)

    fragment = extractor.extract_fragment(
        batch_payload={"batchIndex": 0, "chunkIndexes": [0], "chunks": []},
        ontology_catalog="ONTOLOGY",
    )

    assert fragment.coverage[0].chunk_index == 0


def test_gemini_extractor_reports_invalid_fragment_json():
    client = RecordingGeminiClient("{not json")
    extractor = GeminiBatchExtractor(model="test-model", client=client)

    try:
        extractor.extract_fragment(
            batch_payload={"batchIndex": 0, "chunkIndexes": [0], "chunks": []},
            ontology_catalog="ONTOLOGY",
        )
    except ValueError as exc:
        assert "LLM returned invalid GraphPatchFragment JSON" in str(exc)
    else:
        raise AssertionError("Expected invalid JSON to fail")


def test_gemini_extractor_reports_invalid_fragment_structure():
    client = RecordingGeminiClient(json.dumps({"nodes": [], "edges": []}))
    extractor = GeminiBatchExtractor(model="test-model", client=client)

    try:
        extractor.extract_fragment(
            batch_payload={"batchIndex": 0, "chunkIndexes": [0], "chunks": []},
            ontology_catalog="ONTOLOGY",
        )
    except ValueError as exc:
        assert "LLM returned invalid GraphPatchFragment structure" in str(exc)
    else:
        raise AssertionError("Expected invalid structure to fail")


def test_gemini_extractor_prompt_names_canonical_shape_and_forbidden_keys():
    prompt = GeminiBatchExtractor._prompt(
        batch_payload={"batchIndex": 0, "chunkIndexes": [0], "chunks": []},
        ontology_catalog="ONTOLOGY",
        previous_error={
            "errorKind": "invalid_graph_patch_fragment",
            "shapeSummary": {"firstItemKeys": ["entities", "chunkStatus"]},
        },
    )

    assert "Top-level keys must be exactly: nodes, edges, coverage, warnings" in prompt
    assert "Return one JSON object, never an array" in prompt
    assert "Forbidden keys anywhere in the response: entities, chunkStatus" in prompt
    assert '"propertyName": "pskg:productCode"' in prompt
    assert "convert it to the canonical GraphPatchFragment object" in prompt
    assert "Prefer list-valued pskg:productAttributes" in prompt
    assert "one verbatim evidence row per item" in prompt
    assert "Do not stuff independent fee" in prompt
    assert "scalar BankingProduct.pskg:fee" in prompt
    assert "create one pskg:BusinessRule per distinct fee/pricing fact" in prompt
    assert "pskg:hasSalesConditionRule" in prompt


def test_end_to_end_processes_all_batches_then_fills(monkeypatch):
    context = FakeToolContext()
    monkeypatch.setattr(
        ingestion_tools,
        "_get_context_service",
        lambda: TwoBatchReadyContextService(),
    )
    extractor = QueueExtractor(
        [
            batch_fragment({"chunkIndexes": [0, 1, 2, 3, 4]}),
            batch_fragment({"chunkIndexes": [5]}, relevant_chunk=-1),
        ]
    )
    monkeypatch.setattr(ingestion_tools, "_get_batch_extractor", lambda: extractor)
    fill_service = ReceiptFillService()
    monkeypatch.setattr(
        ingestion_tools,
        "create_fill_service",
        lambda **kwargs: fill_service,
    )

    result = asyncio.run(
        ingestion_tools.ingest_document_end_to_end("product.md", context)
    )

    assert result["success"] is True
    assert result["stage"] == "completed"
    assert result["terminal"] is True
    assert result["workspaceStats"]["processedBatches"] == 2
    assert len(extractor.calls) == 2
    assert fill_service.closed is True


def test_end_to_end_retries_failed_batch_then_continues(monkeypatch):
    context = FakeToolContext()
    monkeypatch.setattr(
        ingestion_tools,
        "_get_context_service",
        lambda: TwoBatchReadyContextService(),
    )
    extractor = QueueExtractor(
        [
            invalid_broad_fragment({"chunkIndexes": [0, 1, 2, 3, 4]}),
            batch_fragment({"chunkIndexes": [0, 1, 2, 3, 4]}),
            batch_fragment({"chunkIndexes": [5]}, relevant_chunk=-1),
        ]
    )
    monkeypatch.setattr(ingestion_tools, "_get_batch_extractor", lambda: extractor)
    monkeypatch.setattr(
        ingestion_tools,
        "create_fill_service",
        lambda **kwargs: ReceiptFillService(),
    )

    result = asyncio.run(
        ingestion_tools.ingest_document_end_to_end("product.md", context)
    )

    assert result["success"] is True
    assert result["workspaceStats"]["processedBatches"] == 2
    assert len(extractor.calls) == 3
    assert extractor.calls[1]["previous_error"]["errorSummary"]["codes"] == [
        "COVERAGE_NOT_EVIDENCED"
    ]


def test_end_to_end_retries_invalid_fragment_shape_then_continues(monkeypatch):
    context = FakeToolContext()
    monkeypatch.setattr(
        ingestion_tools,
        "_get_context_service",
        lambda: ReadyContextService(),
    )
    extractor = ShapeRepairExtractor(ready_fragment())
    monkeypatch.setattr(ingestion_tools, "_get_batch_extractor", lambda: extractor)
    monkeypatch.setattr(
        ingestion_tools,
        "create_fill_service",
        lambda **kwargs: ReceiptFillService(),
    )

    result = asyncio.run(
        ingestion_tools.ingest_document_end_to_end("product.md", context)
    )

    assert result["success"] is True
    assert result["stage"] == "completed"
    assert len(extractor.calls) == 2
    assert extractor.calls[1]["previous_error"]["errorKind"] == (
        "invalid_graph_patch_fragment"
    )
    assert "entities" in extractor.calls[1]["previous_error"]["repairInstructions"]


def test_end_to_end_stops_after_invalid_fragment_shape_retries(monkeypatch):
    context = FakeToolContext()
    monkeypatch.setattr(
        ingestion_tools,
        "_get_context_service",
        lambda: ReadyContextService(),
    )
    extractor = QueueExtractor(
        [
            InvalidGraphPatchFragmentError(
                "LLM returned invalid GraphPatchFragment structure: list payload",
                summary={"kind": "list"},
            ),
            InvalidGraphPatchFragmentError(
                "LLM returned invalid GraphPatchFragment structure: list payload",
                summary={"kind": "list"},
            ),
        ]
    )
    monkeypatch.setattr(ingestion_tools, "_get_batch_extractor", lambda: extractor)
    monkeypatch.setattr(
        ingestion_tools,
        "create_fill_service",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("must not fill")),
    )

    result = asyncio.run(
        ingestion_tools.ingest_document_end_to_end(
            "product.md",
            context,
            max_retries_per_batch=2,
        )
    )

    assert result["success"] is False
    assert result["stage"] == "explicit_extraction_failure"
    assert result["terminal"] is True
    assert result["attempt"] == 2
    assert result["errorKind"] == "invalid_graph_patch_fragment"
    assert result["errors"][0]["code"] == "ORCHESTRATION_FAILED"


def test_end_to_end_stops_on_unchanged_retry(monkeypatch):
    context = FakeToolContext()
    monkeypatch.setattr(
        ingestion_tools,
        "_get_context_service",
        lambda: TwoBatchReadyContextService(),
    )
    bad = invalid_broad_fragment({"chunkIndexes": [0, 1, 2, 3, 4]})
    extractor = QueueExtractor([bad, bad])
    monkeypatch.setattr(ingestion_tools, "_get_batch_extractor", lambda: extractor)
    monkeypatch.setattr(
        ingestion_tools,
        "create_fill_service",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("must not fill")),
    )

    result = asyncio.run(
        ingestion_tools.ingest_document_end_to_end(
            "product.md",
            context,
            max_retries_per_batch=2,
        )
    )

    assert result["success"] is False
    assert result["stage"] == "explicit_extraction_failure"
    assert result["terminal"] is True
    assert "UNCHANGED_RETRY" in {item["code"] for item in result["errors"]}


def test_end_to_end_reports_llm_request_config_error(monkeypatch):
    context = FakeToolContext()
    monkeypatch.setattr(
        ingestion_tools,
        "_get_context_service",
        lambda: TwoBatchReadyContextService(),
    )
    extractor = QueueExtractor(
        [
            RuntimeError(
                "Invalid JSON payload received. Unknown name "
                '"additional_properties" at '
                "'generation_config.response_schema'"
            )
        ]
    )
    monkeypatch.setattr(ingestion_tools, "_get_batch_extractor", lambda: extractor)
    monkeypatch.setattr(
        ingestion_tools,
        "create_fill_service",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("must not fill")),
    )

    result = asyncio.run(
        ingestion_tools.ingest_document_end_to_end("product.md", context)
    )

    assert result["success"] is False
    assert result["stage"] == "explicit_extraction_failure"
    assert result["terminal"] is True
    assert result["errorKind"] == "llm_request_config"
    assert "uploaded document was not processed" in result["errors"][0]["message"]


def test_end_to_end_stops_at_readiness_gate_without_fill(monkeypatch):
    context = FakeToolContext()
    monkeypatch.setattr(
        ingestion_tools,
        "_get_context_service",
        lambda: ReadyContextService(),
    )
    extractor = QueueExtractor([fragment_without_authoritative_status()])
    monkeypatch.setattr(ingestion_tools, "_get_batch_extractor", lambda: extractor)
    monkeypatch.setattr(
        ingestion_tools,
        "create_fill_service",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("must not fill")),
    )

    result = asyncio.run(
        ingestion_tools.ingest_document_end_to_end("product.md", context)
    )

    assert result["success"] is True
    assert result["stage"] == "readiness_gate"
    assert result["terminal"] is True
    assert result["validForPersistence"] is False


def test_end_to_end_reports_persistence_error(monkeypatch):
    context = FakeToolContext()
    monkeypatch.setattr(
        ingestion_tools,
        "_get_context_service",
        lambda: ReadyContextService(),
    )
    extractor = QueueExtractor([ready_fragment()])
    monkeypatch.setattr(ingestion_tools, "_get_batch_extractor", lambda: extractor)
    monkeypatch.setattr(
        ingestion_tools,
        "create_fill_service",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("neo4j unavailable")),
    )

    result = asyncio.run(
        ingestion_tools.ingest_document_end_to_end("product.md", context)
    )

    assert result["success"] is False
    assert result["stage"] == "persistence"
    assert result["terminal"] is True
    assert result["errors"][0]["code"] == "NEO4J_WRITE_FAILED"
