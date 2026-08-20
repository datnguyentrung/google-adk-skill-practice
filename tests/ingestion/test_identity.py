from app.services.ingestion.identity import (
    create_product_sales_identity_resolver,
)
from app.services.ingestion.loader import OntologyLoader
from app.services.ingestion.registry import OntologyRegistry

ONTOLOGY_PATH = (
    "app/data/ontology/product_sales_knowledge_graph_base_v3_1.ontology.json"
)


def create_resolver():
    ontology = OntologyLoader.load(ONTOLOGY_PATH)
    return create_product_sales_identity_resolver(
        OntologyRegistry(ontology)
    )


def test_source_scoped_identity_is_deterministic():
    resolver = create_resolver()
    properties = {
        "pskg:ruleType": "ELIGIBILITY",
        "pskg:businessRuleCondition": "Age >= 20",
        "pskg:businessRuleStatus": "Published",
    }

    first = resolver.resolve(
        class_name="pskg:BusinessRule",
        properties=properties,
        source_scope="product.md",
    )
    second = resolver.resolve(
        class_name="pskg:BusinessRule",
        properties=dict(reversed(list(properties.items()))),
        source_scope=" product.md ",
    )

    assert first.strategy == "source_scoped"
    assert first.key_name == "_ingestionKey"
    assert first.key_value == second.key_value
    assert first.key_value is not None
    assert len(first.key_value) == 64


def test_natural_key_still_takes_precedence():
    resolver = create_resolver()
    identity = resolver.resolve(
        class_name="pskg:BankingProduct",
        properties={"pskg:productCode": "  CARD-001  "},
        source_scope="product.md",
    )

    assert identity.strategy == "natural_key"
    assert identity.key_value == "CARD-001"
