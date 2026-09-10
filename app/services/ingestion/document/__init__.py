"""Phase 1 — Đọc tài liệu nguồn và chuẩn bị ngữ cảnh extraction.

`DocumentReader` chịu trách nhiệm biến file/upload thành danh sách `DocumentChunk`;
`DocumentPreparation` ghép chunk với ontology để tạo `ExtractionContext` cho LLM.
"""

from app.services.ingestion.document.preparation import (
    DEFAULT_ONTOLOGY_PATH,
    DocumentPreparation,
)
from app.services.ingestion.document.reader import DocumentReadError, DocumentReader

__all__ = [
    "DEFAULT_ONTOLOGY_PATH",
    "DocumentPreparation",
    "DocumentReadError",
    "DocumentReader",
]
