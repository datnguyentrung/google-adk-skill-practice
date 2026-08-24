import pytest

from app.services.ingestion.fill_service import FillService, FillValidationError
from app.services.ingestion.graph_patch_compiler import CompiledGraphPatch
from app.services.ingestion.validate_graph_patch import GraphPatchValidationService


def source_chunks():
    return [
        {
            "index": 0,
            "source": "source.md",
            "section": "Fixture",
            "content": "P-1 Published 01/08/2026 Rule Eligibility",
        }
    ]


def ready_patch() -> dict:
    product_ev = [{"source": "source.md", "chunkIndex": 0, "section": "Fixture", "text": "P-1 Published 01/08/2026"}]
    rule_ev = [{"source": "source.md", "chunkIndex": 0, "section": "Fixture", "text": "Rule"}]
    edge_ev = [{"source": "source.md", "chunkIndex": 0, "section": "Fixture", "text": "Eligibility"}]
    return {
        "nodes": [
            {
                "tempId": "product-1",
                "className": "pskg:BankingProduct",
                "properties": [
                    {"propertyName": "pskg:productCode", "value": "P-1", "evidence": product_ev},
                    {"propertyName": "pskg:bankingProductStatus", "value": "Published", "evidence": product_ev},
                    {"propertyName": "pskg:bankingProductEffectiveFrom", "value": "2026-08-01", "evidence": product_ev},
                ],
                "evidence": product_ev,
                "confidence": 1.0,
            },
            {
                "tempId": "rule-1",
                "className": "pskg:BusinessRule",
                "properties": [
                    {"propertyName": "pskg:businessRuleStatus", "value": "Published", "evidence": product_ev}
                ],
                "evidence": rule_ev,
                "confidence": 1.0,
            },
        ],
        "edges": [
            {
                "edgeName": "pskg:hasEligibilityRule",
                "sourceTempId": "product-1",
                "targetTempId": "rule-1",
                "evidence": edge_ev,
                "confidence": 1.0,
            }
        ],
        "coverage": [{"chunkIndex": 0, "decision": "MAPPED", "reason": "Fixture facts"}],
        "warnings": [],
    }


class FakeTransaction:
    def __init__(self):
        self.committed = False
        self.rolled_back = False


class FakeSession:
    def __init__(self):
        self.tx = FakeTransaction()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute_write(self, callback):
        try:
            result = callback(self.tx)
        except Exception:
            self.tx.rolled_back = True
            raise
        self.tx.committed = True
        return result


class FakeDriver:
    def __init__(self):
        self.last_session = None

    def session(self, database):
        self.last_session = FakeSession()
        return self.last_session


class FakeClient:
    database_name = "test"

    def __init__(self):
        self.driver_calls = 0
        self.driver = FakeDriver()

    def get_driver(self):
        self.driver_calls += 1
        return self.driver

    def close_driver(self):
        pass


class RecordingWriter:
    def __init__(self, error=None):
        self.error = error
        self.patch = None

    def write_graph_patch(self, tx, patch):
        self.patch = patch
        if self.error is not None:
            raise self.error
        return {"product-1": "node-1", "rule-1": "node-2"}


def test_invalid_patch_does_not_acquire_driver():
    client = FakeClient()
    service = FillService(
        client=client,
        validation_service=GraphPatchValidationService(),
        writer=RecordingWriter(),
    )

    with pytest.raises(FillValidationError):
        service.fill({"nodes": [], "edges": [], "extra": True}, None)

    assert client.driver_calls == 0


def test_persistence_not_ready_patch_does_not_acquire_driver():
    client = FakeClient()
    patch = ready_patch()
    patch["nodes"][0]["properties"] = [
        entry
        for entry in patch["nodes"][0]["properties"]
        if entry["propertyName"] != "pskg:bankingProductStatus"
    ]
    service = FillService(
        client=client,
        validation_service=GraphPatchValidationService(),
        writer=RecordingWriter(),
    )

    with pytest.raises(FillValidationError):
        service.fill(patch, None, source_chunks())

    assert client.driver_calls == 0


def test_valid_patch_writes_compiled_patch_atomically():
    client = FakeClient()
    writer = RecordingWriter()
    service = FillService(
        client=client,
        validation_service=GraphPatchValidationService(),
        writer=writer,
    )

    result = service.fill(ready_patch(), "artifact", source_chunks())

    assert result["status"] == "success"
    assert isinstance(writer.patch, CompiledGraphPatch)
    assert client.driver.last_session.tx.committed is True
    assert client.driver.last_session.tx.rolled_back is False


def test_failure_during_graph_write_rolls_back_transaction():
    client = FakeClient()
    service = FillService(
        client=client,
        validation_service=GraphPatchValidationService(),
        writer=RecordingWriter(RuntimeError("edge write failed")),
    )

    with pytest.raises(RuntimeError, match="edge write failed"):
        service.fill(ready_patch(), "artifact", source_chunks())

    assert client.driver.last_session.tx.committed is False
    assert client.driver.last_session.tx.rolled_back is True
