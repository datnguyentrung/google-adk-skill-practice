import asyncio
from pathlib import Path
from types import SimpleNamespace

from app.services.ingestion.fill_service import FillValidationError
from app.tools import ingestion_tools

DOCUMENT_PATH = Path(
    "docs/HƯỚNG DẪN NGHIỆP VỤ SẢN PHẨM THẺ TÍN DỤNG FLEXI REWARDS.md"
)


class FakeToolContext:
    def __init__(self, artifact):
        self.artifact = artifact
        self.state = {}

    async def load_artifact(self, filename: str):
        return self.artifact


def test_prepare_extraction_context_tool_reads_uploaded_artifact():
    artifact = SimpleNamespace(
        inline_data=SimpleNamespace(
            data=DOCUMENT_PATH.read_bytes(),
            mime_type="text/markdown",
        ),
        text=None,
    )
    tool_context = FakeToolContext(artifact)

    result = asyncio.run(
        ingestion_tools.prepare_extraction_context(
            DOCUMENT_PATH.name,
            tool_context,
        )
    )

    assert result["success"] is True
    assert result["document_name"] == DOCUMENT_PATH.name
    assert result["chunks"]
    assert "pskg:BankingProduct" in result["ontology_context"]
    assert any("CC-FLEXI-001" in chunk["content"] for chunk in result["chunks"])
    assert tool_context.state["temp:ingestion_source"] == DOCUMENT_PATH.name


def test_prepare_extraction_context_logs_progress(caplog):
    artifact = SimpleNamespace(
        inline_data=SimpleNamespace(
            data=DOCUMENT_PATH.read_bytes(),
            mime_type="text/markdown",
        ),
        text=None,
    )
    tool_context = FakeToolContext(artifact)

    with caplog.at_level("INFO", logger="app.tools.ingestion_tools"):
        result = asyncio.run(
            ingestion_tools.prepare_extraction_context(
                DOCUMENT_PATH.name,
                tool_context,
            )
        )

    assert result["success"] is True
    messages = [record.getMessage() for record in caplog.records]
    assert any("Ingestion artifact loaded" in message for message in messages)
    assert any(
        "Ingestion extraction context prepared" in message
        for message in messages
    )


def test_validate_graph_patch_tool_returns_structured_errors():
    result = ingestion_tools.validate_graph_patch(
        {
            "nodes": [
                {
                    "tempId": "bad-1",
                    "className": "pskg:NotAClass",
                    "properties": {},
                    "evidence": [],
                    "confidence": 1.0,
                }
            ],
            "edges": [],
            "warnings": [],
        }
    )

    assert result["valid"] is False
    assert any("Unknown ontology class" in error for error in result["errors"])


def test_validate_graph_patch_logs_invalid_result(caplog):
    graph_patch = {
        "nodes": [
            {
                "tempId": "bad-1",
                "className": "pskg:NotAClass",
                "properties": {},
                "evidence": [],
                "confidence": 1.0,
            }
        ],
        "edges": [],
        "warnings": [],
    }

    with caplog.at_level("INFO", logger="app.tools.ingestion_tools"):
        result = ingestion_tools.validate_graph_patch(graph_patch)

    assert result["valid"] is False
    messages = [record.getMessage() for record in caplog.records]
    assert any("Ingestion validate_graph_patch started" in message for message in messages)
    assert any(
        "Ingestion validate_graph_patch completed valid=False"
        in message
        for message in messages
    )


def test_fill_graph_patch_rejects_schema_before_factory(monkeypatch):
    called = False

    def fail_if_called():
        nonlocal called
        called = True
        raise AssertionError("factory must not be called")

    monkeypatch.setattr(ingestion_tools, "create_fill_service", fail_if_called)

    result = ingestion_tools.fill_graph_patch(
        {
            "nodes": [
                {
                    "tempId": "product-1",
                    "className": "pskg:BankingProduct",
                    "properties": {},
                    "evidence": [],
                    "confidence": 2.0,
                }
            ],
            "edges": [],
            "warnings": [],
        }
    )

    assert result["success"] is False
    assert result["stage"] == "schema_validation"
    assert called is False


def test_fill_graph_patch_logs_schema_failure_before_factory(monkeypatch, caplog):
    called = False

    def fail_if_called():
        nonlocal called
        called = True
        raise AssertionError("factory must not be called")

    monkeypatch.setattr(ingestion_tools, "create_fill_service", fail_if_called)

    with caplog.at_level("INFO", logger="app.tools.ingestion_tools"):
        result = ingestion_tools.fill_graph_patch(
            {
                "nodes": [
                    {
                        "tempId": "product-1",
                        "className": "pskg:BankingProduct",
                        "properties": {},
                        "evidence": [],
                        "confidence": 2.0,
                    }
                ],
                "edges": [],
                "warnings": [],
            }
        )

    assert result["success"] is False
    assert result["stage"] == "schema_validation"
    assert called is False
    messages = [record.getMessage() for record in caplog.records]
    assert any("Ingestion fill_graph_patch started" in message for message in messages)
    assert any(
        "Ingestion fill_graph_patch schema validation failed"
        in message
        for message in messages
    )


class FakeFillService:
    def __init__(self, error: Exception | None = None):
        self.error = error
        self.closed = False

    def fill(self, patch):
        if self.error is not None:
            raise self.error
        return {
            "status": "success",
            "nodes": len(patch.nodes),
            "edges": len(patch.edges),
            "nodeIds": {"product-1": "node-1"},
        }

    def close(self):
        self.closed = True


def _minimal_schema_valid_patch() -> dict:
    return {
        "nodes": [
            {
                "tempId": "product-1",
                "className": "pskg:BankingProduct",
                "properties": {},
                "evidence": [],
                "confidence": 1.0,
            }
        ],
        "edges": [],
        "warnings": [],
    }


def test_fill_graph_patch_success_closes_service(monkeypatch):
    service = FakeFillService()
    monkeypatch.setattr(
        ingestion_tools,
        "create_fill_service",
        lambda: service,
    )

    result = ingestion_tools.fill_graph_patch(_minimal_schema_valid_patch())

    assert result["success"] is True
    assert result["stage"] == "completed"
    assert result["nodeIds"] == {"product-1": "node-1"}
    assert service.closed is True


def test_fill_graph_patch_maps_validation_error_and_closes(monkeypatch):
    service = FakeFillService(
        FillValidationError("Node product-1: missing required property")
    )
    monkeypatch.setattr(
        ingestion_tools,
        "create_fill_service",
        lambda: service,
    )

    result = ingestion_tools.fill_graph_patch(_minimal_schema_valid_patch())

    assert result["success"] is False
    assert result["stage"] == "ontology_validation"
    assert result["errors"] == [
        "Node product-1: missing required property"
    ]
    assert service.closed is True


def test_validate_tool_exposes_graph_patch_schema_to_model():
    from google.adk.tools import FunctionTool

    declaration = FunctionTool(
        ingestion_tools.validate_graph_patch
    )._get_declaration()
    schema = declaration.parameters_json_schema

    assert schema["properties"]["graph_patch"]["$ref"] == "#/$defs/GraphPatch"
    assert schema["$defs"]["ExtractedNode"]["properties"]["evidence"]["items"][
        "$ref"
    ] == "#/$defs/Evidence"
    assert schema["$defs"]["ExtractedNode"]["properties"]["properties"][
        "type"
    ] == "object"


def test_prepare_tool_exposes_only_artifact_name_to_model():
    from google.adk.tools import FunctionTool

    declaration = FunctionTool(
        ingestion_tools.prepare_extraction_context
    )._get_declaration()
    schema = declaration.parameters_json_schema

    assert set(schema["properties"]) == {"artifact_name"}
    assert schema["required"] == ["artifact_name"]
