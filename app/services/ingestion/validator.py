from datetime import date, datetime
from decimal import Decimal
from typing import Any

from app.core.schemas.ingestion.validation import ValidationIssue
from app.services.ingestion.graph_patch_compiler import (
    RULE_TYPE_BY_EDGE,
    CompiledGraphPatch,
)
from app.services.ingestion.registry import OntologyRegistry


class OntologyValidator:
    """Validate emitted facts separately from persistence completeness."""

    def __init__(self, registry: OntologyRegistry):
        self.registry = registry

    def validate_extraction(
        self,
        patch: CompiledGraphPatch,
    ) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        node_by_temp_id = {node.temp_id: node for node in patch.nodes}

        for node_index, node in enumerate(patch.nodes):
            ontology_class = self.registry.get_class(node.class_name)
            if ontology_class is None:
                issues.append(
                    ValidationIssue(
                        code="UNKNOWN_CLASS",
                        message=f"Unknown ontology class: {node.class_name}",
                        location=f"nodes.{node_index}.className",
                        node_temp_id=node.temp_id,
                    )
                )
                continue

            for property_name, value in node.properties.items():
                attribute = self.registry.get_attribute(property_name)
                location = f"nodes.{node_index}.properties.{property_name}"
                if attribute is None:
                    issues.append(
                        ValidationIssue(
                            code="UNKNOWN_PROPERTY",
                            message=f"Unknown ontology property: {property_name}",
                            location=location,
                            node_temp_id=node.temp_id,
                            property_name=property_name,
                        )
                    )
                    continue
                if ontology_class.name not in attribute.domain:
                    issues.append(
                        ValidationIssue(
                            code="PROPERTY_DOMAIN_MISMATCH",
                            message=(
                                f"Property {property_name} does not belong to "
                                f"class {node.class_name}"
                            ),
                            location=location,
                            node_temp_id=node.temp_id,
                            property_name=property_name,
                        )
                    )
                    continue
                if not self._is_valid_property_value(value, attribute.range):
                    issues.append(
                        ValidationIssue(
                            code="PROPERTY_DATATYPE_MISMATCH",
                            message=(
                                f"Invalid datatype for {property_name}; expected "
                                f"{attribute.range}, got {type(value).__name__}"
                            ),
                            location=location,
                            node_temp_id=node.temp_id,
                            property_name=property_name,
                        )
                    )

        for edge_index, edge in enumerate(patch.edges):
            source = node_by_temp_id.get(edge.source_temp_id)
            target = node_by_temp_id.get(edge.target_temp_id)
            if source is None or target is None:
                issues.append(
                    ValidationIssue(
                        code="DANGLING_REFERENCE",
                        message=f"Edge {edge.edge_name} references an unknown tempId",
                        location=f"edges.{edge_index}",
                        edge_name=edge.edge_name,
                    )
                )
                continue

            ontology_edge = self.registry.get_edge(edge.edge_name)
            if ontology_edge is None:
                issues.append(
                    ValidationIssue(
                        code="UNKNOWN_EDGE",
                        message=f"Unknown ontology edge: {edge.edge_name}",
                        location=f"edges.{edge_index}.edgeName",
                        edge_name=edge.edge_name,
                    )
                )
                continue

            source_class = self.registry.get_class(source.class_name)
            target_class = self.registry.get_class(target.class_name)
            if source_class is not None and source_class.name not in ontology_edge.domain:
                issues.append(
                    ValidationIssue(
                        code="EDGE_DOMAIN_MISMATCH",
                        message=(
                            f"Invalid edge domain for {edge.edge_name}: "
                            f"{source.class_name} is not allowed"
                        ),
                        location=f"edges.{edge_index}.sourceTempId",
                        node_temp_id=source.temp_id,
                        edge_name=edge.edge_name,
                    )
                )
            if target_class is not None and target_class.name not in ontology_edge.range:
                issues.append(
                    ValidationIssue(
                        code="EDGE_RANGE_MISMATCH",
                        message=(
                            f"Invalid edge range for {edge.edge_name}: "
                            f"{target.class_name} is not allowed"
                        ),
                        location=f"edges.{edge_index}.targetTempId",
                        node_temp_id=target.temp_id,
                        edge_name=edge.edge_name,
                    )
                )

            expected_rule_type = RULE_TYPE_BY_EDGE.get(edge.edge_name)
            if (
                expected_rule_type is not None
                and target.properties.get("pskg:ruleType") != expected_rule_type
            ):
                issues.append(
                    ValidationIssue(
                        code="SEMANTIC_CONFLICT",
                        message=(
                            f"Edge {edge.edge_name} requires target "
                            f"pskg:ruleType={expected_rule_type}"
                        ),
                        location=f"edges.{edge_index}",
                        node_temp_id=target.temp_id,
                        property_name="pskg:ruleType",
                        edge_name=edge.edge_name,
                    )
                )

        return self._deduplicate(issues)

    def validate_persistence(
        self,
        patch: CompiledGraphPatch,
    ) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []

        for node_index, node in enumerate(patch.nodes):
            ontology_class = self.registry.get_class(node.class_name)
            if ontology_class is None:
                continue

            for rule in ontology_class.rules:
                attribute = self.registry.get_attribute(rule.property)
                if attribute is None:
                    continue
                value = node.properties.get(rule.property)
                message = self._attribute_rule_failure(rule, value)
                if message is not None:
                    issues.append(
                        ValidationIssue(
                            code="ONTOLOGY_RULE_UNSATISFIED",
                            message=message,
                            location=f"nodes.{node_index}.properties.{rule.property}",
                            node_temp_id=node.temp_id,
                            property_name=rule.property,
                        )
                    )

        for node_index, node in enumerate(patch.nodes):
            ontology_class = self.registry.get_class(node.class_name)
            if ontology_class is None:
                continue
            outgoing = [
                edge for edge in patch.edges if edge.source_temp_id == node.temp_id
            ]
            for rule in ontology_class.rules:
                if self.registry.get_edge(rule.property) is None:
                    continue
                count = sum(edge.edge_name == rule.property for edge in outgoing)
                message = self._edge_rule_failure(rule, count)
                if message is not None:
                    issues.append(
                        ValidationIssue(
                            code="ONTOLOGY_RULE_UNSATISFIED",
                            message=message,
                            location=f"nodes.{node_index}.edges.{rule.property}",
                            node_temp_id=node.temp_id,
                            edge_name=rule.property,
                        )
                    )

        return self._deduplicate(issues)

    def _attribute_rule_failure(self, rule, value: Any) -> str | None:
        count = self._value_count(value)
        if rule.operator == "exactlyQualified":
            expected = self._to_int(rule.value)
            if expected is not None and count != expected:
                return (
                    f"Property {rule.property} must occur exactly {expected} "
                    f"time(s); got {count}"
                )
        elif rule.operator == "minQualified":
            minimum = self._to_int(rule.value)
            if minimum is not None and count < minimum:
                return (
                    f"Property {rule.property} must occur at least {minimum} "
                    f"time(s); got {count}"
                )
        elif rule.operator == "some":
            if value is None:
                return f"Missing required property: {rule.property}"
            expected = rule.value
            if isinstance(expected, str) and expected.startswith("xsd:"):
                if not self._is_valid_property_value(value, [expected]):
                    return f"Property {rule.property} must satisfy {expected}"
            elif isinstance(expected, str):
                values = value if isinstance(value, list) else [value]
                if expected not in values:
                    return f"Property {rule.property} must contain value {expected}"
        return None

    def _edge_rule_failure(self, rule, count: int) -> str | None:
        if rule.operator == "exactlyQualified":
            expected = self._to_int(rule.value)
            if expected is not None and count != expected:
                return (
                    f"Edge {rule.property} must occur exactly {expected} time(s); "
                    f"got {count}"
                )
        elif rule.operator == "minQualified":
            minimum = self._to_int(rule.value)
            if minimum is not None and count < minimum:
                return (
                    f"Edge {rule.property} must occur at least {minimum} time(s); "
                    f"got {count}"
                )
        elif rule.operator == "some" and count < 1:
            return f"Edge {rule.property} is required"
        return None

    def _is_valid_property_value(self, value: Any, ranges: list[str]) -> bool:
        if isinstance(value, list):
            return bool(value) and all(
                self._is_valid_single_value(item, ranges) for item in value
            )
        return self._is_valid_single_value(value, ranges)

    def _is_valid_single_value(self, value: Any, ranges: list[str]) -> bool:
        if value is None:
            return False
        for range_name in ranges:
            if range_name in {"xsd:string", "xsd:anyURI"} and isinstance(value, str):
                return True
            if range_name == "xsd:boolean" and isinstance(value, bool):
                return True
            if (
                range_name == "xsd:integer"
                and isinstance(value, int)
                and not isinstance(value, bool)
            ):
                return True
            if (
                range_name == "xsd:decimal"
                and isinstance(value, (int, float, Decimal))
                and not isinstance(value, bool)
            ):
                return True
            if range_name == "xsd:date" and self._is_iso_date(value):
                return True
            if range_name == "xsd:dateTime" and self._is_iso_datetime(value):
                return True
        return False

    @staticmethod
    def _is_iso_date(value: Any) -> bool:
        if isinstance(value, datetime):
            return False
        if isinstance(value, date):
            return True
        if not isinstance(value, str):
            return False
        try:
            date.fromisoformat(value)
            return len(value) == 10
        except ValueError:
            return False

    @staticmethod
    def _is_iso_datetime(value: Any) -> bool:
        if isinstance(value, datetime):
            return True
        if not isinstance(value, str):
            return False
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
            return True
        except ValueError:
            return False

    @staticmethod
    def _value_count(value: Any) -> int:
        if value is None:
            return 0
        return len(value) if isinstance(value, list) else 1

    @staticmethod
    def _to_int(value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _deduplicate(issues: list[ValidationIssue]) -> list[ValidationIssue]:
        unique: dict[tuple[str, str, str], ValidationIssue] = {}
        for issue in issues:
            unique[(issue.code, issue.location, issue.message)] = issue
        return list(unique.values())
