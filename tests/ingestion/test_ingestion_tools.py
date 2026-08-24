import asyncio
import hashlib
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

from app.tools import ingestion_tools


DOCUMENT_PATH = next(Path("docs").glob("*FLEXI REWARDS.md"))


class FakeToolContext:
    def __init__(self, artifact=None):
        self.artifact = artifact
        self.state: dict = {}

    async def load_artifact(self, filename: str):
        return self.artifact


def ready_patch() -> dict:
    return {
        "nodes": [
            {
                "tempId": "product-1",
                "className": "pskg:BankingProduct",
                "properties": [
                    {"propertyName": "pskg:productCode", "value": "CC-FLEXI-001"},
                    {
                        "propertyName": "pskg:bankingProductStatus",
                        "value": "Published",
                    },
                    {
                        "propertyName": "pskg:bankingProductEffectiveFrom",
                        "value": "2026-08-01",
                    },
                ],
                "evidence": [{"source": "flexi.md", "text": "CC-FLEXI-001"}],
                "confidence": 1.0,
            },
            {
                "tempId": "rule-1",
                "className": "pskg:BusinessRule",
                "properties": [
                    {
                        "propertyName": "pskg:businessRuleStatus",
                        "value": "Published",
                    }
                ],
                "evidence": [{"source": "flexi.md", "text": "Age 20"}],
                "confidence": 0.9,
            },
        ],
        "edges": [
            {
                "edgeName": "pskg:hasEligibilityRule",
                "sourceTempId": "product-1",
                "targetTempId": "rule-1",
                "evidence": [{"source": "flexi.md", "text": "Eligibility"}],
                "confidence": 0.9,
            }
        ],
        "warnings": [],
    }


def test_prepare_hashes_raw_artifact_bytes_and_clears_old_gate():
    data = DOCUMENT_PATH.read_bytes()
    artifact = SimpleNamespace(
        inline_data=SimpleNamespace(data=data, mime_type="text/markdown"),
        text=None,
    )
    context = FakeToolContext(artifact)
    context.state[ingestion_tools.VALIDATED_FINGERPRINT_STATE_KEY] = "old"

    result = asyncio.run(
        ingestion_tools.prepare_extraction_context(DOCUMENT_PATH.name, context)
    )

    assert result["success"] is True
    assert context.state[ingestion_tools.ARTIFACT_DIGEST_STATE_KEY] == hashlib.sha256(
        data
    ).hexdigest()
    assert context.state[ingestion_tools.ARTIFACT_NAME_STATE_KEY] == DOCUMENT_PATH.name
    assert ingestion_tools.VALIDATED_FINGERPRINT_STATE_KEY not in context.state


def test_same_artifact_name_with_different_bytes_changes_digest():
    first = FakeToolContext(
        SimpleNamespace(
            inline_data=SimpleNamespace(data=b"first", mime_type="text/plain"),
            text=None,
        )
    )
    second = FakeToolContext(
        SimpleNamespace(
            inline_data=SimpleNamespace(data=b"second", mime_type="text/plain"),
            text=None,
        )
    )
    asyncio.run(ingestion_tools.prepare_extraction_context("same.md", first))
    asyncio.run(ingestion_tools.prepare_extraction_context("same.md", second))

    assert first.state[ingestion_tools.ARTIFACT_DIGEST_STATE_KEY] != second.state[
        ingestion_tools.ARTIFACT_DIGEST_STATE_KEY
    ]


def test_validate_returns_public_result_only_and_sets_gate_when_ready():
    context = FakeToolContext()
    result = ingestion_tools.validate_graph_patch(ready_patch(), context)

    assert result["validForExtraction"] is True
    assert result["validForPersistence"] is True
    assert "compiled_patch" not in result
    assert "fingerprint" not in result
    assert ingestion_tools.VALIDATED_FINGERPRINT_STATE_KEY in context.state


def test_not_ready_validation_clears_gate():
    context = FakeToolContext()
    context.state[ingestion_tools.VALIDATED_FINGERPRINT_STATE_KEY] = "old"
    patch = ready_patch()
    patch["nodes"][0]["properties"] = [
        entry
        for entry in patch["nodes"][0]["properties"]
        if entry["propertyName"] != "pskg:bankingProductStatus"
    ]

    result = ingestion_tools.validate_graph_patch(patch, context)

    assert result["validForExtraction"] is True
    assert result["validForPersistence"] is False
    assert ingestion_tools.VALIDATED_FINGERPRINT_STATE_KEY not in context.state


def test_extraction_invalid_validation_clears_gate():
    context = FakeToolContext()
    context.state[ingestion_tools.VALIDATED_FINGERPRINT_STATE_KEY] = "old"
    patch = ready_patch()
    patch["nodes"][0]["properties"].append(
        {"propertyName": "pskg:notReal", "value": "invented"}
    )

    result = ingestion_tools.validate_graph_patch(patch, context)

    assert result["validForExtraction"] is False
    assert ingestion_tools.VALIDATED_FINGERPRINT_STATE_KEY not in context.state


def test_fill_without_validate_does_not_create_neo4j_service(monkeypatch):
    called = False

    def factory(**kwargs):
        nonlocal called
        called = True
        raise AssertionError("factory must not be called")

    monkeypatch.setattr(ingestion_tools, "create_fill_service", factory)
    result = ingestion_tools.fill_graph_patch(ready_patch(), FakeToolContext())

    assert result["stage"] == "validation_precondition"
    assert result["errors"][0]["code"] == "VALIDATION_PRECONDITION"
    assert called is False


def test_patch_or_artifact_change_invalidates_gate(monkeypatch):
    called = False

    def factory(**kwargs):
        nonlocal called
        called = True
        raise AssertionError("factory must not be called")

    monkeypatch.setattr(ingestion_tools, "create_fill_service", factory)
    context = FakeToolContext()
    context.state[ingestion_tools.ARTIFACT_DIGEST_STATE_KEY] = "artifact-a"
    ingestion_tools.validate_graph_patch(ready_patch(), context)

    changed_patch = deepcopy(ready_patch())
    changed_patch["nodes"][0]["properties"][0]["value"] = "OTHER"
    patch_result = ingestion_tools.fill_graph_patch(changed_patch, context)
    assert patch_result["stage"] == "validation_precondition"

    context.state[ingestion_tools.ARTIFACT_DIGEST_STATE_KEY] = "artifact-b"
    artifact_result = ingestion_tools.fill_graph_patch(ready_patch(), context)
    assert artifact_result["stage"] == "validation_precondition"
    assert called is False


def test_invalid_modified_patch_returns_precondition_before_validation(monkeypatch):
    context = FakeToolContext()
    ingestion_tools.validate_graph_patch(ready_patch(), context)
    changed_patch = deepcopy(ready_patch())
    changed_patch["nodes"][0]["properties"].append(
        {"propertyName": "pskg:notReal", "value": None}
    )
    monkeypatch.setattr(
        ingestion_tools,
        "create_fill_service",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("must not create")),
    )

    result = ingestion_tools.fill_graph_patch(changed_patch, context)

    assert result["stage"] == "validation_precondition"
    assert result["errors"][0]["code"] == "VALIDATION_PRECONDITION"


def test_gate_does_not_exist_in_a_new_invocation_context(monkeypatch):
    first_invocation = FakeToolContext()
    ingestion_tools.validate_graph_patch(ready_patch(), first_invocation)
    second_invocation = FakeToolContext()

    monkeypatch.setattr(
        ingestion_tools,
        "create_fill_service",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("must not create")),
    )
    result = ingestion_tools.fill_graph_patch(ready_patch(), second_invocation)

    assert result["stage"] == "validation_precondition"


class FakeFillService:
    def __init__(self):
        self.closed = False

    def fill(self, patch, artifact_content_digest):
        assert artifact_content_digest is None
        return {
            "status": "success",
            "nodes": 2,
            "edges": 1,
            "nodeIds": {"product-1": "node-1", "rule-1": "node-2"},
        }

    def close(self):
        self.closed = True


def test_validated_fill_closes_service(monkeypatch):
    context = FakeToolContext()
    ingestion_tools.validate_graph_patch(ready_patch(), context)
    service = FakeFillService()
    monkeypatch.setattr(
        ingestion_tools,
        "create_fill_service",
        lambda **kwargs: service,
    )

    result = ingestion_tools.fill_graph_patch(ready_patch(), context)

    assert result["success"] is True
    assert result["stage"] == "completed"
    assert service.closed is True


def test_validate_and_fill_function_schemas_hide_context_and_use_property_array():
    from google.adk.tools import FunctionTool

    for function in (
        ingestion_tools.validate_graph_patch,
        ingestion_tools.fill_graph_patch,
    ):
        schema = FunctionTool(function)._get_declaration().parameters_json_schema
        assert set(schema["properties"]) == {"graph_patch"}
        assert schema["properties"]["graph_patch"]["$ref"] == (
            "#/$defs/GraphPatchDraft"
        )
        properties_schema = schema["$defs"]["ExtractedNode"]["properties"][
            "properties"
        ]
        assert properties_schema["type"] == "array"
        assert properties_schema["items"]["$ref"] == "#/$defs/ExtractedProperty"
