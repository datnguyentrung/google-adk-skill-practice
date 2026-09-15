from app.tools.schema_tools import (
    load_business_rules_schema,
    load_product_catalog_schema,
)


def test_load_business_rules_schema_includes_neo4j_physical_mappings():
    res = load_business_rules_schema()
    assert res["success"] is True
    schema = res["schema"]

    # Check edges have relationshipType in UPPER_SNAKE_CASE
    edge_map = {e["technicalName"]: e for e in schema["edges"]}
    assert "pskg:hasEligibilityRule" in edge_map
    assert edge_map["pskg:hasEligibilityRule"]["relationshipType"] == "HAS_ELIGIBILITY_RULE"

    assert "pskg:hasSalesConditionRule" in edge_map
    assert edge_map["pskg:hasSalesConditionRule"]["relationshipType"] == "HAS_SALES_CONDITION_RULE"

    # Check classes have neo4jLabel
    class_map = {c["technicalName"]: c for c in schema["classes"]}
    assert "pskg:BusinessRule" in class_map
    assert class_map["pskg:BusinessRule"]["neo4jLabel"] == "BusinessRule"

    # Check attributes have neo4jPropertyKey
    attr_map = {a["technicalName"]: a for a in schema["attributes"]}
    assert "pskg:businessRuleCondition" in attr_map
    assert attr_map["pskg:businessRuleCondition"]["neo4jPropertyKey"] == "businessRuleCondition"


def test_load_product_catalog_schema_includes_neo4j_physical_mappings():
    res = load_product_catalog_schema()
    assert res["success"] is True
    schema = res["schema"]

    edge_map = {e["technicalName"]: e for e in schema["edges"]}
    assert "pskg:hasOffer" in edge_map
    assert edge_map["pskg:hasOffer"]["relationshipType"] == "HAS_OFFER"

    class_map = {c["technicalName"]: c for c in schema["classes"]}
    assert "pskg:BankingProduct" in class_map
    assert class_map["pskg:BankingProduct"]["neo4jLabel"] == "BankingProduct"

    attr_map = {a["technicalName"]: a for a in schema["attributes"]}
    assert "pskg:bankingProductName" in attr_map
    assert attr_map["pskg:bankingProductName"]["neo4jPropertyKey"] == "bankingProductName"
