from pydantic import BaseModel, ConfigDict, Field


class DocumentChunk(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    index: int
    source: str
    section: str | None = None
    content: str
    document_id: str | None = Field(default=None, alias="documentId")
    chunk_id: str | None = Field(default=None, alias="chunkId")
    content_hash: str | None = Field(default=None, alias="contentHash")
    structural_path: str | None = Field(default=None, alias="structuralPath")
    start_line: int | None = Field(default=None, alias="startLine", ge=1)
    end_line: int | None = Field(default=None, alias="endLine", ge=1)
