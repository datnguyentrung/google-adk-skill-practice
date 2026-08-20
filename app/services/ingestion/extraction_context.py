from app.core.schemas.ingestion.document import DocumentChunk
from app.services.ingestion.registry import OntologyRegistry


class ExtractionContextBuilder:
    def __init__(
        self,
        registry: OntologyRegistry,
    ):
        self.registry = registry

    def build_ontology_context(self) -> str:
        lines: list[str] = []

        for class_name in self.registry.list_classes():
            ontology_class = self.registry.get_class(class_name)

            if ontology_class is None:
                continue

            lines.append(f"CLASS: {ontology_class.technical_name}")

            lines.append(f"NAME: {ontology_class.name}")

            lines.append(f"DEFINITION: {ontology_class.definition}")

            properties = self.registry.properties_from_class(
                ontology_class.technical_name
            )

            if properties:
                lines.append("PROPERTIES:")

                for attribute in properties:
                    lines.append(
                        f"  - {attribute.technical_name} | range={attribute.range}"
                    )

            edges = self.registry.edges_from_class(ontology_class.technical_name)

            if edges:
                lines.append("OUTGOING EDGES:")

                for edge in edges:
                    lines.append(
                        "  - "
                        f"{edge.technical_name}"
                        f" | domain={edge.domain}"
                        f" | range={edge.range}"
                    )

            if ontology_class.rules:
                lines.append("RULES:")

                for rule in ontology_class.rules:
                    lines.append(
                        "  - "
                        f"{rule.property}"
                        f" | operator={rule.operator}"
                        f" | value={rule.value}"
                        f" | qualifier={rule.qualifier}"
                    )

            lines.append("")

        return "\n".join(lines)

    @staticmethod
    def build_document_context(
        chunks: list[DocumentChunk],
    ) -> str:
        parts: list[str] = []

        for chunk in chunks:
            parts.append(
                "\n".join(
                    [
                        f"[CHUNK {chunk.index}]",
                        f"SOURCE: {chunk.source}",
                        (f"SECTION: {chunk.section or 'N/A'}"),
                        chunk.content,
                    ]
                )
            )

        return "\n\n".join(parts)
