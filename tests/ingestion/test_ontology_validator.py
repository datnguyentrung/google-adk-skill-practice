from app.services.ingestion.loader import OntologyLoader
from app.services.ingestion.registry import OntologyRegistry
from app.services.ingestion.validator import OntologyValidator

ONTOLOGY_PATH = (
    "app/data/ontology/product_sales_knowledge_graph_base_v3_1.ontology.json"
)


def create_validator() -> OntologyValidator:
    ontology = OntologyLoader.load(ONTOLOGY_PATH)
    registry = OntologyRegistry(ontology)

    return OntologyValidator(registry)


def test_valid_banking_product_node():
    validator = create_validator()

    errors = validator.validate_node(
        class_name="pskg:BankingProduct",
        properties={
            "pskg:productCode": "CARD-001",
            "pskg:bankingProductName": "Flexi Rewards",
            "pskg:bankingProductStatus": "Published",
            "pskg:bankingProductEffectiveFrom": "2026-08-01",
        },
    )

    assert errors == []


def test_unknown_class():
    validator = create_validator()

    errors = validator.validate_node(
        class_name="pskg:SomethingDoesNotExist",
        properties={},
    )

    assert len(errors) == 1
    assert "Unknown ontology class" in errors[0]


def test_unknown_property():
    validator = create_validator()

    errors = validator.validate_node(
        class_name="pskg:BankingProduct",
        properties={
            "pskg:notRealProperty": "hello",
        },
    )

    assert any("Unknown ontology property" in error for error in errors)


def test_property_does_not_belong_to_class():
    validator = create_validator()

    errors = validator.validate_node(
        class_name="pskg:BankingProduct",
        properties={
            "pskg:priority": 10,
        },
    )

    assert any("does not belong" in error for error in errors)


def test_invalid_property_datatype():
    validator = create_validator()

    errors = validator.validate_node(
        class_name="pskg:BankingProduct",
        properties={
            "pskg:productCode": 12345,
        },
    )

    assert any("Invalid datatype" in error for error in errors)

def test_missing_required_product_code():
    validator = create_validator()

    errors = validator.validate_node(
        class_name="pskg:BankingProduct",
        properties={
            "pskg:bankingProductStatus": "Published",
        },
    )

    assert "Missing required property: pskg:productCode" in errors

def test_missing_required_product_status():
    validator = create_validator()

    errors = validator.validate_node(
        class_name="pskg:BankingProduct",
        properties={
            "pskg:productCode": "CARD-001",
        },
    )

    assert (
        "Missing required property: "
        "pskg:bankingProductStatus"
    ) in errors

def test_missing_multiple_required_properties():
    validator = create_validator()

    errors = validator.validate_node(
        class_name="pskg:BankingProduct",
        properties={},
    )

    assert (
        "Missing required property: pskg:productCode"
        in errors
    )

    assert (
        "Missing required property: "
        "pskg:bankingProductStatus"
        in errors
    )
