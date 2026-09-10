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
from app.services.ingestion.mapping.candidates import GlinerCandidateGenerator
from app.services.ingestion.orchestration import state as ingestion_state
from app.services.ingestion.orchestration import tools as ingestion_tools
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


def test_unchanged_committed_source_short_circuits_model(monkeypatch):
    context = FakeToolContext()
    workspace = _workspace()
    ingestion_state._store_workspace(context, workspace)

    async def fake_begin(*args, **kwargs):
        return {
            "success": True,
            "stage": "batching",
            "ingestionId": workspace.ingestion_id,
        }

    monkeypatch.setattr(ingestion_tools, "begin_ingestion", fake_begin)
    monkeypatch.setattr(
        ingestion_tools,
        "_current_source_snapshot",
        lambda _: {
            "documentId": "doc_test",
            "sourceVersionId": "srcv_test",
            "nodes": 7,
            "edges": 4,
        },
    )
    monkeypatch.setattr(
        ingestion_tools,
        "_get_graph_mapper",
        lambda: (_ for _ in ()).throw(AssertionError("mapper must not be created")),
    )

    result = asyncio.run(
        ingestion_tools.ingest_document_end_to_end(SOURCE, context, persist=True)
    )

    assert result["success"] is True
    assert result["incrementalNoOp"] is True
    assert result["nodes"] == 7
    assert result["edges"] == 4
    assert result["verificationStatus"] == "verified"


def test_batch_cache_hit_reuses_fragment_without_model_call(monkeypatch):
    context = FakeToolContext()
    workspace = _workspace()
    ingestion_state._store_workspace(context, workspace)
    batch = workspace.batches[0]

    async def fake_begin(*args, **kwargs):
        return {
            "success": True,
            "stage": "batching",
            "ingestionId": workspace.ingestion_id,
            "nextBatch": _batch_payload(workspace, batch),
        }

    class ForbiddenMapper:
        def map_batch(self, **kwargs):
            raise AssertionError("cache hit must bypass model mapping")

    async def fake_fill(*args, **kwargs):
        return {
            "success": True,
            "stage": "completed",
            "commitStatus": "committed",
            "verificationStatus": "verified",
            "nodes": 0,
            "edges": 0,
        }

    monkeypatch.setattr(ingestion_tools, "begin_ingestion", fake_begin)
    monkeypatch.setattr(ingestion_tools, "_current_source_snapshot", lambda _: None)
    monkeypatch.setattr(ingestion_tools, "_get_graph_mapper", lambda: ForbiddenMapper())
    monkeypatch.setattr(
        ingestion_tools,
        "_cached_batch_fragment",
        lambda *args: (_empty_fragment(), "cache_test", "context_test"),
    )
    monkeypatch.setattr(
        ingestion_tools,
        "finalize_ingestion",
        lambda *args, **kwargs: {"success": True, "stage": "ready_to_fill"},
    )
    monkeypatch.setattr(ingestion_tools, "fill_ingestion", fake_fill)
    monkeypatch.setattr(
        ingestion_state, "_current_provenance", lambda _: workspace.provenance
    )

    result = asyncio.run(
        ingestion_tools.ingest_document_end_to_end(SOURCE, context, persist=True)
    )

    stored = ingestion_state._load_workspace(context)
    assert result["success"] is True
    assert stored is not None
    assert stored.batches[0].extraction_cache_hit is True
    assert stored.batches[0].extraction_cache_key == "cache_test"
    assert result["workspaceStats"]["cacheHitBatches"] == 1


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


def test_gliner_is_candidate_only_and_returns_spans(monkeypatch):
    class FakeGLiNER:
        def __init__(self):
            self.config = SimpleNamespace(max_len=384)
            self.data_processor = SimpleNamespace(
                words_splitter=lambda text: [(text, 0, len(text))]
            )

        @classmethod
        def from_pretrained(cls, model_name):
            return cls()

        def predict_entities(self, text, labels, threshold):
            return [
                {
                    "text": "Flexi Rewards",
                    "label": labels[0],
                    "score": 0.91,
                    "start": 0,
                    "end": 13,
                }
            ]

    monkeypatch.setitem(sys.modules, "gliner", SimpleNamespace(GLiNER=FakeGLiNER))
    registry = SimpleNamespace(
        list_classes=lambda: ["pskg:BankingProduct"],
        get_class=lambda _: SimpleNamespace(
            label="Banking Product", local_name="BankingProduct"
        ),
    )
    generator = GlinerCandidateGenerator(registry, model_name="fake")
    mentions = generator.generate(
        [DocumentChunk(index=0, source=SOURCE, content="Flexi Rewards")]
    )

    assert len(mentions) == 1
    assert mentions[0].text == "Flexi Rewards"
    assert mentions[0].chunk_index == 0


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


def test_update_document_reuses_end_to_end_pipeline(monkeypatch):
    class Store:
        def get_document_record(self, document_id):
            return {"document_id": document_id, "version_id": "srcv-current"}

        def close(self):
            pass

    async def fake_ingest(*args, **kwargs):
        return {"success": True, "stage": "completed", "incrementalNoOp": False}

    monkeypatch.setattr(ingestion_tools, "SourceLifecycleStore", Store)
    monkeypatch.setattr(ingestion_tools, "ingest_document_end_to_end", fake_ingest)
    result = asyncio.run(
        ingestion_tools.update_ingestion_document(SOURCE, FakeToolContext())
    )

    assert result["success"] is True
    assert result["operation"] == "update"
    assert result["replacedExisting"] is True
    assert result["noOp"] is False


def test_update_document_if_missing_error_matches_graphrag(monkeypatch):
    class Store:
        def get_document_record(self, document_id):
            return None

        def close(self):
            pass

    monkeypatch.setattr(ingestion_tools, "SourceLifecycleStore", Store)
    with pytest.raises(DocumentNotFoundError):
        asyncio.run(
            ingestion_tools.update_ingestion_document(SOURCE, FakeToolContext())
        )


def test_update_document_if_missing_ingest_upserts(monkeypatch):
    class Store:
        def get_document_record(self, document_id):
            return None

        def close(self):
            pass

    async def fake_ingest(*args, **kwargs):
        return {"success": True, "stage": "completed", "incrementalNoOp": False}

    monkeypatch.setattr(ingestion_tools, "SourceLifecycleStore", Store)
    monkeypatch.setattr(ingestion_tools, "ingest_document_end_to_end", fake_ingest)
    result = asyncio.run(
        ingestion_tools.update_ingestion_document(
            SOURCE, FakeToolContext(), if_missing="ingest"
        )
    )

    assert result["success"] is True
    assert result["replacedExisting"] is False


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
    result = ingestion_tools.delete_ingestion_document(SOURCE)

    assert result["success"] is True
    assert result["deleted"] is True
    assert captured["document_id"].startswith("doc_")
    assert captured["if_missing"] == "error"


def test_apply_changes_uses_graphrag_dispatch_order(monkeypatch):
    calls: list[tuple[str, str]] = []

    def fake_delete(name, *, if_missing="error"):
        calls.append(("delete", name))
        assert if_missing == "error"
        return {"success": True, "deleted": True}

    async def fake_update(name, *args, **kwargs):
        calls.append(("update", name))
        return {"success": True, "stage": "completed"}

    async def fake_ingest(name, *args, **kwargs):
        calls.append(("ingest", name))
        return {"success": True, "stage": "completed"}

    monkeypatch.setattr(ingestion_tools, "delete_ingestion_document", fake_delete)
    monkeypatch.setattr(ingestion_tools, "update_ingestion_document", fake_update)
    monkeypatch.setattr(ingestion_tools, "ingest_document_end_to_end", fake_ingest)

    result = asyncio.run(
        ingestion_tools.apply_ingestion_changes(
            added=["new.md"],
            modified=["changed.md"],
            deleted=["old.md"],
            tool_context=FakeToolContext(),
        )
    )
    assert calls == [
        ("delete", "old.md"),
        ("update", "changed.md"),
        ("ingest", "new.md"),
    ]
    assert result["success"] is True
    assert result["results"]["deleted"][0]["isSuccess"] is True
    assert result["results"]["modified"][0]["isSuccess"] is True
    assert result["results"]["added"][0]["isSuccess"] is True


def test_apply_changes_collects_per_file_errors(monkeypatch):
    def fake_delete(name, *, if_missing="error"):
        assert if_missing == "error"
        raise LookupError(f"missing {name}")

    async def fake_update(name, *args, **kwargs):
        raise RuntimeError(f"update failed {name}")

    async def fake_ingest(name, *args, **kwargs):
        return {"success": True, "stage": "completed"}

    monkeypatch.setattr(ingestion_tools, "delete_ingestion_document", fake_delete)
    monkeypatch.setattr(ingestion_tools, "update_ingestion_document", fake_update)
    monkeypatch.setattr(ingestion_tools, "ingest_document_end_to_end", fake_ingest)
    result = asyncio.run(
        ingestion_tools.apply_ingestion_changes(
            added=["new.md"],
            modified=["changed.md"],
            deleted=["old.md"],
            tool_context=FakeToolContext(),
        )
    )

    assert result["success"] is False
    assert result["results"]["deleted"][0]["errorType"] == "LookupError"
    assert result["results"]["modified"][0]["errorType"] == "RuntimeError"
    assert result["results"]["added"][0]["isSuccess"] is True


def test_apply_changes_rejects_overlapping_sources():
    with pytest.raises(ValueError, match="more than one change list"):
        asyncio.run(
            ingestion_tools.apply_ingestion_changes(
                added=["same.md"],
                modified=["same.md"],
                deleted=[],
                tool_context=FakeToolContext(),
            )
        )


def test_gliner_candidate_band_marks_low_confidence_unknown(monkeypatch):
    class FakeGLiNER:
        def __init__(self):
            self.config = SimpleNamespace(max_len=384)
            self.data_processor = SimpleNamespace(
                words_splitter=lambda text: [(text, 0, len(text))]
            )

        @classmethod
        def from_pretrained(cls, model_name):
            return cls()

        def predict_entities(self, text, labels, threshold):
            return [
                {
                    "text": text,
                    "label": labels[0],
                    "score": 0.60,
                    "start": 0,
                    "end": len(text),
                }
            ]

    GlinerCandidateGenerator._MODEL_CACHE.pop("band-test", None)
    monkeypatch.setitem(sys.modules, "gliner", SimpleNamespace(GLiNER=FakeGLiNER))
    registry = SimpleNamespace(
        list_classes=lambda: ["pskg:BankingProduct"],
        get_class=lambda _: SimpleNamespace(
            label="Banking Product", local_name="BankingProduct"
        ),
    )
    mentions = GlinerCandidateGenerator(
        registry, model_name="band-test", threshold=0.75, candidate_threshold=0.55
    ).generate([DocumentChunk(index=0, source=SOURCE, content="Flexi Rewards")])
    assert mentions[0].label == "Unknown"


def test_gliner_shared_model_cache_reuses_model(monkeypatch):
    loads = {"count": 0}

    class FakeGLiNER:
        def __init__(self):
            self.config = SimpleNamespace(max_len=384)
            self.data_processor = SimpleNamespace(
                words_splitter=lambda text: [(text, 0, len(text))]
            )

        @classmethod
        def from_pretrained(cls, model_name):
            loads["count"] += 1
            return cls()

        def predict_entities(self, text, labels, threshold):
            return []

    GlinerCandidateGenerator._MODEL_CACHE.pop("cache-test", None)
    monkeypatch.setitem(sys.modules, "gliner", SimpleNamespace(GLiNER=FakeGLiNER))
    registry = SimpleNamespace(
        list_classes=lambda: ["pskg:BankingProduct"],
        get_class=lambda _: SimpleNamespace(
            label="Banking Product", local_name="BankingProduct"
        ),
    )
    first = GlinerCandidateGenerator(registry, model_name="cache-test")
    second = GlinerCandidateGenerator(registry, model_name="cache-test")
    first.generate([DocumentChunk(index=0, source=SOURCE, content="A")])
    second.generate([DocumentChunk(index=1, source=SOURCE, content="B")])
    assert loads["count"] == 1


def test_gliner_windowing_covers_long_chunk(monkeypatch):
    calls: list[str] = []

    def split_words(text: str):
        spans = []
        cursor = 0
        for word in text.split():
            start = text.index(word, cursor)
            end = start + len(word)
            spans.append((word, start, end))
            cursor = end
        return spans

    class FakeGLiNER:
        def __init__(self):
            self.config = SimpleNamespace(max_len=384)
            self.data_processor = SimpleNamespace(words_splitter=split_words)

        @classmethod
        def from_pretrained(cls, model_name):
            return cls()

        def predict_entities(self, text, labels, threshold):
            calls.append(text)
            return []

    GlinerCandidateGenerator._MODEL_CACHE.pop("window-test", None)
    monkeypatch.setitem(sys.modules, "gliner", SimpleNamespace(GLiNER=FakeGLiNER))
    registry = SimpleNamespace(
        list_classes=lambda: ["pskg:BankingProduct"],
        get_class=lambda _: SimpleNamespace(
            label="Banking Product", local_name="BankingProduct"
        ),
    )
    generator = GlinerCandidateGenerator(
        registry,
        model_name="window-test",
        window_tokens=2,
        window_overlap=1,
    )
    generator.generate(
        [DocumentChunk(index=0, source=SOURCE, content="one two three four five")]
    )
    assert len(calls) == 4
