import pytest

from app.core.schemas.ingestion.graph_patch import GraphPatch
from app.services.ingestion.identity import (
    create_product_sales_identity_resolver,
)
from app.services.ingestion.loader import OntologyLoader
from app.services.ingestion.neo4j_mapper import Neo4jMapper
from app.services.ingestion.neo4j_writer import Neo4jWriteError, Neo4jWriter
from app.services.ingestion.registry import OntologyRegistry

ONTOLOGY_PATH = (
    "app/data/ontology/product_sales_knowledge_graph_base_v3_1.ontology.json"
)


class FakeResult:
    def __init__(self, record):
        self.record = record

    def single(self):
        return self.record


class FakeTransaction:
    def __init__(self):
        self.calls = []

    def run(self, query, **parameters):
        self.calls.append((query, parameters))
        return FakeResult({"node_id": "node-1", "relationship_id": "rel-1"})


def create_writer() -> Neo4jWriter:
    ontology = OntologyLoader.load(ONTOLOGY_PATH)
    registry = OntologyRegistry(ontology)
    return Neo4jWriter(
        mapper=Neo4jMapper(registry),
        identity_resolver=create_product_sales_identity_resolver(registry),
    )


def test_upsert_node_uses_normalized_identity_for_merge_and_persisted_property():
    writer = create_writer()
    tx = FakeTransaction()

    node_id = writer.upsert_node(
        tx=tx,
        class_name="pskg:BankingProduct",
        properties={
            "pskg:productCode": "  CARD-001  ",
            "pskg:bankingProductStatus": "Published",
        },
    )

    assert node_id == "node-1"
    _, parameters = tx.calls[0]
    assert parameters["identity_value"] == "CARD-001"
    assert parameters["properties"]["productCode"] == "CARD-001"


def test_upsert_node_rejects_unresolved_identity():
    writer = create_writer()
    tx = FakeTransaction()

    with pytest.raises(Neo4jWriteError, match="Cannot safely upsert"):
        writer.upsert_node(
            tx=tx,
            class_name="pskg:BusinessRule",
            properties={
                "pskg:ruleType": "ELIGIBILITY",
                "pskg:businessRuleStatus": "Published",
            },
        )

    assert tx.calls == []


def test_write_graph_patch_upserts_nodes_then_edges():
    writer = create_writer()
    tx = FakeTransaction()
    patch = GraphPatch.model_validate(
        {
            "nodes": [
                {
                    "tempId": "product-1",
                    "className": "pskg:BankingProduct",
                    "properties": {
                        "pskg:productCode": "CARD-001",
                        "pskg:bankingProductStatus": "Published",
                    },
                    "evidence": [],
                    "confidence": 1.0,
                },
                {
                    "tempId": "need-1",
                    "className": "pskg:CustomerNeed",
                    "properties": {
                        "pskg:needCode": "NEED-001",
                        "pskg:needName": "Flexible rewards",
                    },
                    "evidence": [],
                    "confidence": 0.9,
                },
            ],
            "edges": [
                {
                    "edgeName": "pskg:satisfiesNeed",
                    "sourceTempId": "product-1",
                    "targetTempId": "need-1",
                    "evidence": [],
                    "confidence": 0.9,
                }
            ],
            "warnings": [],
        }
    )

    node_ids = writer.write_graph_patch(tx=tx, patch=patch)

    assert node_ids == {
        "product-1": "node-1",
        "need-1": "node-1",
    }
    assert len(tx.calls) == 3
    assert "MERGE (n:`BankingProduct`" in tx.calls[0][0]
    assert "MERGE (n:`CustomerNeed`" in tx.calls[1][0]
    assert "MERGE (source)-[r:`SATISFIES_NEED`]->(target)" in tx.calls[2][0]


def test_upsert_business_rule_uses_source_scoped_identity():
    writer = create_writer()
    tx = FakeTransaction()

    node_id = writer.upsert_node(
        tx=tx,
        class_name="pskg:BusinessRule",
        properties={
            "pskg:ruleType": "ELIGIBILITY",
            "pskg:businessRuleCondition": "Age >= 20",
            "pskg:businessRuleStatus": "Published",
        },
        source_scope="product.md",
    )

    assert node_id == "node-1"
    query, parameters = tx.calls[0]
    assert "`_ingestionKey`" in query
    assert len(parameters["identity_value"]) == 64
    assert parameters["properties"]["_ingestionSource"] == "product.md"
    assert parameters["properties"]["_ingestionKey"] == parameters["identity_value"]
