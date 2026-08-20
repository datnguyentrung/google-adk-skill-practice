from typing import Any

from neo4j import Transaction

from app.services.ingestion.identity import (
    IdentityResolver,
)
from app.services.ingestion.neo4j_mapper import (
    Neo4jMapper,
)


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
    ) -> str:
        identity = self.identity_resolver.resolve(
            class_name=class_name,
            properties=properties,
        )

        if identity.strategy == "unresolved":
            raise Neo4jWriteError(
                f"Cannot safely upsert {class_name}: {identity.reason}"
            )

        label = self.mapper.class_to_label(class_name)

        neo4j_properties = self.mapper.properties_to_neo4j(properties)

        identity_property = self.mapper.property_to_key(identity.key_name)

        identity_value = identity.key_value

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
            raise Neo4jWriteError(f"Failed to upsert node: {class_name}")

        return record["node_id"]

    def upsert_edge(
        self,
        tx: Transaction,
        source_node_id: str,
        target_node_id: str,
        edge_name: str,
    ) -> None:
        relationship_type = self.mapper.edge_to_type(edge_name)

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
            raise Neo4jWriteError(f"Failed to upsert edge: {edge_name}")


def write_graph_patch(
    self,
    tx: Transaction,
    patch,
) -> dict[str, str]:
    node_ids: dict[str, str] = {}

    # 1. nodes trước
    for node in patch.nodes:
        node_id = self.upsert_node(
            tx=tx,
            class_name=node.class_name,
            properties=node.properties,
        )

        node_ids[node.temp_id] = node_id

    # 2. edges sau
    for edge in patch.edges:
        source_id = node_ids.get(edge.source_temp_id)

        target_id = node_ids.get(edge.target_temp_id)

        if source_id is None:
            raise Neo4jWriteError(f"Missing source node: {edge.source_temp_id}")

        if target_id is None:
            raise Neo4jWriteError(f"Missing target node: {edge.target_temp_id}")

        self.upsert_edge(
            tx=tx,
            source_node_id=source_id,
            target_node_id=target_id,
            edge_name=edge.edge_name,
        )

    return node_ids
