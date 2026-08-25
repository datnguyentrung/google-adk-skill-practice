from pydantic import BaseModel, ConfigDict, Field


class DocumentChunk(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    index: int
    source: str
    section: str | None = None
    content: str
    chunk_id: str | None = Field(default=None, alias="chunkId")
    start_line: int | None = Field(default=None, alias="startLine", ge=1)
    end_line: int | None = Field(default=None, alias="endLine", ge=1)
