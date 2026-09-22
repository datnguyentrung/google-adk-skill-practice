"""Phase 5 — validated Neo4j persistence with verified source-version cutover."""

import json
import logging
import re
from pathlib import Path
from typing import Any

from app.config.neo4j import Neo4jClient
from app.core.schemas.ingestion.document import DocumentChunk
from app.core.schemas.ingestion.graph_patch import GraphPatchDraft
from app.core.schemas.ingestion.persistence import CommitStatus, FillResult, FillStatus
from app.core.schemas.ingestion.source import ExtractionCacheEntry, SourceLifecycle
from app.core.schemas.ingestion.validation import GraphPatchValidationResult
from app.core.trace_logger import pprint
from app.services.ingestion.document.preparation import DEFAULT_ONTOLOGY_PATH
from app.services.ingestion.identity.resolver import (
    create_product_sales_identity_resolver,
)
from app.services.ingestion.identity.semantic_resolution import (
    create_semantic_entity_resolver,
)
from app.services.ingestion.incremental.source_store import SourceLifecycleStore
from app.services.ingestion.incremental.staging_store import IngestionStagingStore
from app.services.ingestion.ontology.loader import OntologyLoader
from app.services.ingestion.ontology.registry import OntologyRegistry
from app.services.ingestion.persistence.mapping import Neo4jMapper
from app.services.ingestion.persistence.readback import verify_persisted_graph
from app.services.ingestion.persistence.writer import Neo4jGraphStore
from app.services.ingestion.validation.graph_validation import GraphValidation

logger = logging.getLogger(__name__)


class FillValidationError(ValueError):
    def __init__(self, result: GraphPatchValidationResult):
        self.result = result
        super().__init__("Graph patch is not ready for persistence")


class _ReadbackRollback(RuntimeError):
    """Internal signal: verification failed and the Neo4j transaction must rollback."""

    def __init__(self, receipt, write_result) -> None:
        self.receipt = receipt
        self.write_result = write_result
        super().__init__("Persisted graph readback verification failed")


class GraphPersistence:
    """Persist a validated patch and advance source version only after readback."""

    def __init__(
        self,
        client: Neo4jClient | None = None,
        validation: GraphValidation | None = None,
        writer: Neo4jGraphStore | None = None,
        source_store: SourceLifecycleStore | None = None,
        staging_store: IngestionStagingStore | None = None,
        ontology_path: str | Path = DEFAULT_ONTOLOGY_PATH,
    ) -> None:
        self.client = client or Neo4jClient()
        ontology = OntologyLoader.load(ontology_path)
        registry = OntologyRegistry(ontology)
        self.validation = validation or GraphValidation(ontology_path)
        self.writer = writer or Neo4jGraphStore(
            mapper=Neo4jMapper(registry),
            identity_resolver=create_product_sales_identity_resolver(registry),
            semantic_resolver=create_semantic_entity_resolver(),
        )
        self.source_store = source_store or SourceLifecycleStore(self.client)
        self.staging_store = staging_store or IngestionStagingStore(
            client=self.client, registry=registry
        )

    def fill(
        self,
        graph_patch: GraphPatchDraft | dict[str, Any],
        artifact_content_digest: str | None,
        source_chunks: list[DocumentChunk] | list[dict[str, Any]] | None = None,
        *,
        allow_partial_persistence: bool = False,
        source_lifecycle: SourceLifecycle | None = None,
        extraction_cache_entries: list[ExtractionCacheEntry] | None = None,
    ) -> dict[str, Any]:
        assessment = self.validation.assess(
            graph_patch,
            artifact_content_digest,
            source_chunks,
        )
        if (
            assessment.compiled_patch is None
            or not assessment.result.valid_for_extraction
        ):
            raise FillValidationError(assessment.result)
        partial_persistence = not assessment.result.valid_for_persistence
        if partial_persistence and not allow_partial_persistence:
            raise FillValidationError(assessment.result)

        patch = assessment.compiled_patch
        normalized_chunks = [
            item
            if isinstance(item, DocumentChunk)
            else DocumentChunk.model_validate(item)
            for item in (source_chunks or [])
        ]
        cache_entries = extraction_cache_entries or []
        if source_lifecycle is not None:
            # PENDING is committed separately so a crash before/during the graph
            # transaction remains observable and retryable.
            self.source_store.begin_pending(source_lifecycle)

        driver = self.client.get_driver()
        try:
            with driver.session(database=self.client.database_name) as session:

                def write_and_verify(tx):
                    write_result = self.writer.write_graph_patch(tx, patch)
                    if source_lifecycle is not None:
                        self.source_store.stage_written(
                            tx,
                            lifecycle=source_lifecycle,
                            chunks=normalized_chunks,
                            patch=patch,
                            write_result=write_result,
                            mapper=self.writer.mapper,
                        )

                    try:
                        readback = self.writer.read_graph_patch(tx, write_result)
                    except Exception as exc:
                        receipt = verify_persisted_graph(
                            patch,
                            write_result,
                            {"nodes": [], "relationships": []},
                            self.writer.mapper,
                        )
                        receipt.verified = False
                        receipt.commit_status = CommitStatus.ROLLED_BACK
                        receipt.mismatches.insert(0, f"readback failed: {exc}")
                        raise _ReadbackRollback(receipt, write_result) from exc

                    receipt = verify_persisted_graph(
                        patch,
                        write_result,
                        readback,
                        self.writer.mapper,
                    )
                    if not receipt.verified:
                        receipt.commit_status = CommitStatus.ROLLED_BACK
                        raise _ReadbackRollback(receipt, write_result)

                    if source_lifecycle is not None:
                        self.source_store.commit_verified(
                            tx,
                            lifecycle=source_lifecycle,
                            cache_entries=cache_entries,
                            node_count=len(patch.nodes),
                            edge_count=len(patch.edges),
                            mapper=self.writer.mapper,
                        )
                    receipt.commit_status = CommitStatus.COMMITTED
                    return write_result, receipt

                write_result, receipt = session.execute_write(write_and_verify)
            commit_status = CommitStatus.COMMITTED
        except _ReadbackRollback as exc:
            # The transaction has already been rolled back by the driver.
            write_result = exc.write_result
            receipt = exc.receipt
            commit_status = CommitStatus.ROLLED_BACK
            if source_lifecycle is not None:
                self.source_store.mark_failed(
                    source_lifecycle,
                    "; ".join(receipt.mismatches) or "readback verification failed",
                )
        except Exception as exc:
            if source_lifecycle is not None:
                try:
                    self.source_store.mark_failed(source_lifecycle, str(exc))
                except Exception as mark_exc:  # noqa: BLE001 - preserve original failure
                    logger.warning(
                        "Failed to mark source version as FAILED version_id=%s error=%s",
                        source_lifecycle.version_id,
                        mark_exc,
                    )
            raise

        return FillResult(
            status=(
                FillStatus.SUCCESS if receipt.verified else FillStatus.READBACK_MISMATCH
            ),
            commitStatus=commit_status,
            nodes=len(patch.nodes),
            edges=len(patch.edges),
            nodeIds=write_result.node_ids,
            relationshipIds=write_result.relationship_ids,
            receipt=receipt,
            partialPersistence=partial_persistence,
            persistenceMode="partial" if partial_persistence else "strict",
            readinessIssuesIgnored=(
                [
                    issue.model_dump(by_alias=True, exclude_none=True)
                    for issue in assessment.result.readiness_issues
                ]
                if partial_persistence
                else []
            ),
            documentId=(source_lifecycle.document_id if source_lifecycle else None),
            sourceVersionId=(source_lifecycle.version_id if source_lifecycle else None),
            sourceVersionStatus=(
                "COMMITTED"
                if source_lifecycle and receipt.verified
                else "FAILED"
                if source_lifecycle
                else None
            ),
        ).model_dump(by_alias=True, mode="json", exclude_none=True)

    def fill_staged_ingestion(
        self,
        ingestion_id: str,
        source_lifecycle: SourceLifecycle,
        *,
        source_chunks: list[DocumentChunk] | None = None,
        cache_entries: list[ExtractionCacheEntry] | None = None,
    ) -> dict[str, Any]:
        """
        Promote persistent staging nodes/edges directly to domain graph in Neo4j
        preserving full ontology labels, properties, and relationship types.
        """
        if source_lifecycle is not None:
            self.source_store.begin_pending(source_lifecycle)

        mapper = self.writer.mapper

        def _map_label(class_name: str) -> str:
            try:
                return mapper.class_to_label(class_name)
            except Exception:
                name = class_name.split(":")[-1]
                return re.sub(r"[^A-Za-z0-9_]", "_", name)

        def _map_prop(prop_name: str) -> str:
            try:
                return mapper.property_to_key(prop_name)
            except Exception:
                name = prop_name.split(":")[-1]
                return re.sub(r"[^A-Za-z0-9_]", "_", name)

        def _map_rel(edge_name: str) -> str:
            try:
                return mapper.edge_to_type(edge_name)
            except Exception:
                name = edge_name.split(":")[-1]
                name = re.sub(r"(?<!^)(?=[A-Z])", "_", name).upper()
                return re.sub(r"[^A-Za-z0-9_]", "_", name)

        driver = self.client.get_driver()
        with driver.session(database=self.client.database_name) as session:

            def _promote_tx(tx):
                # 1. Fetch staged entities and their properties
                staged_entities = tx.run(
                    """
                    MATCH (e:IngestionStagedEntity {ingestionId: $ingestion_id})
                    OPTIONAL MATCH (e)-[:HAS_STAGED_PROPERTY]->(p:IngestionStagedProperty)
                    WITH e, collect({name: p.propertyName, val: p.valueJson}) AS props
                    RETURN e.entityKey AS entityKey, e.className AS className, e.confidence AS confidence,
                           e.sourceVersionId AS sourceVersionId, props
                    """,
                    ingestion_id=ingestion_id,
                ).data()

                print(
                    f"\n[TRACE][FILL] Starting domain graph promotion for Ingestion ID {ingestion_id}:"
                )
                print(f"  Staged entities to promote: {len(staged_entities)}")

                promoted_entities = 0
                for se in staged_entities:
                    label = _map_label(se["className"])
                    props_dict: dict[str, Any] = {"entityKey": se["entityKey"]}
                    for p in se["props"]:
                        if p.get("name"):
                            key = _map_prop(p["name"])
                            val_json = p.get("val")
                            if val_json is not None:
                                try:
                                    props_dict[key] = json.loads(val_json)
                                except Exception:
                                    props_dict[key] = val_json

                    # Check target entity


                    tx.run(
                        f"""
                        MERGE (d:`{label}` {{entityKey: $entity_key}})
                        ON CREATE SET d += $props, d.className = $class_name, d.confidence = $confidence, d.sourceVersionId = $version_id
                        ON MATCH SET d += $props
                        """,
                        entity_key=se["entityKey"],
                        props=props_dict,
                        class_name=se["className"],
                        confidence=se["confidence"],
                        version_id=se["sourceVersionId"],
                    )
                    promoted_entities += 1

                # 2. Fetch staged edges
                staged_edges = tx.run(
                    """
                    MATCH (eg:IngestionStagedEdge {ingestionId: $ingestion_id})

                    MATCH (src_stage:IngestionStagedEntity {
                        ingestionId: $ingestion_id,
                        entityKey: eg.sourceEntityKey
                    })

                    MATCH (tgt_stage:IngestionStagedEntity {
                        ingestionId: $ingestion_id,
                        entityKey: eg.targetEntityKey
                    })

                    RETURN
                        eg.edgeKey AS edgeKey,
                        eg.edgeName AS edgeName,
                        eg.sourceEntityKey AS sourceEntityKey,
                        eg.targetEntityKey AS targetEntityKey,
                        eg.confidence AS confidence,
                        eg.sourceVersionId AS sourceVersionId,
                        src_stage.className AS sourceClassName,
                        tgt_stage.className AS targetClassName
                    """,
                    ingestion_id=ingestion_id,
                ).data()

                print(f"  Staged edges to promote: {len(staged_edges)}")

                promoted_edges = 0

                for seg in staged_edges:
                    rel_type = _map_rel(seg["edgeName"])

                    src_label = _map_label(seg["sourceClassName"])
                    tgt_label = _map_label(seg["targetClassName"])

                    result = tx.run(
                        f"""
                        MATCH (src:`{src_label}` {{entityKey: $src_key}})
                        MATCH (tgt:`{tgt_label}` {{entityKey: $tgt_key}})

                        MERGE (src)-[r:`{rel_type}`]->(tgt)

                        ON CREATE SET
                            r.confidence = $confidence,
                            r.sourceVersionId = $version_id

                        ON MATCH SET
                            r.confidence =
                                CASE
                                    WHEN $confidence > coalesce(r.confidence, 0)
                                    THEN $confidence
                                    ELSE r.confidence
                                END,
                            r.sourceVersionId = $version_id

                        RETURN count(r) AS matched
                        """,
                        src_key=seg["sourceEntityKey"],
                        tgt_key=seg["targetEntityKey"],
                        confidence=seg["confidence"],
                        version_id=seg["sourceVersionId"],
                    ).single()

                    matched = result["matched"] if result else 0

                    if matched != 1:
                        raise RuntimeError(
                            "Domain edge promotion endpoint mismatch: "
                            f"edge={seg['edgeName']} "
                            f"source={seg['sourceEntityKey']} ({src_label}) "
                            f"target={seg['targetEntityKey']} ({tgt_label}) "
                            f"matched={matched}"
                        )

                    promoted_edges += 1

                return promoted_entities, promoted_edges

            entity_count, edge_count = session.execute_write(_promote_tx)

            if source_lifecycle is not None:
                session.execute_write(
                    lambda tx: self.source_store.commit_verified(
                        tx,
                        lifecycle=source_lifecycle,
                        cache_entries=cache_entries or [],
                        node_count=entity_count,
                        edge_count=edge_count,
                        mapper=self.writer.mapper,
                    )
                )
            session.execute_write(
                lambda tx: self.staging_store.purge_staging_tx(tx, ingestion_id)
            )

        print(
            f"[TRACE][FILL] Promotion completed: Promoted Nodes={entity_count} | Promoted Relationships={edge_count}"
        )
        return {
            "success": True,
            "status": "SUCCESS",
            "commitStatus": "COMMITTED",
            "nodes": entity_count,
            "edges": edge_count,
            "sourceVersionId": source_lifecycle.version_id
            if source_lifecycle
            else None,
            "sourceVersionStatus": "COMMITTED",
        }

    def close(self) -> None:
        self.client.close_driver()


def create_graph_persistence(
    ontology_path: str | Path = DEFAULT_ONTOLOGY_PATH,
    *,
    validation: GraphValidation | None = None,
) -> GraphPersistence:
    ontology = OntologyLoader.load(ontology_path)
    registry = OntologyRegistry(ontology)
    client = Neo4jClient()
    return GraphPersistence(
        client=client,
        validation=validation or GraphValidation(ontology_path),
        writer=Neo4jGraphStore(
            mapper=Neo4jMapper(registry),
            identity_resolver=create_product_sales_identity_resolver(registry),
            semantic_resolver=create_semantic_entity_resolver(),
        ),
        source_store=SourceLifecycleStore(client),
        staging_store=IngestionStagingStore(client=client, registry=registry),
    )
