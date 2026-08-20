from pydantic import BaseModel


class DocumentChunk(BaseModel):
    index: int
    source: str
    section: str | None = None
    content: str
