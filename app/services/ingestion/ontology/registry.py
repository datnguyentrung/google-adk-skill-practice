"""Phase 0 — Tra cứu ontology đã nạp (class, property, edge, rule).

Module này bọc `OntologyDefinition` trong một chỉ mục tra cứu nhanh theo
technical name, đồng thời trả lời các câu hỏi suy diễn được dùng nhiều lần
trong pipeline: thuộc tính nào do runtime quản lý, thuộc tính nào được suy ra
từ edge, và giá trị mặc định của một class là gì."""

from typing import ClassVar
from app.core.schemas.ingestion.models import (
    OntologyAttribute,
    OntologyClass,
    OntologyDefinition,
    OntologyEdge,
)


class OntologyRegistry:
    """
    Chỉ mục tra cứu ontology kèm các quy tắc suy diễn của ingestion.
    """
    RUNTIME_MANAGED_STATUS_DEFAULT = "Draft"
    EDGE_DERIVED_RULE_TYPES: ClassVar[dict[str, str]] = {
        "pskg:governedByPolicy": "POLICY",
        "pskg:hasEligibilityRule": "ELIGIBILITY",
        "pskg:hasSalesConditionRule": "SALES_CONDITION",
    }

    def __init__(self, ontology: OntologyDefinition):
        """
        Dựng chỉ mục class/property/edge theo technical name từ ontology.

        Args:
            ontology: Định nghĩa ontology đã nạp từ file JSON.
        """
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
        """
        Trả về `OntologyClass` theo technical name; không có thì trả `None`.
        """
        return self._classes.get(technical_name)

    def get_attribute(self, technical_name: str) -> OntologyAttribute | None:
        """
        Trả về `OntologyAttribute` theo technical name; không có thì trả `None`.
        """
        return self._attributes.get(technical_name)

    def get_edge(self, technical_name: str) -> OntologyEdge | None:
        """
        Trả về `OntologyEdge` theo technical name; không có thì trả `None`.
        """
        return self._edges.get(technical_name)

    def list_classes(self) -> list[str]:
        """
        Liệt kê technical name của toàn bộ class trong ontology.
        """
        return list(self._classes.keys())

    def list_attributes(self) -> list[str]:
        """
        Liệt kê technical name của toàn bộ attribute trong ontology.
        """
        return list(self._attributes.keys())

    def list_edges(self) -> list[str]:
        """
        Liệt kê technical name của toàn bộ edge trong ontology.
        """
        return list(self._edges.keys())

    def has_class(self, technical_name: str) -> bool:
        """
        Kiểm tra ontology có class với technical name này hay không.
        """
        return technical_name in self._classes

    def has_attribute(self, technical_name: str) -> bool:
        """
        Kiểm tra ontology có attribute với technical name này hay không.
        """
        return technical_name in self._attributes

    def has_edge(self, technical_name: str) -> bool:
        """
        Kiểm tra ontology có edge với technical name này hay không.
        """
        return technical_name in self._edges

    def has_any(self, technical_name: str) -> bool:
        """
        Kiểm tra technical name có tồn tại dưới bất kỳ loại nào (class/attribute/edge).
        """
        return (
            self.has_class(technical_name)
            or self.has_attribute(technical_name)
            or self.has_edge(technical_name)
        )

    def derived_target_properties_for_edge(
        self, edge_technical_name: str
    ) -> list[tuple[OntologyAttribute, object]]:
        """
        Liệt kê các cặp (attribute, giá trị) mà một edge sẽ suy diễn cho node đích.
        """
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
        """
        Trả về tên các edge có thể suy diễn ra giá trị cho một property.
        """
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
        """
        Liệt kê các cặp (attribute, giá trị mặc định) được cấu hình cho một class.
        """
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
        """
        Cho biết property này do runtime quản lý (không lấy từ tài liệu nguồn).
        """
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
        """
        Nhận dạng attribute `status` được runtime quản lý theo quy ước của ontology.
        """
        return attribute.local_name.endswith("Status")

    def properties_from_class(
        self, class_technical_name: str
    ) -> list[OntologyAttribute]:

        """
        Liệt kê attribute được khai báo trực tiếp trên một class.
        """
        ontology_class = self.get_class(class_technical_name)
        if not ontology_class:
            return []

        return [
            attribute
            for attribute in self.ontology.attributes
            if ontology_class.name in attribute.domain
        ]

    def edges_from_class(self, class_technical_name: str) -> list[OntologyEdge]:

        """
        Liệt kê edge được khai báo trực tiếp trên một class.
        """
        ontology_class = self.get_class(class_technical_name)

        if not ontology_class:
            return []

        return [
            edge for edge in self.ontology.edges if ontology_class.name in edge.domain
        ]
