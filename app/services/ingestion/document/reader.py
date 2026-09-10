"""Phase 1 — load source documents through replaceable strategies."""

from __future__ import annotations

import logging
from pathlib import Path

from app.core.schemas.ingestion.document import DocumentChunk
from app.services.ingestion.document.strategies import (
    ChunkingStrategy,
    LoaderStrategy,
    StructuralTextChunker,
    Utf8TextLoader,
)

logger = logging.getLogger(__name__)


class DocumentReadError(ValueError):
    """Business error raised when an ingestion document cannot be loaded safely."""


class DocumentReader:
    """Load and chunk a source without coupling orchestration to file formats."""

    def __init__(
        self,
        *,
        loader: LoaderStrategy | None = None,
        chunker: ChunkingStrategy | None = None,
    ) -> None:
        self.loader = loader or Utf8TextLoader()
        self.chunker = chunker or StructuralTextChunker()
        self.SUPPORTED_SUFFIXES = set(self.loader.supported_suffixes)

    def read(self, path: str | Path) -> list[DocumentChunk]:
        document_path = Path(path)
        logger.info("Reading ingestion document path=%s", document_path)
        if not document_path.exists():
            raise FileNotFoundError(f"Document not found: {document_path}")
        if not document_path.is_file():
            raise DocumentReadError(f"Document path is not a file: {document_path}")
        try:
            document = self.loader.load_path(document_path)
        except (OSError, UnicodeError, ValueError) as exc:
            raise DocumentReadError(str(exc)) from exc
        return self._split(document)

    def read_bytes(
        self,
        *,
        filename: str,
        data: bytes,
        mime_type: str | None = None,
    ) -> list[DocumentChunk]:
        logger.info(
            "Reading uploaded ingestion document filename=%s mime_type=%s byte_count=%s",
            filename,
            mime_type,
            len(data),
        )
        try:
            document = self.loader.load_bytes(
                filename=filename, data=data, mime_type=mime_type
            )
        except (UnicodeError, ValueError) as exc:
            raise DocumentReadError(str(exc)) from exc
        return self._split(document)

    def _split(self, document) -> list[DocumentChunk]:
        if not document.text.strip():
            raise DocumentReadError(f"Document is empty: {document.source}")
        chunks = self.chunker.split(document)
        if not chunks:
            raise DocumentReadError(
                f"Document contains no chunkable content: {document.source}"
            )
        logger.info(
            "Ingestion document prepared source=%s suffix=%s char_count=%s chunk_count=%s chunker=%s",
            document.source,
            document.suffix,
            len(document.text),
            len(chunks),
            getattr(self.chunker, "version", type(self.chunker).__name__),
        )
        return chunks


__all__ = ["DocumentReadError", "DocumentReader"]
