from pydantic import BaseModel, ConfigDict, Field

from app.core.schemas.ingestion.document import DocumentChunk


class ExtractionContext(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    document_name: str
    document_id: str | None = Field(default=None, alias="documentId")
    chunks: list[DocumentChunk]
    ontology_context: str
