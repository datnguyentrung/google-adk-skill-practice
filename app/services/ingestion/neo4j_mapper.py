import re

from app.services.ingestion.registry import OntologyRegistry


class Neo4jMappingError(ValueError):
    pass


class Neo4jMapper:
    def __init__(
        self,
        registry: OntologyRegistry,
    ):
        self.registry = registry

    def class_to_label(
        self,
        class_technical_name: str,
    ) -> str:
        ontology_class = self.registry.get_class(
            class_technical_name
        )

        if ontology_class is None:
            raise Neo4jMappingError(
                f"Unknown ontology class: "
                f"{class_technical_name}"
            )

        return ontology_class.local_name

    def property_to_key(
        self,
        property_technical_name: str,
    ) -> str:
        attribute = self.registry.get_attribute(
            property_technical_name
        )

        if attribute is None:
            raise Neo4jMappingError(
                f"Unknown ontology property: "
                f"{property_technical_name}"
            )

        return attribute.local_name

    def edge_to_type(
        self,
        edge_technical_name: str,
    ) -> str:
        edge = self.registry.get_edge(
            edge_technical_name
        )

        if edge is None:
            raise Neo4jMappingError(
                f"Unknown ontology edge: "
                f"{edge_technical_name}"
            )

        return self._camel_to_upper_snake(
            edge.local_name
        )

    def properties_to_neo4j(
        self,
        properties: dict,
    ) -> dict:
        result = {}

        for technical_name, value in properties.items():
            key = self.property_to_key(
                technical_name
            )

            result[key] = value

        return result

    @staticmethod
    def _camel_to_upper_snake(
        value: str,
    ) -> str:
        value = re.sub(
            r"(?<!^)(?=[A-Z])",
            "_",
            value,
        )

        return value.upper()
