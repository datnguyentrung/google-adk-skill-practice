import asyncio
import os
import random
from types import SimpleNamespace

import httpx
from google import genai

from app.core.schemas.ingestion.graph_patch import GraphPatchFragment
from app.services.ingestion import use_case as ingestion_use_case
from app.services.ingestion.model_call_control import StageLocalModelCallExhausted
from app.services.ingestion.orchestrator import InvalidGraphPatchFragmentError
from app.services.ingestion.semantic_placement import GeminiAtomicFactExtractor

DOC_TEXT = (
    "# HƯỚNG DẪN NGHIỆP VỤ SẢN PHẨM THẺ TÍN DỤNG FLEXI REWARDS\n\n"
    "## Tên sản phẩm\n\n"
    "| Tên sản phẩm | Thẻ FLEXI Rewards |\n\n"
    "## Điều kiện và tính năng\n\n"
    "Khách hàng cá nhân có thu nhập ổn định. Sản phẩm không có phí thường niên.\n\n"
)


class RateLimitError(Exception):
    status_code = 429


class ConfigError(Exception):
    status_code = 400


class FakeArtifact:
    def __init__(self, text: str):
        self.inline_data = None
        self.text = text


class FakeToolContext:
    def __init__(self, artifacts: dict[str, str]):
        self.state = {}
        self._artifacts = artifacts
        self.saved_artifacts = {}

    async def load_artifact(self, filename: str):
        text = self._artifacts.get(filename)
        if text is None:
            return None
        return FakeArtifact(text)

    async def save_artifact(self, filename: str, artifact, **kwargs):
        self.saved_artifacts[filename] = artifact
        return 1


def _no_fact_fragment(batch_payload: dict) -> GraphPatchFragment:
    coverage = []
    for chunk in batch_payload.get("chunks", []):
        chunk_index = chunk.get("index")
        if chunk_index is None:
            chunk_index = chunk.get("chunkIndex")
        coverage.append(
            {
                "chunkIndex": int(chunk_index),
                "decision": "NO_RELEVANT_FACT",
                "reason": "No ontology-representable fact in this test chunk.",
            }
        )
    return GraphPatchFragment(nodes=[], edges=[], coverage=coverage, warnings=[])


class FakeExtractor:
    def __init__(self, failures=None, fragment_factory=_no_fact_fragment):
        self._failures = list(failures or [])
        self.fragment_factory = fragment_factory
        self.calls = []
        self.repair_calls = []

    def extract_fragment(self, **kwargs):
        self.calls.append(
            {
                "batch_payload": kwargs.get("batch_payload"),
                "previous_error": kwargs.get("previous_error"),
                "graph_context": kwargs.get("graph_context"),
            }
        )
        if self._failures:
            failure = self._failures.pop(0)
            if isinstance(failure, Exception):
                raise failure
        return self.fragment_factory(kwargs["batch_payload"])

    def repair_fragment(self, **kwargs):
        self.repair_calls.append(
            {
                "validation_errors": kwargs.get("validation_errors"),
                "affected_chunks": kwargs.get("affected_chunks"),
                "graph_context": kwargs.get("graph_context"),
            }
        )
        if self._failures:
            failure = self._failures.pop(0)
            if isinstance(failure, Exception):
                raise failure
        return GraphPatchFragment(
            nodes=[],
            edges=[],
            coverage=[
                {
                    "chunkIndex": int(chunk["index"]),
                    "decision": "NO_RELEVANT_FACT",
                    "reason": "No ontology-representable fact in this test chunk.",
                }
                for chunk in kwargs.get("affected_chunks", [])
            ],
            warnings=[],
        )


class FakePlanner:
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


def _tool_context() -> FakeToolContext:
    return FakeToolContext({"test-doc.md": DOC_TEXT})


def _stub_loop_dependencies(
    monkeypatch,
    extractor,
    *,
    submit_responses=None,
    finalized=None,
):
    monkeypatch.setattr(
        ingestion_use_case,
        "_get_semantic_placement_planner",
        lambda: FakePlanner(extractor),
    )
    monkeypatch.setattr(
        ingestion_use_case,
        "_get_validation_service",
        lambda: SimpleNamespace(
            compiler=SimpleNamespace(ontology_digest="test-ontology-digest"),
            source_grounding=object(),
        ),
    )
    monkeypatch.setattr(
        ingestion_use_case,
        "_transport_retry_delay_seconds",
        lambda *args, **kwargs: 0.0,
    )
    monkeypatch.setattr(
        ingestion_use_case,
        "_rate_limit_retry_delay_seconds",
        lambda *args, **kwargs: 0.0,
    )

    async def _noop_sleep(*args, **kwargs):
        return None

    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    submit_calls = {"count": 0}

    def _fake_submit(*args, **kwargs):
        responses = submit_responses
        if responses is None:
            return {"success": True, "processedBatches": 1, "stage": "ready_to_finalize"}
        index = min(submit_calls["count"], len(responses) - 1)
        submit_calls["count"] += 1
        return responses[index]

    monkeypatch.setattr(ingestion_use_case, "submit_ingestion_batch", _fake_submit)
    monkeypatch.setattr(
        ingestion_use_case,
        "finalize_ingestion",
        lambda *args, **kwargs: finalized
        or {"success": True, "stage": "ready_to_fill"},
    )


def _run_end_to_end(**kwargs):
    return asyncio.run(
        ingestion_use_case.ingest_document_end_to_end(
            "test-doc.md",
            _tool_context(),
            persist=False,
            **kwargs,
        )
    )


def test_transient_transport_classification():
    assert ingestion_use_case._is_transient_transport_error(
        httpx.RemoteProtocolError("server disconnected")
    )
    assert ingestion_use_case._is_transient_transport_error(
        httpx.ConnectError("connect failed")
    )
    assert ingestion_use_case._is_transient_transport_error(
        httpx.ReadTimeout("read stalled")
    )
    assert ingestion_use_case._is_transient_transport_error(
        httpx.ReadError("read failed")
    )
    assert ingestion_use_case._is_transient_transport_error(
        httpx.WriteError("write failed")
    )
    assert not ingestion_use_case._is_transient_transport_error(
        httpx.LocalProtocolError("local protocol error")
    )
    assert not ingestion_use_case._is_transient_transport_error(
        httpx.UnsupportedProtocol("unsupported protocol")
    )
    assert not ingestion_use_case._is_transient_transport_error(ValueError("boom"))
    assert not ingestion_use_case._is_transient_transport_error(
        InvalidGraphPatchFragmentError("bad json")
    )


def test_transport_backoff_bounded_jitter(monkeypatch):
    monkeypatch.setattr(
        ingestion_use_case, "INGESTION_TRANSPORT_RETRY_BASE_SECONDS", 2.0
    )
    monkeypatch.setattr(
        ingestion_use_case, "INGESTION_TRANSPORT_RETRY_MAX_SECONDS", 60.0
    )
    monkeypatch.setattr(
        ingestion_use_case, "INGESTION_TRANSPORT_RETRY_JITTER_SECONDS", 1.0
    )
    rng = random.Random(7)
    delays = [
        ingestion_use_case._transport_retry_delay_seconds(attempt, rng=rng)
        for attempt in (1, 2, 3)
    ]
    assert delays[0] >= 2.0 and delays[0] <= 3.0
    assert delays[1] >= 4.0 and delays[1] <= 5.0
    assert delays[2] >= 8.0 and delays[2] <= 9.0

    monkeypatch.setattr(
        ingestion_use_case, "INGESTION_TRANSPORT_RETRY_MAX_SECONDS", 5.0
    )
    capped = [
        ingestion_use_case._transport_retry_delay_seconds(attempt, rng=rng)
        for attempt in range(1, 6)
    ]
    assert all(delay <= 5.0 for delay in capped)

    monkeypatch.setattr(
        ingestion_use_case, "INGESTION_TRANSPORT_RETRY_JITTER_SECONDS", 0.0
    )
    assert ingestion_use_case._transport_retry_delay_seconds(1, rng=rng) == 2.0
    assert ingestion_use_case._transport_retry_delay_seconds(2, rng=rng) == 4.0


def test_transport_blip_does_not_consume_semantic_budget(monkeypatch):
    extractor = FakeExtractor(
        failures=[httpx.RemoteProtocolError("server disconnected")]
    )
    _stub_loop_dependencies(monkeypatch, extractor)

    result = _run_end_to_end(
        max_retries_per_batch=1,
        max_transport_retries=2,
    )

    assert result["success"] is True
    assert len(extractor.calls) == 2
    assert extractor.calls[0]["previous_error"] is None
    assert extractor.calls[1]["previous_error"] is None


def test_rate_limit_blip_does_not_consume_semantic_budget(monkeypatch):
    extractor = FakeExtractor(failures=[RateLimitError("quota exceeded")])
    _stub_loop_dependencies(monkeypatch, extractor)

    result = _run_end_to_end(
        max_retries_per_batch=1,
        max_rate_limit_retries=2,
    )

    assert result["success"] is True
    assert len(extractor.calls) == 2


def test_transport_exhaustion_returns_failure_reason(monkeypatch):
    extractor = FakeExtractor(
        failures=[httpx.RemoteProtocolError("server disconnected")] * 3
    )
    _stub_loop_dependencies(monkeypatch, extractor)

    result = _run_end_to_end(
        max_retries_per_batch=1,
        max_transport_retries=2,
    )

    assert result["success"] is False
    assert result["terminal"] is True
    assert result["stage"] == "explicit_extraction_failure"
    assert result["failureReason"] == "TRANSPORT_RETRIES_EXHAUSTED"
    assert result["transportRetries"] == 2
    assert result["attempt"] == 1
    assert result["errorKind"] == "llm_extraction"
    assert result["errors"][0]["code"] == "ORCHESTRATION_FAILED"
    assert len(extractor.calls) == 3


def test_rate_limit_exhaustion_returns_failure_reason(monkeypatch):
    extractor = FakeExtractor(failures=[RateLimitError("quota exceeded")] * 3)
    _stub_loop_dependencies(monkeypatch, extractor)

    result = _run_end_to_end(
        max_retries_per_batch=1,
        max_rate_limit_retries=2,
    )

    assert result["success"] is False
    assert result["failureReason"] == "RATE_LIMIT_RETRIES_EXHAUSTED"
    assert result["rateLimitRetries"] == 2
    assert len(extractor.calls) == 3


def test_schema_repair_consumes_semantic_budget_and_succeeds(monkeypatch):
    extractor = FakeExtractor(
        failures=[InvalidGraphPatchFragmentError("LLM returned invalid JSON")]
    )
    _stub_loop_dependencies(monkeypatch, extractor)

    result = _run_end_to_end(
        max_retries_per_batch=2,
    )

    assert result["success"] is True
    assert len(extractor.calls) == 2
    repair = extractor.calls[1]["previous_error"]
    assert repair is not None
    assert repair["errorKind"] == "invalid_graph_patch_fragment"
    assert "repairInstructions" in repair


def test_schema_repair_exhaustion_returns_failure_reason(monkeypatch):
    extractor = FakeExtractor(
        failures=[
            InvalidGraphPatchFragmentError("LLM returned invalid JSON"),
            InvalidGraphPatchFragmentError("LLM returned invalid JSON again"),
        ]
    )
    _stub_loop_dependencies(monkeypatch, extractor)

    result = _run_end_to_end(
        max_retries_per_batch=2,
    )

    assert result["success"] is False
    assert result["failureReason"] == "SEMANTIC_REPAIR_EXHAUSTED"
    assert result["attempt"] == 2
    assert len(extractor.calls) == 2


def test_validation_rejection_consumes_semantic_budget_and_preserves_previous_error(
    monkeypatch,
):
    extractor = FakeExtractor(
        failures=[None, httpx.RemoteProtocolError("disconnect")]
    )
    rejection = {
        "success": False,
        "stage": "batch_validation",
        "batchIndex": 0,
        "retryRequired": True,
        "errorSummary": {"codes": ["PROPERTY_VALUE_NOT_GROUNDED"]},
        "repairInstructions": "Ground the value in source evidence.",
        "affectedChunkIndexes": [0],
        "errors": [
            {
                "code": "PROPERTY_VALUE_NOT_GROUNDED",
                "message": "Ground the value in source evidence.",
                "location": "nodes.0.properties.0.evidence",
                "nodeTempId": "node-a",
                "propertyName": "ex:p",
            }
        ],
    }
    success = {"success": True, "processedBatches": 1, "stage": "ready_to_finalize"}
    _stub_loop_dependencies(
        monkeypatch,
        extractor,
        submit_responses=[rejection, success],
    )

    result = _run_end_to_end(
        max_retries_per_batch=2,
        max_transport_retries=2,
    )

    assert result["success"] is True
    assert len(extractor.calls) == 3
    assert len(extractor.repair_calls) == 0
    repair = {
        "stage": "batch_validation",
        "batchIndex": 0,
        "errorSummary": {"codes": ["PROPERTY_VALUE_NOT_GROUNDED"]},
        "repairInstructions": "Ground the value in source evidence.",
        "affectedChunkIndexes": [0],
        "rejectedProperties": ["node-a.ex:p"],
        "rejectedEdges": [],
        "rejectedCoverage": [],
        "rejectedNodes": [],
    }
    assert extractor.calls[1]["previous_error"] == repair
    assert extractor.calls[2]["previous_error"] == repair


def test_validation_rejection_exhaustion_returns_failure_reason(monkeypatch):
    extractor = FakeExtractor()
    rejection = {
        "success": False,
        "stage": "batch_validation",
        "batchIndex": 0,
        "retryRequired": True,
        "errorSummary": {"codes": ["COVERAGE_NOT_EVIDENCED"]},
        "repairInstructions": "Resubmit with grounded coverage.",
        "affectedChunkIndexes": [0],
        "errors": [
            {
                "code": "COVERAGE_NOT_EVIDENCED",
                "message": "Chunk 0 is marked MAPPED but no grounded fact references it",
                "location": "coverage.0",
            }
        ],
    }
    _stub_loop_dependencies(
        monkeypatch,
        extractor,
        submit_responses=[rejection],
    )

    result = _run_end_to_end(
        max_retries_per_batch=2,
    )

    assert result["success"] is False
    assert result["failureReason"] == "SEMANTIC_REPAIR_EXHAUSTED"
    assert "could not be safely reduced" in result["message"]
    assert len(extractor.calls) == 2
    assert len(extractor.repair_calls) == 0


def test_atomic_extractor_initializes_default_client(monkeypatch):
    captured = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(genai, "Client", FakeClient)
    GeminiAtomicFactExtractor()

    assert captured["api_key"] == os.getenv("GOOGLE_API_KEY")


def test_extractor_accepts_injected_client():
    fake_client = object()
    extractor = GeminiAtomicFactExtractor(client=fake_client)
    assert extractor.client is fake_client


def test_extractor_config_error_classification_helper():
    assert ingestion_use_case._is_extractor_configuration_error(
        ConfigError("400 INVALID_ARGUMENT: Unknown name 'additional_properties'")
    )
    assert ingestion_use_case._is_extractor_configuration_error(
        ValueError(
            "400 INVALID_ARGUMENT: Unknown name 'additional_properties' at "
            "generation_config.response_schema"
        )
    )
    assert not ingestion_use_case._is_extractor_configuration_error(
        ValueError("plain failure")
    )
    assert not ingestion_use_case._is_extractor_configuration_error(
        httpx.RemoteProtocolError("server disconnected")
    )
    assert not ingestion_use_case._is_extractor_configuration_error(
        RateLimitError("quota exceeded")
    )
    assert (
        ingestion_use_case._orchestration_error_kind(
            ConfigError("400 INVALID_ARGUMENT")
        )
        == "llm_request_config"
    )


def test_config_error_fails_fast_with_classification(monkeypatch):
    extractor = FakeExtractor(
        failures=[
            ConfigError(
                "400 INVALID_ARGUMENT: Unknown name 'additional_properties' at "
                "generation_config.response_schema"
            )
        ]
    )
    _stub_loop_dependencies(monkeypatch, extractor)

    result = _run_end_to_end(
        max_retries_per_batch=3,
        max_transport_retries=3,
        max_rate_limit_retries=3,
    )

    assert result["success"] is False
    assert result["terminal"] is True
    assert result["stage"] == "explicit_extraction_failure"
    assert result["failureReason"] == "EXTRACTOR_CONFIGURATION_FAILED"
    assert result["errorKind"] == "llm_request_config"
    assert "transportRetries" not in result
    assert "rateLimitRetries" not in result
    assert len(extractor.calls) == 1


def test_stage_local_rate_exhaustion_does_not_restart_semantic_pipeline(monkeypatch):
    exhausted = StageLocalModelCallExhausted(
        "local rate retries exhausted",
        cause=RateLimitError("429 RESOURCE_EXHAUSTED"),
    )
    extractor = FakeExtractor(failures=[exhausted])
    _stub_loop_dependencies(monkeypatch, extractor)

    result = _run_end_to_end(
        max_retries_per_batch=3,
        max_rate_limit_retries=3,
    )

    assert result["success"] is False
    assert result["failureReason"] == "RATE_LIMIT_RETRIES_EXHAUSTED"
    assert len(extractor.calls) == 1
