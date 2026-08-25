import pytest

from app.core.schemas.ingestion.persistence import GraphWriteResult, PersistedNode
from app.services.ingestion.fill_service import FillService, FillValidationError
from app.services.ingestion.graph_patch_compiler import CompiledGraphPatch
from app.services.ingestion.loader import OntologyLoader
from app.services.ingestion.neo4j_mapper import Neo4jMapper
from app.services.ingestion.registry import OntologyRegistry
from app.services.ingestion.validate_graph_patch import GraphPatchValidationService


def source_chunks():
    return [
        {
            "index": 0,
            "source": "source.md",
            "section": "Fixture",
            "content": "P-1 Published 01/08/2026 has eligibility rule",
        }
    ]


def ready_patch() -> dict:
    product_ev = [{"source": "source.md", "chunkIndex": 0, "section": "Fixture", "text": "P-1 Published 01/08/2026"}]
    rule_ev = [{"source": "source.md", "chunkIndex": 0, "section": "Fixture", "text": "Rule"}]
    edge_ev = [{"source": "source.md", "chunkIndex": 0, "section": "Fixture", "text": "P-1 Published 01/08/2026 has eligibility rule"}]
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

    def execute_read(self, callback):
        return callback(self.tx)


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
    def __init__(self, error=None, *, mismatch=False, read_error=None):
        self.error = error
        self.mismatch = mismatch
        self.read_error = read_error
        self.patch = None
        self.mapper = Neo4jMapper(
            OntologyRegistry(
                OntologyLoader.load(
                    "app/data/ontology/product_sales_knowledge_graph_base_v3_1.ontology.json"
                )
            )
        )

    def write_graph_patch(self, tx, patch):
        self.patch = patch
        if self.error is not None:
            raise self.error
        return GraphWriteResult(
            nodeIds={"product-1": "node-1", "rule-1": "node-2"},
            relationshipIds={
                "0:pskg:hasEligibilityRule:product-1->rule-1": "rel-1"
            },
        )

    def read_graph_patch(self, tx, write_result):
        if self.read_error is not None:
            raise self.read_error
        product_properties = {
            "productCode": "P-1",
            "bankingProductStatus": "Published",
            "bankingProductEffectiveFrom": "2026-08-01",
        }
        if self.mismatch:
            product_properties["productCode"] = "WRONG"
        return {
            "nodes": [
                {
                    "nodeId": "node-1",
                    "labels": ["BankingProduct"],
                    "properties": product_properties,
                },
                {
                    "nodeId": "node-2",
                    "labels": ["BusinessRule"],
                    "properties": {
                        "businessRuleStatus": "Published",
                        "ruleType": "ELIGIBILITY",
                    },
                },
            ],
            "relationships": [
                {
                    "relationshipId": "rel-1",
                    "type": "HAS_ELIGIBILITY_RULE",
                    "sourceNodeId": "node-1",
                    "targetNodeId": "node-2",
                    "properties": {},
                }
            ],
        }


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
    assert result["commitStatus"] == "committed"
    assert result["receipt"]["verified"] is True
    assert result["receipt"]["relationshipIds"] == {
        "0:pskg:hasEligibilityRule:product-1->rule-1": "rel-1"
    }


def test_readback_mismatch_is_reported_after_commit_without_claiming_rollback():
    client = FakeClient()
    service = FillService(
        client=client,
        validation_service=GraphPatchValidationService(),
        writer=RecordingWriter(mismatch=True),
    )

    result = service.fill(ready_patch(), "artifact", source_chunks())

    assert result["status"] == "readback_mismatch"
    assert result["commitStatus"] == "committed"
    assert result["receipt"]["verified"] is False
    assert any("productCode" in item for item in result["receipt"]["mismatches"])
    assert client.driver.last_session.tx.committed is True
    assert client.driver.last_session.tx.rolled_back is False


def test_readback_exception_is_reported_as_committed_mismatch():
    client = FakeClient()
    service = FillService(
        client=client,
        validation_service=GraphPatchValidationService(),
        writer=RecordingWriter(read_error=RuntimeError("read unavailable")),
    )

    result = service.fill(ready_patch(), "artifact", source_chunks())

    assert result["status"] == "readback_mismatch"
    assert result["commitStatus"] == "committed"
    assert result["receipt"]["verified"] is False
    assert result["receipt"]["mismatches"][0] == "readback failed: read unavailable"
    assert client.driver.last_session.tx.committed is True


@pytest.mark.parametrize(
    ("mutation", "expected_fragment"),
    [
        (lambda data: data["nodes"][0]["labels"].append("StaleLabel"), "labels expected"),
        (
            lambda data: data["nodes"][0]["properties"].update({"stale": True}),
            "property keys expected",
        ),
        (
            lambda data: data["relationships"][0]["properties"].update({"stale": True}),
            "properties expected",
        ),
    ],
)
def test_readback_rejects_stale_labels_and_properties(mutation, expected_fragment):
    class MutatingWriter(RecordingWriter):
        def read_graph_patch(self, tx, write_result):
            data = super().read_graph_patch(tx, write_result)
            mutation(data)
            return data

    result = FillService(
        client=FakeClient(),
        validation_service=GraphPatchValidationService(),
        writer=MutatingWriter(),
    ).fill(ready_patch(), "artifact", source_chunks())

    assert result["status"] == "readback_mismatch"
    assert any(
        expected_fragment in mismatch
        for mismatch in result["receipt"]["mismatches"]
    )


def test_readback_checks_internal_ingestion_metadata_values():
    class InternalMetadataWriter(RecordingWriter):
        def write_graph_patch(self, tx, patch):
            result = super().write_graph_patch(tx, patch)
            result.expected_nodes["product-1"] = PersistedNode(
                nodeId="node-1",
                labels=["BankingProduct"],
                properties={
                    "productCode": "P-1",
                    "bankingProductStatus": "Published",
                    "bankingProductEffectiveFrom": "2026-08-01",
                    "_ingestionKey": "expected",
                },
            )
            return result

        def read_graph_patch(self, tx, write_result):
            data = super().read_graph_patch(tx, write_result)
            data["nodes"][0]["properties"]["_ingestionKey"] = "stale"
            return data

    result = FillService(
        client=FakeClient(),
        validation_service=GraphPatchValidationService(),
        writer=InternalMetadataWriter(),
    ).fill(ready_patch(), "artifact", source_chunks())

    assert result["status"] == "readback_mismatch"
    assert any(
        "_ingestionKey" in mismatch
        for mismatch in result["receipt"]["mismatches"]
    )


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
