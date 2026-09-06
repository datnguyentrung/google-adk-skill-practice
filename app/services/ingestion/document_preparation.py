from __future__ import annotations

import logging
from pathlib import Path

from app.core.schemas.ingestion.document import DocumentChunk
from app.core.schemas.ingestion.extraction import ExtractionContext
from app.services.ingestion.document_reader import DocumentReader
from app.services.ingestion.loader import OntologyLoader
from app.services.ingestion.registry import OntologyRegistry

logger = logging.getLogger(__name__)

DEFAULT_ONTOLOGY_PATH = (
    "app/data/ontology/product_sales_knowledge_graph_base_v3_1.ontology.json"
)


class DocumentPreparation:
    def __init__(self, ontology_path: str | Path = DEFAULT_ONTOLOGY_PATH):
        self.ontology_path = Path(ontology_path)
        ontology = OntologyLoader.load(self.ontology_path)
        self.registry = OntologyRegistry(ontology)
        self.reader = DocumentReader()

    def prepare(self, document_path: str | Path) -> ExtractionContext:
        path = Path(document_path)
        logger.info("Preparing extraction context from path=%s", path)
        chunks = self.reader.read(path)
        return self._context(document_name=path.name, chunks=chunks)

    def prepare_uploaded_document(
        self,
        *,
        filename: str,
        data: bytes,
        mime_type: str | None = None,
    ) -> ExtractionContext:
        logger.info(
            "Preparing extraction context from uploaded document filename=%s mime_type=%s byte_count=%s",
            filename,
            mime_type,
            len(data),
        )
        chunks = self.reader.read_bytes(
            filename=filename,
            data=data,
            mime_type=mime_type,
        )
        return self._context(document_name=filename, chunks=chunks)

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
                        "  - "
                        f"{attribute.technical_name}"
                        f" | label={attribute.label}"
                        f" | range={attribute.range}"
                        f" | definition={attribute.definition}"
                        f" | policy={attribute.ingestion_policy.mode}"
                        f" | grounding={attribute.ingestion_policy.grounding}"
                    )

            edges = self.registry.edges_from_class(
                ontology_class.technical_name
            )
            if edges:
                lines.append("OUTGOING EDGES:")
                for edge in edges:
                    lines.append(
                        "  - "
                        f"{edge.technical_name}"
                        f" | label={edge.label}"
                        f" | domain={edge.domain}"
                        f" | range={edge.range}"
                        f" | definition={edge.definition}"
                    )
                    if edge.grounding_cues:
                        lines.append(f"    cues={edge.grounding_cues}")

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
    def build_document_context(chunks: list[DocumentChunk]) -> str:
        return "\n\n".join(
            "\n".join(
                [
                    f"[CHUNK {chunk.index}]",
                    f"SOURCE: {chunk.source}",
                    f"SECTION: {chunk.section or 'N/A'}",
                    chunk.content,
                ]
            )
            for chunk in chunks
        )

    def _context(self, *, document_name: str, chunks: list[DocumentChunk]) -> ExtractionContext:
        ontology_context = self.build_ontology_context()
        logger.info(
            "Prepared extraction context document=%s chunk_count=%s ontology_context_chars=%s",
            document_name,
            len(chunks),
            len(ontology_context),
        )
        return ExtractionContext(
            document_name=document_name,
            chunks=chunks,
            ontology_context=ontology_context,
        )

