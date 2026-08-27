from app.core.schemas.ingestion.models import (
    OntologyAttribute,
    OntologyClass,
    OntologyDefinition,
    OntologyEdge,
)


class OntologyRegistry:
    RUNTIME_MANAGED_STATUS_DEFAULT = "Draft"
    EDGE_DERIVED_RULE_TYPES = {
        "pskg:governedByPolicy": "POLICY",
        "pskg:hasEligibilityRule": "ELIGIBILITY",
        "pskg:hasSalesConditionRule": "SALES_CONDITION",
    }

    def __init__(self, ontology: OntologyDefinition):
        self.ontology = ontology

        # dict[ClassName, OntologyClass]
        self._classes: dict[str, OntologyClass] = {
            cls.technical_name: cls for cls in ontology.classes
        }

        self._attributes: dict[str, OntologyAttribute] = {
            attr.technical_name: attr for attr in ontology.attributes
        }

        self._edges: dict[str, OntologyEdge] = {
            edge.technical_name: edge for edge in ontology.edges
        }

    def get_class(self, technical_name: str) -> OntologyClass | None:
        return self._classes.get(technical_name)

    def get_attribute(self, technical_name: str) -> OntologyAttribute | None:
        return self._attributes.get(technical_name)

    def get_edge(self, technical_name: str) -> OntologyEdge | None:
        return self._edges.get(technical_name)

    def list_classes(self) -> list[str]:
        return list(self._classes.keys())

    def list_attributes(self) -> list[str]:
        return list(self._attributes.keys())

    def list_edges(self) -> list[str]:
        return list(self._edges.keys())

    def has_class(self, technical_name: str) -> bool:
        return technical_name in self._classes

    def has_attribute(self, technical_name: str) -> bool:
        return technical_name in self._attributes

    def has_edge(self, technical_name: str) -> bool:
        return technical_name in self._edges

    def has_any(self, technical_name: str) -> bool:
        return (
            self.has_class(technical_name)
            or self.has_attribute(technical_name)
            or self.has_edge(technical_name)
        )

    def derived_target_properties_for_edge(
        self, edge_technical_name: str
    ) -> list[tuple[OntologyAttribute, object]]:
        """Return target properties derived from this edge by ontology policy."""
        derived: list[tuple[OntologyAttribute, object]] = []
        for attribute in self.ontology.attributes:
            policy = attribute.ingestion_policy
            if policy.mode != "edge_derived":
                continue
            if edge_technical_name in policy.derive_from_edges:
                derived.append((attribute, policy.derive_from_edges[edge_technical_name]))
        rule_type = self.get_attribute("pskg:ruleType")
        if (
            rule_type is not None
            and edge_technical_name in self.EDGE_DERIVED_RULE_TYPES
            and all(item[0].technical_name != rule_type.technical_name for item in derived)
        ):
            derived.append((rule_type, self.EDGE_DERIVED_RULE_TYPES[edge_technical_name]))
        return derived

    def edge_names_deriving_property(self, attribute_technical_name: str) -> set[str]:
        attribute = self.get_attribute(attribute_technical_name)
        if attribute is None:
            return set()
        policy = attribute.ingestion_policy
        names = set(policy.derive_from_edges) if policy.mode == "edge_derived" else set()
        if attribute_technical_name == "pskg:ruleType":
            names.update(self.EDGE_DERIVED_RULE_TYPES)
        return names

    def configured_defaults_for_class(
        self, class_technical_name: str
    ) -> list[tuple[OntologyAttribute, object]]:
        """Return ontology-configured defaults owned by the ingestion runtime."""
        ontology_class = self.get_class(class_technical_name)
        if ontology_class is None:
            return []
        result: list[tuple[OntologyAttribute, object]] = []
        for attribute in self.ontology.attributes:
            policy = attribute.ingestion_policy
            if ontology_class.name not in attribute.domain:
                continue
            if (
                policy.mode not in {"runtime_managed", "system_default"}
                and not self._is_runtime_managed_status_attribute(attribute)
            ):
                continue
            default_value = (
                policy.default_value
                if policy.default_value is not None
                else self.RUNTIME_MANAGED_STATUS_DEFAULT
            )
            result.append((attribute, default_value))
        return result

    def is_runtime_managed_attribute(self, technical_name: str) -> bool:
        attribute = self.get_attribute(technical_name)
        return bool(
            attribute
            and (
                attribute.ingestion_policy.mode == "runtime_managed"
                or self._is_runtime_managed_status_attribute(attribute)
            )
        )

    @staticmethod
    def _is_runtime_managed_status_attribute(attribute: OntologyAttribute) -> bool:
        return attribute.local_name.endswith("Status")

    def properties_from_class(
        self, class_technical_name: str
    ) -> list[OntologyAttribute]:

        ontology_class = self.get_class(class_technical_name)
        if not ontology_class:
            return []

        return [
            attribute
            for attribute in self.ontology.attributes
            if ontology_class.name in attribute.domain
        ]

    def edges_from_class(self, class_technical_name: str) -> list[OntologyEdge]:

        ontology_class = self.get_class(class_technical_name)

        if not ontology_class:
            return []

        return [
            edge for edge in self.ontology.edges if ontology_class.name in edge.domain
        ]
