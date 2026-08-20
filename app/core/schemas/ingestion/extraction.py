from pydantic import BaseModel

from app.core.schemas.ingestion.document import DocumentChunk


class ExtractionContext(BaseModel):
    document_name: str
    chunks: list[DocumentChunk]
    ontology_context: str
