"""Tests for SchemaProjectionBuilder."""

from app.services.ingestion.schema.projection_builder import SchemaProjectionBuilder
from app.services.ingestion.schema.skill_registry import SchemaSkillRegistry


def test_schema_projection_builder_single_skill():
    registry = SchemaSkillRegistry()
    builder = SchemaProjectionBuilder()

    bundle = registry.get_bundle("business-rules")
    assert bundle is not None

    context = builder.build([bundle], registry.ontology_registry, reason=["Test reason"])
    assert context.selected_skill_ids == ["business-rules"]
    assert len(context.class_technical_names) > 0
    assert "CLASS " in context.projection_text
    assert context.selection_reason == ["Test reason"]


def test_schema_projection_builder_multiple_skills_deduplication():
    registry = SchemaSkillRegistry()
    builder = SchemaProjectionBuilder()

    bundles = registry.load(["campaign-targeting", "customer-recommendation"])
    assert len(bundles) == 2

    context = builder.build(bundles, registry.ontology_registry)
    # Class list should contain unique technical names
    assert len(context.class_technical_names) == len(set(context.class_technical_names))
    assert len(context.attr_technical_names) == len(set(context.attr_technical_names))
    assert len(context.edge_technical_names) == len(set(context.edge_technical_names))
