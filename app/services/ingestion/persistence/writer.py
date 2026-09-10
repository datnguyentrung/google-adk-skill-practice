"""Phase 5 — Ghi node/edge xuống Neo4j và đọc lại theo element ID.

Đây là lớp chạm trực tiếp vào Neo4j: upsert node theo identity, upsert relationship
theo khoá ổn định, ghi cả patch trong một transaction, rồi đọc lại đúng các element ID
vừa commit để trả về cho bước xác minh."""

import logging
from typing import Any

from neo4j import Transaction

from app.core.schemas.ingestion.persistence import (
    GraphWriteResult,
    PersistedGraphReadback,
    PersistedNode,
    PersistedRelationship,
)
from app.services.ingestion.identity.resolver import (
    IdentityResolver,
    source_scope_from_evidence,
)
from app.services.ingestion.identity.semantic_resolution import SemanticEntityResolver
from app.services.ingestion.persistence.mapping import Neo4jMapper
from app.services.ingestion.persistence.readback import relationship_key

logger = logging.getLogger(__name__)


class Neo4jWriteError(RuntimeError):
    """
    Lỗi khi ghi dữ liệu xuống Neo4j thất bại.
    """


class Neo4jGraphStore:
    """
    Persist graph patches and read back exactly the committed element IDs.
    """
    def __init__(
        self,
        mapper: Neo4jMapper,
        identity_resolver: IdentityResolver,
        semantic_resolver: SemanticEntityResolver | None = None,
    ):
        """
        Khởi tạo store với mapper và identity resolver.

        Args:
            mapper: Mapper đổi tên ontology sang định danh Neo4j.
            identity_resolver: Bộ resolve identity cho node.
        """
        self.mapper = mapper
        self.identity_resolver = identity_resolver
        self.semantic_resolver = semantic_resolver

    def upsert_node(
        self,
        tx: Transaction,
        class_name: str,
        properties: dict[str, Any],
        source_scope: str | None = None,
    ) -> str:
        # Public compatibility wrapper; write_graph_patch also needs the snapshot.
        node_id, _ = self._upsert_node_with_snapshot(
            tx=tx,
            class_name=class_name,
            properties=properties,
            source_scope=source_scope,
        )
        return node_id

    def _upsert_node_with_snapshot(
        self,
        *,
        tx: Transaction,
        class_name: str,
        properties: dict[str, Any],
        source_scope: str | None,
    ) -> tuple[str, dict[str, Any]]:
        identity = self.identity_resolver.resolve(
            class_name=class_name,
            properties=properties,
            source_scope=source_scope,
        )
        label = self.mapper.class_to_label(class_name)
        neo4j_properties = self.mapper.properties_to_neo4j(properties)
        if source_scope:
            neo4j_properties['_ingestionSource'] = source_scope

        if identity.strategy != 'natural_key' and self.semantic_resolver is not None:
            try:
                candidate = self.semantic_resolver.resolve(
                    tx,
                    class_name=class_name,
                    properties=properties,
                    source_scope=source_scope,
                    mapper=self.mapper,
                )
            except Exception as exc:  # noqa: BLE001 - semantic fallback must fail open
                logger.warning(
                    'Semantic identity resolution failed class_name=%s error=%s',
                    class_name,
                    exc,
                )
                candidate = None
            if candidate is not None:
                record = tx.run(
                    '''MATCH (n) WHERE elementId(n) = $node_id
                       SET n += $properties
                       RETURN elementId(n) AS node_id, properties(n) AS properties''',
                    node_id=candidate.node_id,
                    properties=neo4j_properties,
                ).single()
                if record is None:
                    raise Neo4jWriteError(f'Failed semantic upsert for {class_name}')
                return str(record['node_id']), dict(record['properties'])

        if identity.strategy == 'unresolved':
            raise Neo4jWriteError(
                f'Cannot safely upsert {class_name}: {identity.reason}'
            )
        if identity.key_name is None or identity.key_value is None:
            raise Neo4jWriteError(
                f'Resolved identity is incomplete for class {class_name}'
            )
        identity_property = (
            '_ingestionKey'
            if identity.strategy == 'source_scoped'
            else self.mapper.property_to_key(identity.key_name)
        )
        neo4j_properties[identity_property] = identity.key_value
        merge_query = f'''MERGE (n:`{label}` {{`{identity_property}`: $identity_value}})
        SET n += $properties
        RETURN elementId(n) AS node_id, properties(n) AS properties'''
        record = tx.run(
            merge_query,
            identity_value=identity.key_value,
            properties=neo4j_properties,
        ).single()
        if record is None:
            raise Neo4jWriteError(f'Failed to upsert node: {class_name}')
        return str(record['node_id']), dict(record['properties'])

    def upsert_edge(
        self,
        tx: Transaction,
        source_node_id: str,
        target_node_id: str,
        edge_name: str,
    ) -> str:
        """
        MERGE một relationship giữa hai node đã ghi.

        Returns:
            Element ID của relationship sau khi ghi.
        """
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

        relationship_id = record["relationship_id"]

        logger.info(
            "Neo4j edge upsert completed edge_name=%s relationship_type=%s",
            edge_name,
            relationship_type,
        )
        return relationship_id

    def write_graph_patch(
        self,
        tx: Transaction,
        patch,
    ) -> GraphWriteResult:
        """
        Ghi toàn bộ patch trong một transaction và trả về kết quả kèm element ID.

        Args:
            patch: Patch đã compile.
            artifact_content_digest: Digest artifact nguồn (nếu có).

        Returns:
            `GraphWriteResult` gồm ID các node/relationship đã commit.
        """
        logger.info(
            "Neo4j graph patch write started node_count=%s edge_count=%s",
            len(patch.nodes),
            len(patch.edges),
        )
        node_ids: dict[str, str] = {}
        relationship_ids: dict[str, str] = {}
        expected_nodes: dict[str, PersistedNode] = {}
        expected_relationships: dict[str, PersistedRelationship] = {}

        # 1. Upsert nodes trước để resolve tempId -> Neo4j elementId.
        for node in patch.nodes:
            logger.info(
                "Neo4j graph patch writing node temp_id=%s class_name=%s",
                node.temp_id,
                node.class_name,
            )
            source_scope = source_scope_from_evidence(node.evidence)
            node_id, persisted_properties = self._upsert_node_with_snapshot(
                tx=tx,
                class_name=node.class_name,
                properties=node.properties,
                source_scope=source_scope,
            )
            node_ids[node.temp_id] = node_id
            expected_nodes[node.temp_id] = PersistedNode(
                nodeId=node_id,
                labels=[self.mapper.class_to_label(node.class_name)],
                properties=persisted_properties,
            )
            logger.info(
                "Neo4j graph patch node written temp_id=%s node_id=%s",
                node.temp_id,
                node_id,
            )

        # 2. Chỉ tạo edge sau khi toàn bộ node đã được resolve.
        for edge_index, edge in enumerate(patch.edges):
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

            relationship_id = self.upsert_edge(
                tx=tx,
                source_node_id=source_id,
                target_node_id=target_id,
                edge_name=edge.edge_name,
            )
            key = relationship_key(edge_index, edge)
            relationship_ids[key] = relationship_id
            expected_relationships[key] = PersistedRelationship(
                relationshipId=relationship_id,
                type=self.mapper.edge_to_type(edge.edge_name),
                sourceNodeId=source_id,
                targetNodeId=target_id,
                properties={},
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
        return GraphWriteResult(
            nodeIds=node_ids,
            relationshipIds=relationship_ids,
            expectedNodes=expected_nodes,
            expectedRelationships=expected_relationships,
        )

    def _expected_node(
        self,
        *,
        node_id: str,
        class_name: str,
        properties: dict[str, Any],
        source_scope: str | None,
    ) -> PersistedNode:
        """
        Dựng dữ liệu mong đợi của một node (label, thuộc tính) để đối chiếu khi ghi/đọc lại.
        """
        identity = self.identity_resolver.resolve(
            class_name=class_name,
            properties=properties,
            source_scope=source_scope,
        )
        if identity.key_name is None or identity.key_value is None:
            raise Neo4jWriteError(f"Resolved identity is incomplete for {class_name}")
        identity_property = (
            "_ingestionKey"
            if identity.strategy == "source_scoped"
            else self.mapper.property_to_key(identity.key_name)
        )
        expected_properties = self.mapper.properties_to_neo4j(properties)
        expected_properties[identity_property] = identity.key_value
        if source_scope:
            expected_properties["_ingestionSource"] = source_scope
        return PersistedNode(
            nodeId=node_id,
            labels=[self.mapper.class_to_label(class_name)],
            properties=expected_properties,
        )

    @staticmethod
    def read_graph_patch(
        tx: Transaction,
        write_result: GraphWriteResult,
    ) -> PersistedGraphReadback:
        """
        Đọc lại đúng các element ID đã commit để phục vụ bước verify.

        Returns:
            `PersistedGraphReadback` gồm node và relationship vừa ghi.
        """
        node_result = tx.run(
            """
            MATCH (n)
            WHERE elementId(n) IN $node_ids
            RETURN elementId(n) AS node_id,
                   labels(n) AS labels,
                   properties(n) AS properties
            """,
            node_ids=list(dict.fromkeys(write_result.node_ids.values())),
        )
        nodes = [
            {
                "nodeId": record["node_id"],
                "labels": list(record["labels"]),
                "properties": dict(record["properties"]),
            }
            for record in node_result
        ]
        relationship_result = tx.run(
            """
            MATCH (source)-[r]->(target)
            WHERE elementId(r) IN $relationship_ids
            RETURN elementId(r) AS relationship_id,
                   type(r) AS relationship_type,
                   elementId(source) AS source_node_id,
                   elementId(target) AS target_node_id,
                   properties(r) AS properties
            """,
            relationship_ids=list(
                dict.fromkeys(write_result.relationship_ids.values())
            ),
        )
        relationships = [
            {
                "relationshipId": record["relationship_id"],
                "type": record["relationship_type"],
                "sourceNodeId": record["source_node_id"],
                "targetNodeId": record["target_node_id"],
                "properties": dict(record["properties"]),
            }
            for record in relationship_result
        ]
        return PersistedGraphReadback(
            nodes=nodes,
            relationships=relationships,
        )


Neo4jWriter = Neo4jGraphStore
