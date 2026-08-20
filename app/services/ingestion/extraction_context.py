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
            ontology_class = self.registry.get_class(
                class_name
            )

            if ontology_class is None:
                continue

            lines.append(
                f"CLASS {ontology_class.technical_name}"
            )

            lines.append(
                f"  name: {ontology_class.name}"
            )

            lines.append(
                f"  definition: "
                f"{ontology_class.definition}"
            )

            attributes = (
                self.registry.properties_from_class(
                    ontology_class.technical_name
                )
            )

            if attributes:
                lines.append("  properties:")

                for attribute in attributes:
                    lines.append(
                        f"    - "
                        f"{attribute.technical_name}"
                        f" -> {attribute.range}"
                    )

            edges = self.registry.edges_from_class(
                ontology_class.technical_name
            )

            if edges:
                lines.append("  outgoing_edges:")

                for edge in edges:
                    lines.append(
                        f"    - "
                        f"{edge.technical_name}"
                        f" -> {edge.range}"
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
                        (
                            f"SECTION: "
                            f"{chunk.section or 'N/A'}"
                        ),
                        chunk.content,
                    ]
                )
            )

        return "\n\n".join(parts)
