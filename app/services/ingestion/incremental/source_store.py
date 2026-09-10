"""Neo4j-backed source lifecycle, lexical provenance, and extraction cache."""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Literal

from neo4j import Transaction

from app.config.neo4j import Neo4jClient
from app.core.schemas.ingestion.document import DocumentChunk
from app.core.schemas.ingestion.persistence import GraphWriteResult
from app.core.schemas.ingestion.source import ExtractionCacheEntry, SourceLifecycle
from app.services.ingestion.document.strategies import chunk_content_hash
from app.services.ingestion.incremental.cleanup import cleanup_stale_assertions
from app.services.ingestion.incremental.identity import effective_chunk_id

logger = logging.getLogger(__name__)


class DocumentNotFoundError(LookupError):
    """Raised when an incremental source operation targets an unknown document."""


class SourceLifecycleStore:
    """Persist source versions without advancing currentVersion before verification."""

    def __init__(self, client: Neo4jClient | None = None) -> None:
        self.client = client or Neo4jClient()

    def close(self) -> None:
        self.client.close_driver()

    def get_document_record(self, document_id: str) -> dict[str, Any] | None:
        """Return the current source record for a stable document id."""
        if not document_id or not document_id.strip():
            raise ValueError("'document_id' must be a non-empty string")
        driver = self.client.get_driver()
        with driver.session(database=self.client.database_name) as session:
            record = session.execute_read(
                lambda tx: tx.run(
                    """
                    MATCH (d:IngestionSourceDocument {documentId: $document_id})
                    OPTIONAL MATCH (v:IngestionSourceVersion {versionId: d.currentVersionId})
                    RETURN d.documentId AS document_id,
                           d.name AS name,
                           d.currentVersionId AS version_id,
                           d.currentIngestionSignature AS ingestion_signature,
                           d.currentContentHash AS content_hash,
                           v.status AS status
                    """,
                    document_id=document_id,
                ).single()
            )
        if record is None or not record["version_id"] or record["status"] != "COMMITTED":
            return None
        return dict(record)

    def get_current(self, lifecycle: SourceLifecycle) -> dict[str, Any] | None:
        driver = self.client.get_driver()
        with driver.session(database=self.client.database_name) as session:
            record = session.execute_read(
                lambda tx: tx.run(
                    """
                    MATCH (d:IngestionSourceDocument {documentId: $document_id})
                    OPTIONAL MATCH (v:IngestionSourceVersion {versionId: d.currentVersionId})
                    RETURN d.currentIngestionSignature AS signature,
                           d.currentVersionId AS version_id,
                           d.currentNodeCount AS node_count,
                           d.currentEdgeCount AS edge_count,
                           v.status AS status
                    """,
                    document_id=lifecycle.document_id,
                ).single()
            )
        if record is None:
            return None
        if (
            record["signature"] != lifecycle.ingestion_signature
            or record["version_id"] != lifecycle.version_id
            or record["status"] != "COMMITTED"
        ):
            return None
        return {
            "documentId": lifecycle.document_id,
            "sourceVersionId": lifecycle.version_id,
            "nodes": int(record["node_count"] or 0),
            "edges": int(record["edge_count"] or 0),
        }

    def is_current(self, lifecycle: SourceLifecycle) -> bool:
        return self.get_current(lifecycle) is not None

    def get_cached_fragment(self, cache_key: str) -> dict[str, Any] | None:
        driver = self.client.get_driver()
        with driver.session(database=self.client.database_name) as session:
            record = session.execute_read(
                lambda tx: tx.run(
                    """
                    MATCH (c:IngestionExtractionCache {cacheKey: $cache_key})
                    RETURN c.fragmentJson AS fragment_json
                    """,
                    cache_key=cache_key,
                ).single()
            )
        if record is None or not record["fragment_json"]:
            return None
        try:
            return json.loads(record["fragment_json"])
        except (TypeError, json.JSONDecodeError):
            logger.warning("Invalid ingestion cache payload cache_key=%s", cache_key)
            return None

    def begin_pending(self, lifecycle: SourceLifecycle) -> None:
        driver = self.client.get_driver()
        with driver.session(database=self.client.database_name) as session:
            session.execute_write(lambda tx: self._begin_pending(tx, lifecycle))

    @staticmethod
    def _begin_pending(tx: Transaction, lifecycle: SourceLifecycle) -> None:
        tx.run(
            """
            MERGE (d:IngestionSourceDocument {documentId: $document_id})
            ON CREATE SET d.createdAt = datetime()
            SET d.name = $document_name, d.updatedAt = datetime()
            MERGE (v:IngestionSourceVersion {versionId: $version_id})
            ON CREATE SET v.createdAt = datetime()
            SET v.documentId = $document_id,
                v.contentHash = $content_hash,
                v.configSignature = $config_signature,
                v.ingestionSignature = $ingestion_signature,
                v.ontologyDigest = $ontology_digest,
                v.skillDigest = $skill_digest,
                v.modelId = $model_id,
                v.chunkerVersion = $chunker_version,
                v.mapperVersion = $mapper_version,
                v.compilerVersion = $compiler_version,
                v.status = CASE WHEN v.status = 'COMMITTED' THEN v.status ELSE 'PENDING' END,
                v.updatedAt = datetime()
            MERGE (d)-[:HAS_SOURCE_VERSION]->(v)
            """,
            **_lifecycle_params(lifecycle),
        ).consume()

    def mark_failed(self, lifecycle: SourceLifecycle, reason: str) -> None:
        driver = self.client.get_driver()
        with driver.session(database=self.client.database_name) as session:
            session.execute_write(
                lambda tx: tx.run(
                    """
                    MATCH (v:IngestionSourceVersion {versionId: $version_id})
                    WHERE v.status <> 'COMMITTED'
                    SET v.status = 'FAILED', v.failureReason = $reason,
                        v.updatedAt = datetime()
                    """,
                    version_id=lifecycle.version_id,
                    reason=reason[:2000],
                ).consume()
            )

    def delete_document(
        self,
        document_id: str,
        *,
        mapper=None,
        if_missing: Literal["error", "ignore"] = "error",
    ) -> dict[str, Any]:
        """Deactivate a source and remove only facts no longer supported elsewhere."""
        if not document_id or not document_id.strip():
            raise ValueError("'document_id' must be a non-empty string")
        if if_missing not in {"error", "ignore"}:
            raise ValueError("if_missing must be 'error' or 'ignore'")
        driver = self.client.get_driver()
        with driver.session(database=self.client.database_name) as session:
            return session.execute_write(
                lambda tx: self._delete_document(
                    tx, document_id, mapper=mapper, if_missing=if_missing
                )
            )

    @staticmethod
    def _delete_document(
        tx: Transaction,
        document_id: str,
        *,
        mapper=None,
        if_missing: Literal["error", "ignore"] = "error",
    ) -> dict[str, Any]:
        record = tx.run(
            """
            MATCH (d:IngestionSourceDocument {documentId: $document_id})
            RETURN d.currentVersionId AS current_version_id, d.name AS name
            """,
            document_id=document_id,
        ).single()
        if record is None or not record["current_version_id"]:
            if if_missing == "ignore":
                return {
                    "deleted": False,
                    "documentId": document_id,
                    "reason": "NOT_CURRENT",
                }
            raise DocumentNotFoundError(
                f"No current ingestion document with id '{document_id}' exists."
            )

        old_version_id = str(record["current_version_id"])
        tx.run(
            """
            MATCH (d:IngestionSourceDocument {documentId: $document_id})
            MATCH (v:IngestionSourceVersion {versionId: $old_version_id})
            SET d.currentVersionId = null,
                d.currentIngestionSignature = null,
                d.currentContentHash = null,
                d.currentNodeCount = 0,
                d.currentEdgeCount = 0,
                d.deletedAt = datetime(), d.updatedAt = datetime()
            SET v.status = 'DELETED', v.deletedAt = datetime(), v.updatedAt = datetime()
            """,
            document_id=document_id,
            old_version_id=old_version_id,
        ).consume()
        cleanup = cleanup_stale_assertions(
            tx,
            old_version_id=old_version_id,
            new_version_id=None,
            mapper=mapper,
        )
        return {
            "deleted": True,
            "documentId": document_id,
            "documentName": record["name"],
            "sourceVersionId": old_version_id,
            "cleanup": cleanup,
        }

    def stage_written(
        self,
        tx: Transaction,
        *,
        lifecycle: SourceLifecycle,
        chunks: list[DocumentChunk],
        patch,
        write_result: GraphWriteResult,
        mapper=None,
    ) -> None:
        """Stage lexical/provenance graph in the same transaction as domain writes."""
        self._begin_pending(tx, lifecycle)
        tx.run(
            """
            MATCH (:IngestionSourceVersion {versionId: $version_id})
                  -[r:HAS_SOURCE_CHUNK]->()
            DELETE r
            """,
            version_id=lifecycle.version_id,
        ).consume()
        tx.run(
            """
            MATCH (a:IngestionSourceAssertion {versionId: $version_id})
            DETACH DELETE a
            """,
            version_id=lifecycle.version_id,
        ).consume()
        tx.run(
            """
            MATCH ()-[r:INGESTION_EVIDENCED_BY {versionId: $version_id}]->()
            DELETE r
            """,
            version_id=lifecycle.version_id,
        ).consume()

        chunk_by_index: dict[int, tuple[str, DocumentChunk]] = {}
        for chunk in chunks:
            chunk_id = effective_chunk_id(chunk)
            chunk_by_index[chunk.index] = (chunk_id, chunk)
            self._write_chunk(tx, lifecycle, chunk_id, chunk)

        for node in patch.nodes:
            node_id = write_result.node_ids.get(node.temp_id)
            if node_id is None:
                continue
            node_evidence = list(node.evidence)
            for items in node.property_evidence.values():
                node_evidence.extend(items)
            self._link_node_evidence(
                tx, lifecycle.version_id, node_id, node_evidence, chunk_by_index
            )
            self._write_node_assertions(
                tx, lifecycle, node, node_id, chunk_by_index, mapper=mapper
            )

        for edge_index, edge in enumerate(patch.edges):
            key = _relationship_key(edge_index, edge)
            relationship_id = write_result.relationship_ids.get(key)
            if relationship_id is None:
                continue
            self._write_edge_assertion(
                tx,
                lifecycle,
                edge,
                relationship_id,
                write_result,
                chunk_by_index,
            )

        tx.run(
            """
            MATCH (v:IngestionSourceVersion {versionId: $version_id})
            SET v.status = 'WRITTEN', v.writtenAt = datetime(),
                v.updatedAt = datetime()
            """,
            version_id=lifecycle.version_id,
        ).consume()

    @staticmethod
    def _write_chunk(
        tx: Transaction,
        lifecycle: SourceLifecycle,
        chunk_id: str,
        chunk: DocumentChunk,
    ) -> None:
        tx.run(
            """
            MATCH (v:IngestionSourceVersion {versionId: $version_id})
            MERGE (c:IngestionSourceChunk {chunkId: $chunk_id})
            SET c.documentId = $document_id,
                c.source = $source,
                c.chunkIndex = $chunk_index,
                c.section = $section,
                c.content = $content,
                c.contentHash = $content_hash,
                c.structuralPath = $structural_path,
                c.startLine = $start_line,
                c.endLine = $end_line,
                c.updatedAt = datetime()
            MERGE (v)-[r:HAS_SOURCE_CHUNK]->(c)
            SET r.ordinal = $chunk_index
            """,
            version_id=lifecycle.version_id,
            chunk_id=chunk_id,
            document_id=lifecycle.document_id,
            source=chunk.source,
            chunk_index=chunk.index,
            section=chunk.section,
            content=chunk.content,
            content_hash=chunk.content_hash or chunk_content_hash(chunk.content),
            structural_path=chunk.structural_path,
            start_line=chunk.start_line,
            end_line=chunk.end_line,
        ).consume()

    @staticmethod
    def _link_node_evidence(
        tx: Transaction,
        version_id: str,
        node_id: str,
        evidence,
        chunk_by_index: dict[int, tuple[str, DocumentChunk]],
    ) -> None:
        for chunk_id in _chunk_ids_for_evidence(evidence, chunk_by_index):
            tx.run(
                """
                MATCH (n) WHERE elementId(n) = $node_id
                MATCH (c:IngestionSourceChunk {chunkId: $chunk_id})
                MERGE (n)-[r:INGESTION_EVIDENCED_BY {versionId: $version_id}]->(c)
                SET r.updatedAt = datetime()
                """,
                node_id=node_id,
                chunk_id=chunk_id,
                version_id=version_id,
            ).consume()

    def _write_node_assertions(
        self,
        tx: Transaction,
        lifecycle: SourceLifecycle,
        node,
        node_id: str,
        chunk_by_index: dict[int, tuple[str, DocumentChunk]],
        *,
        mapper=None,
    ) -> None:
        self._write_assertion(
            tx,
            lifecycle=lifecycle,
            assertion_kind="NODE",
            assertion_key=f"node:{node_id}",
            node_id=node_id,
            properties={"className": node.class_name},
            evidence=node.evidence,
            chunk_by_index=chunk_by_index,
        )
        for property_name, property_evidence in node.property_evidence.items():
            if not property_evidence:
                continue
            self._write_assertion(
                tx,
                lifecycle=lifecycle,
                assertion_kind="PROPERTY",
                assertion_key=f"property:{node_id}:{property_name}",
                node_id=node_id,
                properties={
                    "className": node.class_name,
                    "propertyName": property_name,
                    "neo4jPropertyKey": (
                        mapper.property_to_key(property_name) if mapper is not None else None
                    ),
                    "valueJson": json.dumps(
                        node.properties.get(property_name),
                        ensure_ascii=False,
                        sort_keys=True,
                        default=str,
                    ),
                },
                evidence=property_evidence,
                chunk_by_index=chunk_by_index,
            )

    def _write_edge_assertion(
        self,
        tx: Transaction,
        lifecycle: SourceLifecycle,
        edge,
        relationship_id: str,
        write_result: GraphWriteResult,
        chunk_by_index: dict[int, tuple[str, DocumentChunk]],
    ) -> None:
        self._write_assertion(
            tx,
            lifecycle=lifecycle,
            assertion_kind="EDGE",
            assertion_key=f"edge:{relationship_id}",
            node_id=None,
            properties={
                "edgeName": edge.edge_name,
                "relationshipElementId": relationship_id,
                "sourceNodeId": write_result.node_ids.get(edge.source_temp_id),
                "targetNodeId": write_result.node_ids.get(edge.target_temp_id),
            },
            evidence=edge.evidence,
            chunk_by_index=chunk_by_index,
        )

    @staticmethod
    def _write_assertion(
        tx: Transaction,
        *,
        lifecycle: SourceLifecycle,
        assertion_kind: str,
        assertion_key: str,
        node_id: str | None,
        properties: dict[str, Any],
        evidence,
        chunk_by_index: dict[int, tuple[str, DocumentChunk]],
    ) -> None:
        assertion_id = _assertion_id(lifecycle.version_id, assertion_key)
        tx.run(
            """
            MERGE (a:IngestionSourceAssertion {assertionId: $assertion_id})
            SET a.versionId = $version_id,
                a.documentId = $document_id,
                a.kind = $kind,
                a.assertionKey = $assertion_key,
                a.payloadJson = $payload_json,
                a.updatedAt = datetime()
            """,
            assertion_id=assertion_id,
            version_id=lifecycle.version_id,
            document_id=lifecycle.document_id,
            kind=assertion_kind,
            assertion_key=assertion_key,
            payload_json=json.dumps(
                properties, ensure_ascii=False, sort_keys=True, default=str
            ),
        ).consume()
        if node_id is not None:
            tx.run(
                """
                MATCH (a:IngestionSourceAssertion {assertionId: $assertion_id})
                MATCH (n) WHERE elementId(n) = $node_id
                MERGE (a)-[:ABOUT_NODE]->(n)
                """,
                assertion_id=assertion_id,
                node_id=node_id,
            ).consume()
        for chunk_id in _chunk_ids_for_evidence(evidence, chunk_by_index):
            tx.run(
                """
                MATCH (a:IngestionSourceAssertion {assertionId: $assertion_id})
                MATCH (c:IngestionSourceChunk {chunkId: $chunk_id})
                MERGE (a)-[:EVIDENCED_BY]->(c)
                """,
                assertion_id=assertion_id,
                chunk_id=chunk_id,
            ).consume()

    @staticmethod
    def commit_verified(
        tx: Transaction,
        *,
        lifecycle: SourceLifecycle,
        cache_entries: list[ExtractionCacheEntry],
        node_count: int,
        edge_count: int,
        mapper=None,
    ) -> None:
        for entry in cache_entries:
            tx.run(
                """
                MERGE (c:IngestionExtractionCache {cacheKey: $cache_key})
                SET c.documentId = $document_id,
                    c.sourceVersionId = $source_version_id,
                    c.batchIndex = $batch_index,
                    c.chunkIds = $chunk_ids,
                    c.graphContextDigest = $graph_context_digest,
                    c.fragmentJson = $fragment_json,
                    c.updatedAt = datetime()
                """,
                **entry.model_dump(by_alias=False),
            ).consume()

        current = tx.run(
            """
            MATCH (d:IngestionSourceDocument {documentId: $document_id})
            RETURN d.currentVersionId AS current_version_id
            """,
            document_id=lifecycle.document_id,
        ).single()
        old_version_id = current["current_version_id"] if current else None

        tx.run(
            """
            MATCH (d:IngestionSourceDocument {documentId: $document_id})
            MATCH (v:IngestionSourceVersion {versionId: $version_id})
            SET v.status = 'COMMITTED', v.committedAt = datetime(),
                v.failureReason = null, v.updatedAt = datetime()
            SET d.currentVersionId = $version_id,
                d.currentIngestionSignature = $ingestion_signature,
                d.currentContentHash = $content_hash,
                d.currentNodeCount = $node_count,
                d.currentEdgeCount = $edge_count,
                d.deletedAt = null,
                d.updatedAt = datetime()
            """,
            document_id=lifecycle.document_id,
            version_id=lifecycle.version_id,
            ingestion_signature=lifecycle.ingestion_signature,
            content_hash=lifecycle.content_hash,
            node_count=node_count,
            edge_count=edge_count,
        ).consume()

        cleanup_stale_assertions(
            tx,
            old_version_id=old_version_id,
            new_version_id=lifecycle.version_id,
            mapper=mapper,
        )
        if old_version_id and old_version_id != lifecycle.version_id:
            tx.run(
                """
                MATCH (old:IngestionSourceVersion {versionId: $old_version_id})
                WHERE old.status = 'COMMITTED'
                SET old.status = 'SUPERSEDED', old.supersededAt = datetime(),
                    old.updatedAt = datetime()
                """,
                old_version_id=old_version_id,
            ).consume()


def _lifecycle_params(lifecycle: SourceLifecycle) -> dict[str, Any]:
    return lifecycle.model_dump(by_alias=False)


def _assertion_id(version_id: str, key: str) -> str:
    digest = hashlib.sha256(f"{version_id}\0{key}".encode()).hexdigest()
    return f"assert_{digest[:40]}"


def _chunk_ids_for_evidence(
    evidence,
    chunk_by_index: dict[int, tuple[str, DocumentChunk]],
) -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()
    for item in evidence:
        matched = chunk_by_index.get(item.chunk_index)
        if matched is None:
            continue
        chunk_id = matched[0]
        if chunk_id not in seen:
            ids.append(chunk_id)
            seen.add(chunk_id)
    return ids


def _relationship_key(index: int, edge) -> str:
    return (
        f"{index}:{edge.edge_name}:"
        f"{edge.source_temp_id}->{edge.target_temp_id}"
    )


__all__ = ["DocumentNotFoundError", "SourceLifecycleStore"]
