"""Phase 5 — Ánh xạ tên ontology sang định danh dùng trong Neo4j.

Neo4j không cho phép đặt label/relationship/property tuỳ ý, nên module này chuyển
class → label, edge → relationship type, property → key theo quy ước của graph và
chặn các định danh không hợp lệ trước khi ghi."""

import re

from app.services.ingestion.ontology.registry import OntologyRegistry


class Neo4jMappingError(ValueError):
    """
    Lỗi khi tên ontology không thể ánh xạ sang định danh hợp lệ của Neo4j.
    """
    pass


class Neo4jMapper:
    """
    Ánh xạ class/property/edge của ontology sang label/relationship/property của Neo4j.
    """
    def __init__(
        self,
        registry: OntologyRegistry,
    ):
        """
        Ghi nhận registry ontology dùng để tra cứu tên hiển thị.

        Args:
            registry: Registry ontology.
        """
        self.registry = registry

    def class_to_label(
        self,
        class_technical_name: str,
    ) -> str:
        """
        Đổi tên class ontology thành label Neo4j hợp lệ.

        Raises:
            Neo4jMappingError: Class không tồn tại hoặc label không hợp lệ.
        """
        ontology_class = self.registry.get_class(
            class_technical_name
        )

        if ontology_class is None:
            raise Neo4jMappingError(
                f"Unknown ontology class: "
                f"{class_technical_name}"
            )

        return self._validate_identifier(
            ontology_class.local_name,
            kind="label",
        )

    def property_to_key(
        self,
        property_technical_name: str,
    ) -> str:
        """
        Đổi tên property ontology thành key của Neo4j.
        """
        attribute = self.registry.get_attribute(
            property_technical_name
        )

        if attribute is None:
            raise Neo4jMappingError(
                f"Unknown ontology property: "
                f"{property_technical_name}"
            )

        return self._validate_identifier(
            attribute.local_name,
            kind="property",
        )

    def edge_to_type(
        self,
        edge_technical_name: str,
    ) -> str:
        """
        Đổi tên edge ontology thành relationship type của Neo4j.
        """
        edge = self.registry.get_edge(
            edge_technical_name
        )

        if edge is None:
            raise Neo4jMappingError(
                f"Unknown ontology edge: "
                f"{edge_technical_name}"
            )

        relationship_type = self._camel_to_upper_snake(
            edge.local_name
        )
        return self._validate_identifier(
            relationship_type,
            kind="relationship type",
        )

    def properties_to_neo4j(
        self,
        properties: dict,
    ) -> dict:
        """
        Chuyển toàn bộ thuộc tính của node sang dạng key/value dùng trong Neo4j.
        """
        result = {}

        for technical_name, value in properties.items():
            key = self.property_to_key(
                technical_name
            )

            result[key] = value

        return result

    @staticmethod
    def _validate_identifier(
        value: str,
        kind: str,
    ) -> str:
        """
        Kiểm tra định danh sinh ra có hợp lệ với Neo4j hay không.
        """
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
            raise Neo4jMappingError(
                f"Unsafe Neo4j {kind} derived from ontology: {value}"
            )
        return value

    @staticmethod
    def _camel_to_upper_snake(
        value: str,
    ) -> str:
        """
        Đổi tên kiểu camelCase sang UPPER_SNAKE_CASE theo quy ước.
        """
        value = re.sub(
            r"(?<!^)(?=[A-Z])",
            "_",
            value,
        )

        return value.upper()
