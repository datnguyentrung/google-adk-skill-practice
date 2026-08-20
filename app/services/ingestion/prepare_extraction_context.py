import logging
from pathlib import Path

from app.core.schemas.ingestion.extraction import (
    ExtractionContext,
)
from app.services.ingestion.document_reader import (
    DocumentReader,
)
from app.services.ingestion.extraction_context import (
    ExtractionContextBuilder,
)
from app.services.ingestion.loader import OntologyLoader
from app.services.ingestion.registry import OntologyRegistry

logger = logging.getLogger(__name__)

DEFAULT_ONTOLOGY_PATH = (
    "app/data/ontology/product_sales_knowledge_graph_base_v3_1.ontology.json"
)


class ExtractionContextService:
    def __init__(
        self,
        ontology_path: str | Path = DEFAULT_ONTOLOGY_PATH,
    ):
        self.ontology_path = Path(ontology_path)
        logger.info(
            "Initializing ExtractionContextService ontology_path=%s",
            self.ontology_path,
        )

        ontology = OntologyLoader.load(self.ontology_path)

        self.registry = OntologyRegistry(ontology)

        self.reader = DocumentReader()

        self.builder = ExtractionContextBuilder(self.registry)

    def prepare(
        self,
        document_path: str | Path,
    ) -> ExtractionContext:
        path = Path(document_path)
        logger.info("Preparing extraction context from path=%s", path)

        chunks = self.reader.read(path)

        ontology_context = self.builder.build_ontology_context()

        logger.info(
            "Prepared extraction context document=%s chunk_count=%s ontology_context_chars=%s",
            path.name,
            len(chunks),
            len(ontology_context),
        )

        return ExtractionContext(
            document_name=path.name,
            chunks=chunks,
            ontology_context=ontology_context,
        )

    def prepare_uploaded_document(
        self,
        *,
        filename: str,
        data: bytes,
        mime_type: str | None = None,
    ) -> ExtractionContext:
        """Prepare extraction context from an ADK-uploaded artifact."""

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
        ontology_context = self.builder.build_ontology_context()

        logger.info(
            "Prepared uploaded extraction context document=%s chunk_count=%s ontology_context_chars=%s",
            filename,
            len(chunks),
            len(ontology_context),
        )

        return ExtractionContext(
            document_name=filename,
            chunks=chunks,
            ontology_context=ontology_context,
        )
