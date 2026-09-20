import asyncio
import sys
from types import SimpleNamespace

import pytest

from app.core.schemas.ingestion.document import DocumentChunk
from app.core.schemas.ingestion.graph_patch import GraphPatchFragment
from app.core.schemas.ingestion.identity import NodeIdentity
from app.core.schemas.ingestion.persistence import (
    GraphWriteResult,
    PersistedGraphReadback,
    PersistedGraphReceipt,
)
from app.core.schemas.ingestion.source import SourceLifecycle
from app.core.schemas.ingestion.workspace import IngestionProvenance
from app.services.ingestion.document import DocumentReader
from app.services.ingestion.identity.semantic_resolution import SemanticEntityResolver
from app.services.ingestion.incremental import DocumentNotFoundError
from app.services.ingestion.incremental.cleanup import cleanup_stale_assertions
from app.services.ingestion.incremental.identity import build_batch_cache_key
import app.tools.ingestion_tools as ingestion_tools
from app.services.ingestion.orchestration import state as ingestion_state
from app.services.ingestion.orchestration.context import _batch_payload
from app.services.ingestion.persistence.service import GraphPersistence
from app.services.ingestion.persistence.writer import Neo4jGraphStore
from app.services.ingestion.workspace import IngestionWorkspaceService, staged_ingestion

SOURCE = "AN TAM ONLINE TERM DEPOSIT PRODUCT GUIDE.md"


class FakeToolContext:
    def __init__(self):
        self.state = {}


def _provenance() -> IngestionProvenance:
    return IngestionProvenance(
        artifactDigest="a" * 64,
        ontologyDigest="b" * 64,
        skillDigest="c" * 64,
        documentId="doc_test",
        configSignature="cfg_test",
        ingestionSignature="ing_test",
        sourceVersionId="srcv_test",
        modelId="model-test",
        chunkerVersion="chunker-test",
        mapperVersion="mapper-test",
        compilerVersion="compiler-test",
    )


def _workspace():
    chunk = DocumentChunk(
        index=0,
        source=SOURCE,
        section="A",
        content="No graph fact.",
        documentId="doc_test",
        chunkId="chk_test",
        contentHash="hash_test",
        structuralPath="A#0",
    )
    workspace = IngestionWorkspaceService().begin(
        artifact_name=SOURCE,
        provenance=_provenance(),
        chunks=[chunk],
    )
    return workspace


def _lifecycle() -> SourceLifecycle:
    return SourceLifecycle(
        documentId="doc_test",
        documentName=SOURCE,
        contentHash="a" * 64,
        configSignature="cfg_test",
        ingestionSignature="ing_test",
        versionId="srcv_test",
        ontologyDigest="b" * 64,
        skillDigest="c" * 64,
        modelId="model-test",
        chunkerVersion="chunker-test",
        mapperVersion="mapper-test",
        compilerVersion="compiler-test",
    )


def _empty_fragment() -> GraphPatchFragment:
    return GraphPatchFragment(
        nodes=[],
        edges=[],
        coverage=[
            {
                "chunkIndex": 0,
                "decision": "NO_RELEVANT_FACT",
                "reason": "No ontology fact in the chunk",
            }
        ],
        warnings=[],
    )


def test_document_and_chunk_identity_is_stable_for_unchanged_sections():
    reader = DocumentReader()
    first = reader.read_bytes(
        filename=SOURCE,
        data=b"# A\nalpha\n\n# B\nbeta\n",
    )
    second = reader.read_bytes(
        filename=SOURCE,
        data=b"# A\nalpha\n\n# B\nbeta changed\n",
    )

    assert first[0].document_id == second[0].document_id
    assert first[0].chunk_id == second[0].chunk_id
    assert first[0].content_hash == second[0].content_hash
    assert first[1].chunk_id != second[1].chunk_id
    assert first[1].content_hash != second[1].content_hash


def test_batch_cache_key_depends_on_graph_context():
    workspace = _workspace()
    chunks = workspace.chunks
    first_key, first_context = build_batch_cache_key(
        lifecycle=_lifecycle(), chunks=chunks, graph_context="graph A"
    )
    same_key, same_context = build_batch_cache_key(
        lifecycle=_lifecycle(), chunks=chunks, graph_context="graph A"
    )
    changed_key, changed_context = build_batch_cache_key(
        lifecycle=_lifecycle(), chunks=chunks, graph_context="graph B"
    )

    assert (first_key, first_context) == (same_key, same_context)
    assert first_key != changed_key
    assert first_context != changed_context


class _FakeValidation:
    def __init__(self, patch):
        self.patch = patch

    def assess(self, *args, **kwargs):
        return SimpleNamespace(
            compiled_patch=self.patch,
            result=SimpleNamespace(
                valid_for_extraction=True,
                valid_for_persistence=True,
                readiness_issues=[],
            ),
        )


class _FakeWriter:
    mapper = object()

    def write_graph_patch(self, tx, patch):
        return GraphWriteResult(nodeIds={"n": "node-1"}, relationshipIds={})

    def read_graph_patch(self, tx, write_result):
        return PersistedGraphReadback(nodes=[], relationships=[])


class _FakeSession:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute_write(self, callback):
        return callback(SimpleNamespace())


class _FakeDriver:
    def session(self, **kwargs):
        return _FakeSession()


class _FakeClient:
    database_name = "neo4j"

    def get_driver(self):
        return _FakeDriver()

    def close_driver(self):
        pass


class _FakeSourceStore:
    def __init__(self):
        self.pending = 0
        self.staged = 0
        self.committed = 0
        self.failed: list[str] = []

    def begin_pending(self, lifecycle):
        self.pending += 1

    def stage_written(self, tx, **kwargs):
        self.staged += 1

    def commit_verified(self, tx, **kwargs):
        self.committed += 1

    def mark_failed(self, lifecycle, reason):
        self.failed.append(reason)


def _receipt(verified: bool) -> PersistedGraphReceipt:
    return PersistedGraphReceipt(
        verified=verified,
        expectedNodeCount=1,
        expectedRelationshipCount=0,
        nodeIds={"n": "node-1"},
        relationshipIds={},
        nodes=[],
        relationships=[],
        labelDistribution={},
        relationshipTypeDistribution={},
        mismatches=[] if verified else ["forced mismatch"],
    )


def _persistence(store: _FakeSourceStore) -> GraphPersistence:
    patch = SimpleNamespace(nodes=[SimpleNamespace(temp_id="n")], edges=[])
    return GraphPersistence(
        client=_FakeClient(),
        validation=_FakeValidation(patch),
        writer=_FakeWriter(),
        source_store=store,
    )


def test_verified_readback_commits_source_cutover(monkeypatch):
    store = _FakeSourceStore()
    monkeypatch.setattr(
        "app.services.ingestion.persistence.service.verify_persisted_graph",
        lambda *args, **kwargs: _receipt(True),
    )

    result = _persistence(store).fill({}, "digest", [], source_lifecycle=_lifecycle())

    assert result["commitStatus"] == "committed"
    assert result["sourceVersionStatus"] == "COMMITTED"
    assert result["receipt"]["commitStatus"] == "committed"
    assert store.pending == 1
    assert store.staged == 1
    assert store.committed == 1
    assert store.failed == []


def test_readback_mismatch_rolls_back_cutover(monkeypatch):
    store = _FakeSourceStore()
    monkeypatch.setattr(
        "app.services.ingestion.persistence.service.verify_persisted_graph",
        lambda *args, **kwargs: _receipt(False),
    )

    result = _persistence(store).fill({}, "digest", [], source_lifecycle=_lifecycle())

    assert result["commitStatus"] == "rolled_back"
    assert result["sourceVersionStatus"] == "FAILED"
    assert result["receipt"]["commitStatus"] == "rolled_back"
    assert store.pending == 1
    assert store.staged == 1
    assert store.committed == 0
    assert store.failed == ["forced mismatch"]


def test_true_chunk_cache_mode_partitions_one_chunk_per_batch(monkeypatch):
    monkeypatch.setattr(staged_ingestion, "TRUE_CHUNK_CACHE_MODE", True)
    chunks = DocumentReader().read_bytes(
        filename=SOURCE,
        data=b"# A\na\n# B\nb\n# C\nc\n",
    )
    workspace = IngestionWorkspaceService().begin(
        artifact_name=SOURCE,
        provenance=_provenance(),
        chunks=chunks,
    )

    assert len(workspace.batches) == len(chunks)
    assert all(len(batch.chunk_indexes) == 1 for batch in workspace.batches)


class _VectorProvider:
    def embed(self, text: str) -> list[float]:
        return [1.0, 0.0] if "same" in text else [0.0, 1.0]


class _AcceptVerifier:
    def __init__(self):
        self.calls = 0

    def verify(self, **kwargs) -> bool:
        self.calls += 1
        return True


class _Result:
    def __init__(self, records=None, single=None):
        self.records = records or []
        self._single = single

    def __iter__(self):
        return iter(self.records)

    def single(self):
        return self._single

    def consume(self):
        return None


class _SemanticTx:
    def run(self, query, **kwargs):
        if "RETURN elementId(n) AS node_id, properties(n) AS properties" in query:
            return _Result(
                records=[
                    {"node_id": "node-existing", "properties": {"name": "same entity"}}
                ]
            )
        raise AssertionError(query)


class _SimpleMapper:
    def class_to_label(self, class_name):
        return "BusinessRule"

    def properties_to_neo4j(self, properties):
        return {key.split(":")[-1]: value for key, value in properties.items()}

    def property_to_key(self, property_name):
        return property_name.split(":")[-1]


def test_semantic_resolution_requires_verifier_by_default():
    verifier = _AcceptVerifier()
    resolver = SemanticEntityResolver(
        embedding_provider=_VectorProvider(),
        verifier=verifier,
        soft_threshold=0.8,
        hard_threshold=0.95,
        allow_hard_merge=False,
    )

    candidate = resolver.resolve(
        _SemanticTx(),
        class_name="pskg:BusinessRule",
        properties={"description": "same entity"},
        source_scope=None,
        mapper=_SimpleMapper(),
    )

    assert candidate is not None
    assert candidate.node_id == "node-existing"
    assert verifier.calls == 1


class _NaturalKeyResolver:
    def resolve(self, **kwargs):
        return NodeIdentity(
            class_name=kwargs["class_name"],
            strategy="natural_key",
            key_name="pskg:productCode",
            key_value="CC-1",
        )


class _ForbiddenSemanticResolver:
    def resolve(self, *args, **kwargs):
        raise AssertionError("semantic resolver must not run for natural-key identity")


class _UpsertTx:
    def run(self, query, **kwargs):
        assert "MERGE (n:`BusinessRule`" in query
        return _Result(
            single={
                "node_id": "node-natural",
                "properties": {
                    "productCode": "CC-1",
                    "description": "same entity",
                },
            }
        )


def test_natural_key_bypasses_semantic_resolution():
    store = Neo4jGraphStore(
        mapper=_SimpleMapper(),
        identity_resolver=_NaturalKeyResolver(),
        semantic_resolver=_ForbiddenSemanticResolver(),
    )
    node_id = store.upsert_node(
        _UpsertTx(),
        class_name="pskg:BankingProduct",
        properties={
            "pskg:productCode": "CC-1",
            "pskg:description": "same entity",
        },
    )

    assert node_id == "node-natural"


class _CleanupTx:
    def __init__(self, *, supported: bool):
        self.supported = supported
        self.queries: list[str] = []

    def run(self, query, **kwargs):
        self.queries.append(query)
        if "MATCH (old:IngestionSourceAssertion" in query:
            return _Result(
                records=[
                    {
                        "kind": "PROPERTY",
                        "assertion_key": "property:node-1:pskg:description",
                        "payload_json": (
                            '{"className":"pskg:BusinessRule",'
                            '"propertyName":"pskg:description",'
                            '"neo4jPropertyKey":"description"}'
                        ),
                        "node_id": "node-1",
                    },
                    {
                        "kind": "EDGE",
                        "assertion_key": "edge:rel-1",
                        "payload_json": '{"relationshipElementId":"rel-1"}',
                        "node_id": None,
                    },
                ]
            )
        if "RETURN count(support) > 0 AS supported" in query:
            return _Result(single={"supported": self.supported})
        if "INGESTION_EVIDENCED_BY" in query and "DELETE r" in query:
            return _Result()
        if "REMOVE n.`description`" in query:
            return _Result(single={"removed": True})
        if "WHERE elementId(r) = $relationship_id" in query:
            return _Result(single={"existed": True})
        if "DETACH DELETE n" in query:
            return _Result(single={"deletable": True})
        raise AssertionError(query)


def test_stale_cleanup_removes_only_unowned_facts():
    result = cleanup_stale_assertions(
        _CleanupTx(supported=False),
        old_version_id="old",
        new_version_id="new",
        mapper=None,
    )

    assert result == {"properties": 1, "edges": 1, "nodes": 1}


def test_stale_cleanup_keeps_facts_owned_by_another_current_source():
    result = cleanup_stale_assertions(
        _CleanupTx(supported=True),
        old_version_id="old",
        new_version_id="new",
        mapper=None,
    )

    assert result == {"properties": 0, "edges": 0, "nodes": 0}


def test_delete_document_calls_reference_counted_source_store(monkeypatch):
    captured = {}

    class Store:
        def delete_document(self, document_id, *, mapper=None, if_missing="error"):
            captured["document_id"] = document_id
            captured["if_missing"] = if_missing
            return {"deleted": True, "documentId": document_id, "cleanup": {}}

    class Service:
        source_store = Store()
        writer = SimpleNamespace(mapper=object())

        def close(self):
            pass

    monkeypatch.setattr(ingestion_tools, "create_graph_persistence", lambda: Service())
    result = ingestion_tools.delete_document(SOURCE)

    assert result["success"] is True
    assert result["operation"] == "delete"
    assert captured["document_id"].startswith("doc_")
    assert captured["if_missing"] == "error"
