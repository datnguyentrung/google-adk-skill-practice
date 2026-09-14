"""Tests for SchemaSkillRegistry."""

from app.services.ingestion.schema.skill_registry import (
    SchemaSkillBundle,
    SchemaSkillRegistry,
    get_schema_skill_registry,
)


def test_schema_skill_registry_initialization():
    registry = SchemaSkillRegistry()
    skills = registry.list_skills()
    assert "business-rules" in skills
    assert "product-catalog" in skills
    assert "governance-versioning" in skills
    assert "campaign-targeting" in skills
    assert "customer-recommendation" in skills
    assert "sales-enablement" in skills


def test_schema_skill_registry_load_bundle():
    registry = SchemaSkillRegistry()
    bundle = registry.get_bundle("business-rules")
    assert bundle is not None
    assert bundle.skill_id == "business-rules"
    assert len(bundle.class_technical_names) > 0
    assert "pskg:BusinessRule" in bundle.class_technical_names or "pskg:RequiredDocument" in bundle.class_technical_names


def test_schema_skill_registry_load_multiple():
    registry = SchemaSkillRegistry()
    bundles = registry.load(["business-rules", "product-catalog"])
    assert len(bundles) == 2
    skill_ids = [b.skill_id for b in bundles]
    assert "business-rules" in skill_ids
    assert "product-catalog" in skill_ids


def test_singleton_getter():
    reg1 = get_schema_skill_registry()
    reg2 = get_schema_skill_registry()
    assert reg1 is reg2
