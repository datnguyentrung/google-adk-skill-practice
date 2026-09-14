"""Tests for AdkGraphMapper with SelectedSchemaContext and fallback behavior."""

from unittest.mock import MagicMock

from app.core.schemas.ingestion.document import DocumentChunk
from app.services.ingestion.mapping.graph_mapper import AdkGraphMapper
from app.services.ingestion.ontology.loader import OntologyLoader
from app.services.ingestion.ontology.registry import OntologyRegistry
from app.services.ingestion.patch.compiler import GraphPatchCompiler
from app.services.ingestion.schema import (
    SchemaProjectionBuilder,
    SchemaSkillRegistry,
    SelectedSchemaContext,
)
from app.services.ingestion.validation.ontology_validator import OntologyValidator


def test_mapper_with_schema_context(tmp_path):
    ontology = OntologyLoader.load("app/data/ontology/product_sales_knowledge_graph_base_v3_1.ontology.json")
    registry = OntologyRegistry(ontology)
    compiler = GraphPatchCompiler()
    validator = OntologyValidator(registry)

    executor = MagicMock()
    executor.run.return_value = {
        "nodes": [],
        "edges": [],
        "coverage": [{"chunkIndex": 0, "decision": "NOT_RELEVANT", "reason": "test"}],
    }

    mapper = AdkGraphMapper(
        registry=registry,
        compiler=compiler,
        ontology_validator=validator,
        structured_executor=executor,
    )

    skill_registry = SchemaSkillRegistry(registry)
    builder = SchemaProjectionBuilder()
    bundle = skill_registry.get_bundle("business-rules")
    schema_context = builder.build([bundle], registry)

    chunks = [DocumentChunk(index=0, content="Test chunk", source="test.txt")]
    batch_payload = {"batchIndex": 0}

    fragment = mapper.map_batch(
        batch_payload=batch_payload,
        chunks=chunks,
        schema_context=schema_context,
    )

    assert fragment is not None
    assert executor.run.called
    output_schema = executor.run.call_args.kwargs["output_schema"]
    class_enum = output_schema["$defs"]["ExtractedNode"]["properties"]["className"]["enum"]
    assert class_enum == schema_context.class_technical_names


def test_mapper_fallback_to_full_ontology():
    ontology = OntologyLoader.load("app/data/ontology/product_sales_knowledge_graph_base_v3_1.ontology.json")
    registry = OntologyRegistry(ontology)
    compiler = GraphPatchCompiler()
    validator = OntologyValidator(registry)

    executor = MagicMock()
    executor.run.return_value = {
        "nodes": [],
        "edges": [],
        "coverage": [{"chunkIndex": 0, "decision": "NOT_RELEVANT", "reason": "test"}],
    }

    mapper = AdkGraphMapper(
        registry=registry,
        compiler=compiler,
        ontology_validator=validator,
        structured_executor=executor,
    )

    chunks = [DocumentChunk(index=0, content="Test chunk", source="test.txt")]
    batch_payload = {"batchIndex": 0}

    mapper.map_batch(
        batch_payload=batch_payload,
        chunks=chunks,
        schema_context=None,
    )

    output_schema = executor.run.call_args.kwargs["output_schema"]
    class_enum = output_schema["$defs"]["ExtractedNode"]["properties"]["className"]["enum"]
    assert class_enum == registry.list_classes()


def test_mapper_with_previous_error_dict():
    ontology = OntologyLoader.load("app/data/ontology/product_sales_knowledge_graph_base_v3_1.ontology.json")
    registry = OntologyRegistry(ontology)
    compiler = GraphPatchCompiler()
    validator = OntologyValidator(registry)

    executor = MagicMock()
    executor.run.return_value = {
        "nodes": [],
        "edges": [],
        "coverage": [{"chunkIndex": 0, "decision": "NOT_RELEVANT", "reason": "test"}],
    }

    mapper = AdkGraphMapper(
        registry=registry,
        compiler=compiler,
        ontology_validator=validator,
        structured_executor=executor,
    )

    chunks = [DocumentChunk(index=0, content="Test chunk", source="test.txt")]
    batch_payload = {"batchIndex": 0}
    previous_error = {
        "stage": "direct_graph_mapping",
        "errors": [
            {
                "code": "DERIVED_PROPERTY_REQUIRES_EDGE_EVIDENCE",
                "location": "nodes.0.properties.pskg:ruleType",
                "message": "Validation failed",
            }
        ],
    }

    fragment = mapper.map_batch(
        batch_payload=batch_payload,
        chunks=chunks,
        schema_context=None,
        previous_error=previous_error,
    )

    assert fragment is not None
    prompt_used = executor.run.call_args.kwargs["instruction"]
    assert "PREVIOUS ATTEMPT FEEDBACK / BASELINE:" in prompt_used
    assert "DERIVED_PROPERTY_REQUIRES_EDGE_EVIDENCE" in prompt_used

