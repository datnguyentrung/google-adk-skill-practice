from app.core.schemas.ingestion.models import (
    OntologyAttribute,
    OntologyClass,
    OntologyDefinition,
    OntologyEdge,
)


class OntologyRegistry:
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
