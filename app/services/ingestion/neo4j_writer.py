import logging
from typing import Any

from neo4j import Transaction

from app.services.ingestion.identity import (
    IdentityResolver,
)
from app.services.ingestion.neo4j_mapper import (
    Neo4jMapper,
)

logger = logging.getLogger(__name__)


class Neo4jWriteError(RuntimeError):
    pass


class Neo4jWriter:
    def __init__(
        self,
        mapper: Neo4jMapper,
        identity_resolver: IdentityResolver,
    ):
        self.mapper = mapper
        self.identity_resolver = identity_resolver

    def upsert_node(
        self,
        tx: Transaction,
        class_name: str,
        properties: dict[str, Any],
        source_scope: str | None = None,
    ) -> str:
        logger.info(
            "Neo4j node upsert started class_name=%s property_count=%s has_source_scope=%s",
            class_name,
            len(properties),
            source_scope is not None,
        )
        identity = self.identity_resolver.resolve(
            class_name=class_name,
            properties=properties,
            source_scope=source_scope,
        )

        if identity.strategy == "unresolved":
            logger.warning(
                "Neo4j node upsert identity unresolved class_name=%s reason=%s",
                class_name,
                identity.reason,
            )
            raise Neo4jWriteError(
                f"Cannot safely upsert {class_name}: {identity.reason}"
            )

        label = self.mapper.class_to_label(class_name)

        neo4j_properties = self.mapper.properties_to_neo4j(properties)

        if identity.key_name is None or identity.key_value is None:
            logger.warning(
                "Neo4j node upsert identity incomplete class_name=%s strategy=%s",
                class_name,
                identity.strategy,
            )
            raise Neo4jWriteError(
                f"Resolved identity is incomplete for class {class_name}"
            )

        if identity.strategy == "source_scoped":
            identity_property = "_ingestionKey"
        else:
            identity_property = self.mapper.property_to_key(identity.key_name)

        identity_value = identity.key_value
        logger.info(
            "Neo4j node upsert mapped class_name=%s label=%s identity_strategy=%s identity_property=%s",
            class_name,
            label,
            identity.strategy,
            identity_property,
        )

        # MERGE and persisted properties must use the same canonical identity value.
        neo4j_properties[identity_property] = identity_value
        if source_scope:
            neo4j_properties["_ingestionSource"] = source_scope

        query = f"""
        MERGE (n:`{label}` {{
            `{identity_property}`: $identity_value
        }})
        SET n += $properties
        RETURN elementId(n) AS node_id
        """

        result = tx.run(
            query,
            identity_value=identity_value,
            properties=neo4j_properties,
        )

        record = result.single()

        if record is None:
            logger.warning("Neo4j node upsert returned no record class_name=%s", class_name)
            raise Neo4jWriteError(f"Failed to upsert node: {class_name}")

        node_id = record["node_id"]
        logger.info(
            "Neo4j node upsert completed class_name=%s label=%s node_id=%s",
            class_name,
            label,
            node_id,
        )

        return node_id

    def upsert_edge(
        self,
        tx: Transaction,
        source_node_id: str,
        target_node_id: str,
        edge_name: str,
    ) -> None:
        logger.info(
            "Neo4j edge upsert started edge_name=%s source_node_id=%s target_node_id=%s",
            edge_name,
            source_node_id,
            target_node_id,
        )
        relationship_type = self.mapper.edge_to_type(edge_name)
        logger.info(
            "Neo4j edge upsert mapped edge_name=%s relationship_type=%s",
            edge_name,
            relationship_type,
        )

        query = f"""
        MATCH (source)
        WHERE elementId(source) = $source_id

        MATCH (target)
        WHERE elementId(target) = $target_id

        MERGE (source)-[r:`{relationship_type}`]->(target)

        RETURN elementId(r) AS relationship_id
        """

        result = tx.run(
            query,
            source_id=source_node_id,
            target_id=target_node_id,
        )

        record = result.single()

        if record is None:
            logger.warning("Neo4j edge upsert returned no record edge_name=%s", edge_name)
            raise Neo4jWriteError(f"Failed to upsert edge: {edge_name}")

        logger.info(
            "Neo4j edge upsert completed edge_name=%s relationship_type=%s",
            edge_name,
            relationship_type,
        )

    @staticmethod
    def _source_scope_from_evidence(evidence) -> str | None:
        sources = sorted(
            {
                item.source.strip()
                for item in evidence
                if getattr(item, "source", None) and item.source.strip()
            }
        )
        if not sources:
            return None
        return "|".join(sources)

    def write_graph_patch(
        self,
        tx: Transaction,
        patch,
    ) -> dict[str, str]:
        logger.info(
            "Neo4j graph patch write started node_count=%s edge_count=%s",
            len(patch.nodes),
            len(patch.edges),
        )
        node_ids: dict[str, str] = {}

        # 1. Upsert nodes trước để resolve tempId -> Neo4j elementId.
        for node in patch.nodes:
            logger.info(
                "Neo4j graph patch writing node temp_id=%s class_name=%s",
                node.temp_id,
                node.class_name,
            )
            source_scope = self._source_scope_from_evidence(node.evidence)
            node_id = self.upsert_node(
                tx=tx,
                class_name=node.class_name,
                properties=node.properties,
                source_scope=source_scope,
            )
            node_ids[node.temp_id] = node_id
            logger.info(
                "Neo4j graph patch node written temp_id=%s node_id=%s",
                node.temp_id,
                node_id,
            )

        # 2. Chỉ tạo edge sau khi toàn bộ node đã được resolve.
        for edge in patch.edges:
            logger.info(
                "Neo4j graph patch writing edge edge_name=%s source_temp_id=%s target_temp_id=%s",
                edge.edge_name,
                edge.source_temp_id,
                edge.target_temp_id,
            )
            source_id = node_ids.get(edge.source_temp_id)
            target_id = node_ids.get(edge.target_temp_id)

            if source_id is None:
                logger.warning(
                    "Neo4j graph patch missing source node source_temp_id=%s edge_name=%s",
                    edge.source_temp_id,
                    edge.edge_name,
                )
                raise Neo4jWriteError(
                    f"Missing source node: {edge.source_temp_id}"
                )

            if target_id is None:
                logger.warning(
                    "Neo4j graph patch missing target node target_temp_id=%s edge_name=%s",
                    edge.target_temp_id,
                    edge.edge_name,
                )
                raise Neo4jWriteError(
                    f"Missing target node: {edge.target_temp_id}"
                )

            self.upsert_edge(
                tx=tx,
                source_node_id=source_id,
                target_node_id=target_id,
                edge_name=edge.edge_name,
            )
            logger.info(
                "Neo4j graph patch edge written edge_name=%s source_temp_id=%s target_temp_id=%s",
                edge.edge_name,
                edge.source_temp_id,
                edge.target_temp_id,
            )

        logger.info(
            "Neo4j graph patch write completed node_id_count=%s edge_count=%s",
            len(node_ids),
            len(patch.edges),
        )
        return node_ids
