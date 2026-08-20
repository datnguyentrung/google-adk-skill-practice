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

DEFAULT_ONTOLOGY_PATH = (
    "app/data/ontology/product_sales_knowledge_graph_base_v3_1.ontology.json"
)


class ExtractionContextService:
    def __init__(
        self,
        ontology_path: str | Path = DEFAULT_ONTOLOGY_PATH,
    ):
        self.ontology_path = Path(ontology_path)

        ontology = OntologyLoader.load(self.ontology_path)

        self.registry = OntologyRegistry(ontology)

        self.reader = DocumentReader()

        self.builder = ExtractionContextBuilder(self.registry)

    def prepare(
        self,
        document_path: str | Path,
    ) -> ExtractionContext:
        path = Path(document_path)

        chunks = self.reader.read(path)

        ontology_context = self.builder.build_ontology_context()

        return ExtractionContext(
            document_name=path.name,
            chunks=chunks,
            ontology_context=ontology_context,
        )
