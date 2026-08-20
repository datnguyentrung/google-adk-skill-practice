from app.services.ingestion.loader import OntologyLoader
from app.services.ingestion.neo4j_mapper import (
    Neo4jMapper,
)
from app.services.ingestion.registry import OntologyRegistry

ONTOLOGY_PATH = (
    "app/data/ontology/product_sales_knowledge_graph_base_v3_1.ontology.json"
)


def create_mapper():
    ontology = OntologyLoader.load(ONTOLOGY_PATH)

    registry = OntologyRegistry(ontology)

    return Neo4jMapper(registry)


def test_class_to_label():
    mapper = create_mapper()

    assert mapper.class_to_label("pskg:BankingProduct") == "BankingProduct"


def test_property_to_key():
    mapper = create_mapper()

    assert mapper.property_to_key("pskg:productCode") == "productCode"


def test_edge_to_type():
    mapper = create_mapper()

    assert mapper.edge_to_type("pskg:hasEligibilityRule") == "HAS_ELIGIBILITY_RULE"


def test_properties_to_neo4j():
    mapper = create_mapper()

    result = mapper.properties_to_neo4j(
        {
            "pskg:productCode": "DEP-001",
            "pskg:bankingProductName": ("Tiền gửi Online"),
        }
    )

    assert result == {
        "productCode": "DEP-001",
        "bankingProductName": ("Tiền gửi Online"),
    }
