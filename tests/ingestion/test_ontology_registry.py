from app.services.ingestion.loader import OntologyLoader
from app.services.ingestion.registry import OntologyRegistry

ONTOLOGY_PATH = (
    "app/data/ontology/product_sales_knowledge_graph_base_v3_1.ontology.json"
)


def test_registry():
    ontology = OntologyLoader.load(ONTOLOGY_PATH)
    registry = OntologyRegistry(ontology)

    banking_product = registry.get_class("pskg:BankingProduct")
    has_offer = registry.get_edge("pskg:hasOffer")
    product_code = registry.get_attribute("pskg:productCode")

    assert banking_product is not None
    assert has_offer is not None
    assert product_code is not None


def test_properties_for_banking_product():
    ontology = OntologyLoader.load(ONTOLOGY_PATH)
    registry = OntologyRegistry(ontology)

    properties = registry.properties_from_class("pskg:BankingProduct")

    technical_names = {item.technical_name for item in properties}

    assert "pskg:productCode" in technical_names
    assert "pskg:bankingProductName" in technical_names
    assert "pskg:bankingProductStatus" in technical_names


def test_edges_from_banking_product():
    ontology = OntologyLoader.load(ONTOLOGY_PATH)
    registry = OntologyRegistry(ontology)

    edges = registry.edges_from_class("pskg:BankingProduct")

    technical_names = {item.technical_name for item in edges}

    assert "pskg:hasOffer" in technical_names
    assert "pskg:targetsSegment" in technical_names
    assert "pskg:satisfiesNeed" in technical_names
    assert "pskg:hasEligibilityRule" in technical_names


def test_unknown_class_returns_empty_lists():
    ontology = OntologyLoader.load(ONTOLOGY_PATH)
    registry = OntologyRegistry(ontology)

    assert registry.properties_from_class("pskg:NotExist") == []

    assert registry.edges_from_class("pskg:NotExist") == []
