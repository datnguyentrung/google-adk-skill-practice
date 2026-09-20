"""Pending Edge Resolver.

Attempts to resolve pending edges in Neo4j staging as new entities are staged.
"""

import logging

from app.config.neo4j import Neo4jClient

logger = logging.getLogger(__name__)


def resolve_pending_edges(
    ingestion_id: str,
    client: Neo4jClient | None = None,
) -> int:
    """
    Search for pending edges whose endpoints now exist in IngestionStagedEntity and promote them.
    """
    client = client or Neo4jClient()
    driver = client.get_driver()
    with driver.session(database=client.database_name) as session:
        resolved_count = session.execute_write(
            lambda tx: tx.run(
                """
                MATCH (pe:IngestionPendingEdge {ingestionId: $ingestion_id})
                MATCH (src:IngestionStagedEntity {ingestionId: $ingestion_id, entityKey: pe.sourceEntityKey})
                MATCH (tgt:IngestionStagedEntity {ingestionId: $ingestion_id, entityKey: pe.targetEntityKey})

                MERGE (eg:IngestionStagedEdge {
                    ingestionId: $ingestion_id,
                    edgeKey: pe.edgeName + '|' + pe.sourceEntityKey + '|' + pe.targetEntityKey
                })
                ON CREATE SET
                    eg.sourceVersionId = pe.sourceVersionId,
                    eg.edgeName = pe.edgeName,
                    eg.sourceEntityKey = pe.sourceEntityKey,
                    eg.targetEntityKey = pe.targetEntityKey,
                    eg.confidence = 1.0,
                    eg.batchIndex = pe.batchIndex

                MERGE (src)-[:STAGED_EDGE {name: pe.edgeName}]->(tgt)
                MERGE (src)-[:HAS_STAGED_EDGE_RECORD]->(eg)

                DETACH DELETE pe
                RETURN count(eg) AS resolved
                """,
                ingestion_id=ingestion_id,
            ).single()
        )
        return resolved_count["resolved"] if resolved_count else 0
