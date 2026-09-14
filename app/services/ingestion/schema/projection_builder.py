"""Schema Projection Builder — Constructing filtered ontology projections for LLM context."""

from dataclasses import dataclass, field
from typing import Any

from app.services.ingestion.mapping.temp_ids import _short_definition
from app.services.ingestion.ontology.registry import OntologyRegistry
from app.services.ingestion.schema.skill_registry import SchemaSkillBundle


@dataclass(frozen=True)
class SelectedSchemaContext:
    """Projection context và schema enums đã lọc cho LLM extraction."""

    selected_skill_ids: list[str] = field(default_factory=list)
    class_technical_names: list[str] = field(default_factory=list)
    attr_technical_names: list[str] = field(default_factory=list)
    edge_technical_names: list[str] = field(default_factory=list)
    projection_text: str = ""
    selection_reason: list[str] = field(default_factory=list)


class SchemaProjectionBuilder:
    """Build SelectedSchemaContext từ danh sách SchemaSkillBundle."""

    def build(
        self,
        bundles: list[SchemaSkillBundle],
        ontology_registry: OntologyRegistry,
        reason: list[str] | None = None,
    ) -> SelectedSchemaContext:
        """Merge nhiều SchemaSkillBundle và dựng projection text ngắn gọn cho LLM context."""
        selected_skills = [b.skill_id for b in bundles]
        reasons = reason or []

        # Deduplicate while preserving order
        classes_set: list[str] = []
        attrs_set: list[str] = []
        edges_set: list[str] = []

        for b in bundles:
            for cls in b.class_technical_names:
                if cls not in classes_set and ontology_registry.has_class(cls):
                    classes_set.append(cls)
            for attr in b.attr_technical_names:
                if attr not in attrs_set and ontology_registry.has_attribute(attr):
                    attrs_set.append(attr)
            for edge in b.edge_technical_names:
                if edge not in edges_set and ontology_registry.has_edge(edge):
                    edges_set.append(edge)

        projection_text = self._build_projection_text(
            classes_set, ontology_registry
        )

        return SelectedSchemaContext(
            selected_skill_ids=selected_skills,
            class_technical_names=classes_set,
            attr_technical_names=attrs_set,
            edge_technical_names=edges_set,
            projection_text=projection_text,
            selection_reason=reasons,
        )

    @staticmethod
    def _build_projection_text(
        class_technical_names: list[str],
        registry: OntologyRegistry,
    ) -> str:
        """Build projection string cho các class được chọn."""
        lines: list[str] = []
        for class_name in class_technical_names:
            cls = registry.get_class(class_name)
            if cls is None:
                continue

            required = []
            configured_defaults = {
                attribute.technical_name
                for attribute, _ in registry.configured_defaults_for_class(
                    class_name
                )
            }
            for rule in cls.rules:
                is_required = rule.operator in {"some", "exactlyQualified"}
                if rule.operator == "minQualified":
                    try:
                        is_required = int(rule.value) > 0
                    except (TypeError, ValueError):
                        is_required = False
                if not is_required:
                    continue
                qualifier = f" | qualifier={rule.qualifier}" if rule.qualifier else ""
                attribute = registry.get_attribute(rule.property)
                if attribute is not None:
                    if (
                        registry.is_runtime_managed_attribute(rule.property)
                        or rule.property in configured_defaults
                        or attribute.ingestion_policy.mode
                        in {"runtime_managed", "system_default"}
                    ):
                        requirement_kind = "SYSTEM"
                    elif (
                        attribute.ingestion_policy.mode == "edge_derived"
                        or registry.edge_names_deriving_property(rule.property)
                    ):
                        requirement_kind = "DERIVED"
                    else:
                        requirement_kind = "SOURCE"
                elif registry.get_edge(rule.property) is not None:
                    requirement_kind = "EDGE"
                else:
                    requirement_kind = "OTHER"
                required.append(
                    f"  REQUIRED_{requirement_kind} {rule.property} | "
                    f"{rule.operator}={rule.value}{qualifier}"
                )

            properties = []
            for attr in registry.properties_from_class(class_name):
                if (
                    attr.ingestion_policy.mode != "source"
                    or registry.is_runtime_managed_attribute(attr.technical_name)
                    or registry.edge_names_deriving_property(attr.technical_name)
                ):
                    continue
                properties.append(
                    "  - "
                    f"{attr.technical_name} | range={','.join(attr.range)} "
                    f"| grounding={attr.ingestion_policy.grounding} "
                    f"| {attr.label} | {_short_definition(attr.definition)}"
                )

            if required or properties:
                lines.append(f"CLASS {cls.technical_name}")
                lines.extend(required)
                lines.extend(properties)

        return "\n".join(lines)
