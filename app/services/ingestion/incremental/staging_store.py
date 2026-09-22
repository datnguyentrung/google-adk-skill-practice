"""Ingestion Persistent Staging Store using isolated Neo4j nodes/relationships.

Stores batch facts (entities, property facts, edges, chunk coverage, conflicts, pending edges)
under dedicated Neo4j labels per ingestion session and source version.
"""

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from app.config.neo4j import Neo4jClient
from app.core.trace_logger import trace_pprint
from app.services.ingestion.ontology import OntologyLoader, OntologyRegistry

logger = logging.getLogger(__name__)

DEFAULT_ONTOLOGY_PATH = Path(
    "app/data/ontology/product_sales_knowledge_graph_base_v3_1.ontology.json"
)


class IngestionStagingStore:
    """Manages persistent staging data in Neo4j without polluting domain nodes."""

    def __init__(
        self,
        client: Neo4jClient | None = None,
        registry: OntologyRegistry | None = None,
        ontology_path: str | Path = DEFAULT_ONTOLOGY_PATH,
    ) -> None:
        self.client = client or Neo4jClient()
        self.registry = registry or OntologyRegistry(OntologyLoader.load(ontology_path))

    def close(self) -> None:
        self.client.close_driver()

    def stage_batch_facts(
        self,
        *,
        ingestion_id: str,
        source_version_id: str,
        batch_index: int,
        entities: list[dict[str, Any]],
        properties: list[dict[str, Any]],
        edges: list[dict[str, Any]],
        coverage: list[dict[str, Any]],
        conflicts: list[dict[str, Any]] | None = None,
        pending_edges: list[dict[str, Any]] | None = None,
    ) -> dict[str, int]:
        """
        Write canonical entities, property facts, edges, coverage, conflicts and pending edges
        to Neo4j staging labels for a given batch.
        """
        conflicts = conflicts or []
        pending_edges = pending_edges or []

        staging_payload_summary = {
            "batch_index": batch_index,
            "ingestion_id": ingestion_id,
            "entities_count": len(entities),
            "properties_count": len(properties),
            "edges_count": len(edges),
            "pending_edges_count": len(pending_edges),
            "coverage_count": len(coverage),
            "conflicts_count": len(conflicts),
        }
        trace_pprint(
            f"[TRACE][STAGING_PAYLOAD] Staging Batch {batch_index} for Ingestion ID {ingestion_id}:",
            staging_payload_summary,
        )



        driver = self.client.get_driver()
        with driver.session(database=self.client.database_name) as session:

            def _tx_stage(tx):
                # 1. Entities
                if entities:
                    tx.run(
                        """
                        UNWIND $entities AS entity
                        MERGE (e:IngestionStagedEntity {
                            ingestionId: $ingestion_id,
                            entityKey: entity.entityKey
                        })
                        ON CREATE SET
                            e.sourceVersionId = $version_id,
                            e.tempId = entity.tempId,
                            e.className = entity.className,
                            e.confidence = entity.confidence,
                            e.firstBatchIndex = $batch_index,
                            e.createdAt = timestamp()
                        ON MATCH SET
                            e.confidence = CASE WHEN entity.confidence > e.confidence THEN entity.confidence ELSE e.confidence END
                        """,
                        ingestion_id=ingestion_id,
                        version_id=source_version_id,
                        batch_index=batch_index,
                        entities=entities,
                    )

                # 2. Properties & Evidence
                if properties:
                    existing_records = tx.run(
                        """
                        MATCH (p:IngestionStagedProperty {ingestionId: $ingestion_id})
                        RETURN p.entityKey AS entityKey, p.propertyName AS propertyName,
                               p.valueJson AS valueJson, p.valueHash AS valueHash, p.isList AS isList
                        """,
                        ingestion_id=ingestion_id,
                    ).data()
                    existing_map = {
                        (r["entityKey"], r["propertyName"]): r for r in existing_records
                    }

                    processed_properties = []
                    for prop in properties:
                        key = (prop["entityKey"], prop["propertyName"])
                        class_name = prop.get("className")
                        if not class_name:
                            entity_key = prop.get("entityKey", "")
                            if "|" in entity_key:
                                class_name = entity_key.split("|")[0]
                            elif entity_key.startswith("entity:"):
                                parts = entity_key.split(":")
                                if len(parts) >= 2:
                                    class_name = parts[1]

                        if key in existing_map:
                            existing = existing_map[key]

                            allows_multiple = (
                                self.registry.property_allows_multiple_values(
                                    class_name or "",
                                    prop["propertyName"],
                                )
                            )

                            if allows_multiple:
                                try:
                                    ex_val = (
                                        json.loads(existing["valueJson"])
                                        if existing.get("valueJson")
                                        else []
                                    )

                                    inc_val = (
                                        json.loads(prop["valueJson"])
                                        if prop.get("valueJson")
                                        else []
                                    )

                                    if not isinstance(ex_val, list):
                                        ex_val = [ex_val] if ex_val is not None else []

                                    if not isinstance(inc_val, list):
                                        inc_val = (
                                            [inc_val] if inc_val is not None else []
                                        )

                                    combined = []

                                    for value in ex_val + inc_val:
                                        if value not in combined:
                                            combined.append(value)

                                    prop["valueJson"] = json.dumps(
                                        combined,
                                        ensure_ascii=False,
                                        sort_keys=True,
                                    )

                                    prop["valueHash"] = hashlib.sha256(
                                        prop["valueJson"].encode("utf-8")
                                    ).hexdigest()[:12]

                                    # Quan trọng
                                    prop["isList"] = True

                                except Exception:
                                    logger.exception(
                                        "Failed to merge multi-valued property "
                                        "entityKey=%s propertyName=%s",
                                        prop["entityKey"],
                                        prop["propertyName"],
                                    )
                                    raise

                                processed_properties.append(prop)
                                existing_map[key] = prop

                            else:
                                if prop["valueHash"] != existing["valueHash"]:
                                    conflict_key = (
                                        f"{prop['entityKey']}|"
                                        f"{prop['propertyName']}|"
                                        f"{batch_index}"
                                    )

                                    conflicts.append(
                                        {
                                            "conflictKey": conflict_key,
                                            "entityKey": prop["entityKey"],
                                            "propertyName": prop["propertyName"],
                                            "existingValueJson": existing["valueJson"],
                                            "incomingValueJson": prop["valueJson"],
                                        }
                                    )

                                    prop_copy = dict(prop)
                                    prop_copy["valueJson"] = existing["valueJson"]
                                    prop_copy["valueHash"] = existing["valueHash"]

                                    processed_properties.append(prop_copy)
                                    existing_map[key] = prop_copy

                                else:
                                    processed_properties.append(prop)
                                    existing_map[key] = prop
                        else:
                            processed_properties.append(prop)
                            existing_map[key] = prop

                    tx.run(
                        """
                        UNWIND $properties AS prop
                        MERGE (p:IngestionStagedProperty {
                            ingestionId: $ingestion_id,
                            entityKey: prop.entityKey,
                            propertyName: prop.propertyName
                        })
                        ON CREATE SET
                            p.sourceVersionId = $version_id,
                            p.valueJson = prop.valueJson,
                            p.valueHash = prop.valueHash,
                            p.isList = prop.isList,
                            p.batchIndex = $batch_index
                        ON MATCH SET
                            p.valueJson = prop.valueJson,
                            p.valueHash = prop.valueHash,
                            p.isList = prop.isList

                        WITH p, prop
                        MATCH (e:IngestionStagedEntity {ingestionId: $ingestion_id, entityKey: prop.entityKey})
                        MERGE (e)-[:HAS_STAGED_PROPERTY]->(p)

                        WITH p, prop
                        UNWIND prop.evidence AS ev
                        MERGE (evNode:IngestionStagedEvidence {
                            ingestionId: $ingestion_id,
                            evidenceKey: ev.evidenceKey
                        })
                        ON CREATE SET
                            evNode.chunkIndex = ev.chunkIndex,
                            evNode.quote = ev.quote
                        MERGE (p)-[:HAS_EVIDENCE]->(evNode)
                        """,
                        ingestion_id=ingestion_id,
                        version_id=source_version_id,
                        batch_index=batch_index,
                        properties=processed_properties,
                    )

                # 3. Edges
                if edges:
                    tx.run(
                        """
                        UNWIND $edges AS edge
                        MERGE (eg:IngestionStagedEdge {
                            ingestionId: $ingestion_id,
                            edgeKey: edge.edgeKey
                        })
                        ON CREATE SET
                            eg.sourceVersionId = $version_id,
                            eg.edgeName = edge.edgeName,
                            eg.sourceEntityKey = edge.sourceEntityKey,
                            eg.targetEntityKey = edge.targetEntityKey,
                            eg.confidence = edge.confidence,
                            eg.batchIndex = $batch_index
                        ON MATCH SET
                            eg.confidence = CASE WHEN edge.confidence > eg.confidence THEN edge.confidence ELSE eg.confidence END

                        WITH eg, edge
                        MATCH (src:IngestionStagedEntity {ingestionId: $ingestion_id, entityKey: edge.sourceEntityKey})
                        MATCH (tgt:IngestionStagedEntity {ingestionId: $ingestion_id, entityKey: edge.targetEntityKey})
                        MERGE (src)-[:STAGED_EDGE {name: edge.edgeName}]->(tgt)
                        MERGE (src)-[:HAS_STAGED_EDGE_RECORD]->(eg)
                        """,
                        ingestion_id=ingestion_id,
                        version_id=source_version_id,
                        batch_index=batch_index,
                        edges=edges,
                    )

                # 4. Pending Edges
                if pending_edges:
                    tx.run(
                        """
                        UNWIND $pending_edges AS pe
                        MERGE (p:IngestionPendingEdge {
                            ingestionId: $ingestion_id,
                            pendingKey: pe.pendingKey
                        })
                        ON CREATE SET
                            p.sourceVersionId = $version_id,
                            p.edgeName = pe.edgeName,
                            p.sourceEntityKey = pe.sourceEntityKey,
                            p.targetEntityKey = pe.targetEntityKey,
                            p.unresolvedRef = pe.unresolvedRef,
                            p.batchIndex = $batch_index
                        """,
                        ingestion_id=ingestion_id,
                        version_id=source_version_id,
                        batch_index=batch_index,
                        pending_edges=pending_edges,
                    )

                # 5. Coverage
                if coverage:
                    tx.run(
                        """
                        UNWIND $coverage AS cov
                        MERGE (c:IngestionChunkCoverage {
                            ingestionId: $ingestion_id,
                            chunkIndex: cov.chunkIndex
                        })
                        ON CREATE SET
                            c.sourceVersionId = $version_id,
                            c.decision = cov.decision,
                            c.reason = cov.reason,
                            c.batchIndex = $batch_index
                        ON MATCH SET
                            c.decision = cov.decision,
                            c.reason = cov.reason
                        """,
                        ingestion_id=ingestion_id,
                        version_id=source_version_id,
                        batch_index=batch_index,
                        coverage=coverage,
                    )

                # 6. Conflicts
                if conflicts:
                    tx.run(
                        """
                        UNWIND $conflicts AS conf
                        MERGE (c:IngestionConflict {
                            ingestionId: $ingestion_id,
                            conflictKey: conf.conflictKey
                        })
                        ON CREATE SET
                            c.sourceVersionId = $version_id,
                            c.entityKey = conf.entityKey,
                            c.propertyName = conf.propertyName,
                            c.existingValueJson = conf.existingValueJson,
                            c.incomingValueJson = conf.incomingValueJson,
                            c.batchIndex = $batch_index
                        """,
                        ingestion_id=ingestion_id,
                        version_id=source_version_id,
                        batch_index=batch_index,
                        conflicts=conflicts,
                    )

                # 7. Batch State
                tx.run(
                    """
                    MERGE (b:IngestionBatchState {
                        ingestionId: $ingestion_id,
                        batchIndex: $batch_index
                    })
                    ON CREATE SET
                        b.sourceVersionId = $version_id,
                        b.status = 'STAGED',
                        b.nodeCount = $node_count,
                        b.edgeCount = $edge_count,
                        b.coverageCount = $coverage_count,
                        b.stagedAt = timestamp()
                    ON MATCH SET
                        b.status = 'STAGED',
                        b.nodeCount = $node_count,
                        b.edgeCount = $edge_count,
                        b.coverageCount = $coverage_count,
                        b.stagedAt = timestamp()
                    """,
                    ingestion_id=ingestion_id,
                    version_id=source_version_id,
                    batch_index=batch_index,
                    node_count=len(entities),
                    edge_count=len(edges),
                    coverage_count=len(coverage),
                )

            session.execute_write(_tx_stage)

        return {
            "stagedEntities": len(entities),
            "stagedProperties": len(properties),
            "stagedEdges": len(edges),
            "pendingEdges": len(pending_edges),
            "coverageCount": len(coverage),
            "conflicts": len(conflicts),
        }

    def find_relevant_entities(
        self,
        ingestion_id: str,
        keywords: list[str] | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Find candidates in staging matching keywords, or all staged entities if keywords is None/empty."""
        driver = self.client.get_driver()
        with driver.session(database=self.client.database_name) as session:
            records = session.execute_read(
                lambda tx: tx.run(
                    """
                    MATCH (e:IngestionStagedEntity {ingestionId: $ingestion_id})
                    OPTIONAL MATCH (e)-[:HAS_STAGED_PROPERTY]->(p:IngestionStagedProperty)
                    WITH e, collect({name: p.propertyName, val: p.valueJson}) AS props
                    WHERE ($keywords IS NULL OR size($keywords) = 0 OR any(kw IN $keywords WHERE
                        toLower(e.entityKey) CONTAINS toLower(kw) OR
                        toLower(e.className) CONTAINS toLower(kw) OR
                        any(p IN props WHERE toLower(coalesce(p.val, '')) CONTAINS toLower(kw))
                    ))
                    RETURN e.entityKey AS entity_key,
                           e.tempId AS temp_id,
                           e.className AS class_name,
                           props AS properties
                    LIMIT $limit
                    """,
                    ingestion_id=ingestion_id,
                    keywords=keywords or [],
                    limit=limit,
                ).data()
            )

            result = []
            for r in records:
                props_dict = {}
                for p in r["properties"]:
                    if p.get("name"):
                        try:
                            props_dict[p["name"]] = (
                                json.loads(p["val"]) if p.get("val") else None
                            )
                        except Exception:
                            props_dict[p["name"]] = p.get("val")
                result.append(
                    {
                        "entityKey": r["entity_key"],
                        "tempId": r.get("temp_id"),
                        "className": r["class_name"],
                        "properties": props_dict,
                    }
                )
            return result

    def get_staging_summary(self, ingestion_id: str) -> dict[str, Any]:
        """Aggregate summary of staged facts for an ingestion session."""
        driver = self.client.get_driver()
        with driver.session(database=self.client.database_name) as session:
            res = session.execute_read(
                lambda tx: tx.run(
                    """
                    OPTIONAL MATCH (e:IngestionStagedEntity {ingestionId: $ingestion_id})
                    WITH count(DISTINCT e) AS entityCount
                    OPTIONAL MATCH (p:IngestionStagedProperty {ingestionId: $ingestion_id})
                    WITH entityCount, count(DISTINCT p) AS propertyCount
                    OPTIONAL MATCH (eg:IngestionStagedEdge {ingestionId: $ingestion_id})
                    WITH entityCount, propertyCount, count(DISTINCT eg) AS edgeCount
                    OPTIONAL MATCH (pe:IngestionPendingEdge {ingestionId: $ingestion_id})
                    WITH entityCount, propertyCount, edgeCount, count(DISTINCT pe) AS pendingEdgeCount
                    OPTIONAL MATCH (cov:IngestionChunkCoverage {ingestionId: $ingestion_id})
                    WITH entityCount, propertyCount, edgeCount, pendingEdgeCount, count(DISTINCT cov) AS coverageCount
                    OPTIONAL MATCH (conf:IngestionConflict {ingestionId: $ingestion_id})
                    WITH entityCount, propertyCount, edgeCount, pendingEdgeCount, coverageCount, count(DISTINCT conf) AS conflictCount
                    OPTIONAL MATCH (bs:IngestionBatchState {ingestionId: $ingestion_id})
                    RETURN entityCount, propertyCount, edgeCount, pendingEdgeCount, coverageCount, conflictCount, count(DISTINCT bs) AS stagedBatchCount
                    """,
                    ingestion_id=ingestion_id,
                ).single()
            )
            if not res:
                return {
                    "entityCount": 0,
                    "propertyCount": 0,
                    "edgeCount": 0,
                    "pendingEdgeCount": 0,
                    "coverageCount": 0,
                    "conflictCount": 0,
                    "stagedBatchCount": 0,
                }
            return {
                "entityCount": res["entityCount"] or 0,
                "propertyCount": res["propertyCount"] or 0,
                "edgeCount": res["edgeCount"] or 0,
                "pendingEdgeCount": res["pendingEdgeCount"] or 0,
                "coverageCount": res["coverageCount"] or 0,
                "conflictCount": res["conflictCount"] or 0,
                "stagedBatchCount": res["stagedBatchCount"] or 0,
            }

    def get_staged_coverage_indexes(self, ingestion_id: str) -> list[int]:
        """Get list of distinct covered chunk indexes from persistent staging."""
        driver = self.client.get_driver()
        with driver.session(database=self.client.database_name) as session:
            records = session.execute_read(
                lambda tx: tx.run(
                    """
                    MATCH (c:IngestionChunkCoverage {ingestionId: $ingestion_id})
                    RETURN c.chunkIndex AS chunkIndex
                    """,
                    ingestion_id=ingestion_id,
                ).data()
            )
            return [r["chunkIndex"] for r in records if r["chunkIndex"] is not None]

    def get_issue_batch_indexes(self, ingestion_id: str) -> dict[str, list[int]]:
        """Get distinct batch indexes associated with pending edges and conflicts."""
        driver = self.client.get_driver()
        with driver.session(database=self.client.database_name) as session:
            res = session.execute_read(
                lambda tx: tx.run(
                    """
                    OPTIONAL MATCH (p:IngestionPendingEdge {ingestionId: $ingestion_id})
                    WITH collect(DISTINCT p.batchIndex) AS pendingBatches
                    OPTIONAL MATCH (c:IngestionConflict {ingestionId: $ingestion_id})
                    RETURN pendingBatches, collect(DISTINCT c.batchIndex) AS conflictBatches
                    """,
                    ingestion_id=ingestion_id,
                ).single()
            )
            if not res:
                return {"pendingEdges": [], "conflicts": []}
            return {
                "pendingEdges": [b for b in (res["pendingBatches"] or []) if b is not None],
                "conflicts": [b for b in (res["conflictBatches"] or []) if b is not None],
            }

    def validate_product_offer_has_offer(self, ingestion_id: str) -> list[dict[str, Any]]:
        """Validate BR-03 across the full staged canonical graph."""
        driver = self.client.get_driver()
        with driver.session(database=self.client.database_name) as session:
            records = session.execute_read(
                lambda tx: tx.run(
                    """
                    MATCH (offer:IngestionStagedEntity {ingestionId: $ingestion_id})
                    WHERE offer.className IN ['pskg:ProductOffer', 'ProductOffer']
                    OPTIONAL MATCH (edge:IngestionStagedEdge {
                        ingestionId: $ingestion_id,
                        edgeName: 'pskg:hasOffer',
                        targetEntityKey: offer.entityKey
                    })
                    OPTIONAL MATCH (product:IngestionStagedEntity {
                        ingestionId: $ingestion_id,
                        entityKey: edge.sourceEntityKey
                    })
                    WITH offer,
                         count(
                            CASE
                                WHEN product.className IN ['pskg:BankingProduct', 'BankingProduct']
                                THEN edge
                            END
                         ) AS incomingHasOfferCount
                    WHERE incomingHasOfferCount <> 1
                    RETURN offer.entityKey AS entityKey,
                           offer.tempId AS tempId,
                           offer.firstBatchIndex AS batchIndex,
                           incomingHasOfferCount
                    ORDER BY offer.entityKey
                    """,
                    ingestion_id=ingestion_id,
                ).data()
            )

        return [
            {
                "code": "ONTOLOGY_CARDINALITY_VIOLATION",
                "message": (
                    "ProductOffer must be linked from exactly one BankingProduct "
                    f"via pskg:hasOffer; found {record['incomingHasOfferCount']}"
                ),
                "location": "stagedGraph.ProductOffer.pskg:hasOffer",
                "nodeTempId": record.get("tempId"),
                "edgeName": "pskg:hasOffer",
                "entityKey": record["entityKey"],
                "batchIndex": record.get("batchIndex"),
                "actualCount": record["incomingHasOfferCount"],
                "expectedCount": 1,
            }
            for record in records
        ]

    def validate_edge_derived_property_relationships(
        self,
        ingestion_id: str,
        *,
        class_name: str,
        property_name: str,
        deriving_edge_names: list[str],
    ) -> list[dict[str, Any]]:
        """Validate staged nodes whose property semantics require an incoming edge."""
        if not deriving_edge_names:
            return []

        driver = self.client.get_driver()
        with driver.session(database=self.client.database_name) as session:
            records = session.execute_read(
                lambda tx: tx.run(
                    """
                    MATCH (n:IngestionStagedEntity {
                        ingestionId: $ingestion_id,
                        className: $class_name
                    })
                    OPTIONAL MATCH (edge:IngestionStagedEdge {
                        ingestionId: $ingestion_id,
                        targetEntityKey: n.entityKey
                    })
                    WHERE edge.edgeName IN $edge_names
                    WITH n, count(edge) AS derivingEdgeCount
                    WHERE derivingEdgeCount = 0
                    RETURN n.entityKey AS entityKey,
                           n.tempId AS tempId,
                           n.firstBatchIndex AS batchIndex
                    ORDER BY n.firstBatchIndex, n.entityKey
                    """,
                    ingestion_id=ingestion_id,
                    class_name=class_name,
                    edge_names=deriving_edge_names,
                ).data()
            )

        return [
            {
                "code": "DERIVED_PROPERTY_REQUIRES_EDGE_EVIDENCE",
                "message": (
                    f"{class_name} node requires an incoming relationship "
                    f"that derives {property_name}"
                ),
                "location": f"stagedGraph.{class_name}.{property_name}",
                "nodeTempId": record.get("tempId"),
                "entityKey": record["entityKey"],
                "propertyName": property_name,
                "batchIndex": record.get("batchIndex"),
            }
            for record in records
        ]

    @staticmethod
    def purge_staging_tx(tx, ingestion_id: str) -> None:
        """Detach delete all staged nodes for an ingestion session within an active transaction."""
        tx.run(
            """
            MATCH (n)
            WHERE n.ingestionId = $ingestion_id AND (
                n:IngestionStagedEntity OR
                n:IngestionStagedProperty OR
                n:IngestionStagedEdge OR
                n:IngestionStagedEvidence OR
                n:IngestionChunkCoverage OR
                n:IngestionPendingEdge OR
                n:IngestionConflict OR
                n:IngestionBatchState
            )
            DETACH DELETE n
            """,
            ingestion_id=ingestion_id,
        )

    def purge_staging(self, ingestion_id: str) -> None:
        """Detach delete all staged nodes for an ingestion session."""
        driver = self.client.get_driver()
        with driver.session(database=self.client.database_name) as session:
            session.execute_write(lambda tx: self.purge_staging_tx(tx, ingestion_id))

    def purge_all_staging(self) -> None:
        """Detach delete all staging nodes across all ingestion sessions."""
        driver = self.client.get_driver()
        with driver.session(database=self.client.database_name) as session:
            session.execute_write(
                lambda tx: tx.run(
                    """
                    MATCH (n)
                    WHERE (
                        n:IngestionStagedEntity OR
                        n:IngestionStagedProperty OR
                        n:IngestionStagedEdge OR
                        n:IngestionStagedEvidence OR
                        n:IngestionChunkCoverage OR
                        n:IngestionPendingEdge OR
                        n:IngestionConflict OR
                        n:IngestionBatchState
                    )
                    DETACH DELETE n
                    """
                )
            )
