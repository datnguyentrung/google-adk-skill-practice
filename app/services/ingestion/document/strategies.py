"""Pluggable loading/chunking strategies with deterministic source identity."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.core.schemas.ingestion.document import DocumentChunk

STRUCTURAL_CHUNKER_VERSION = "structural-v2"
DOCUMENT_ID_VERSION = "document-id-v1"


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def stable_document_id(source: str) -> str:
    """Return a stable logical ID that survives document content updates."""
    normalized = str(Path(source).name).strip().casefold()
    return f"doc_{_sha256(f'{DOCUMENT_ID_VERSION}\0{normalized}')[:32]}"


def chunk_content_hash(content: str) -> str:
    return _sha256(content)


def stable_chunk_id(
    *, document_id: str, structural_path: str, content_hash: str
) -> str:
    material = f"{document_id}\0{structural_path}\0{content_hash}"
    return f"chk_{_sha256(material)[:40]}"


@dataclass(frozen=True)
class LoadedDocument:
    source: str
    suffix: str
    text: str
    mime_type: str | None = None


class LoaderStrategy(Protocol):
    """Load a supported source into canonical UTF-8 text."""

    supported_suffixes: frozenset[str]

    def load_path(self, path: Path) -> LoadedDocument: ...

    def load_bytes(
        self, *, filename: str, data: bytes, mime_type: str | None = None
    ) -> LoadedDocument: ...


class Utf8TextLoader:
    supported_suffixes = frozenset({".md", ".txt"})

    def _validate_suffix(self, source: str) -> str:
        suffix = Path(source).suffix.lower()
        if suffix not in self.supported_suffixes:
            raise ValueError(f"Unsupported document type: {suffix}")
        return suffix

    def load_path(self, path: Path) -> LoadedDocument:
        suffix = self._validate_suffix(path.name)
        return LoadedDocument(
            source=path.name,
            suffix=suffix,
            text=path.read_text(encoding="utf-8"),
        )

    def load_bytes(
        self, *, filename: str, data: bytes, mime_type: str | None = None
    ) -> LoadedDocument:
        suffix = self._validate_suffix(filename)
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"Document is not valid UTF-8: {filename}") from exc
        return LoadedDocument(
            source=filename,
            suffix=suffix,
            text=text,
            mime_type=mime_type,
        )


class ChunkingStrategy(Protocol):
    version: str

    def split(self, document: LoadedDocument) -> list[DocumentChunk]: ...


class StructuralTextChunker:
    """Preserve Markdown structure and attach deterministic chunk identity."""

    version = STRUCTURAL_CHUNKER_VERSION
    heading_pattern = re.compile(r"^(#{1,6})\s+(.+?)\s*$")

    def split(self, document: LoadedDocument) -> list[DocumentChunk]:
        if document.suffix == ".md":
            return self._split_markdown(document)
        return [
            self._chunk(
                source=document.source,
                index=0,
                section=None,
                content=document.text.strip(),
                structural_path="__document__#0",
                start_line=1,
                end_line=max(1, len(document.text.splitlines())),
            )
        ]

    @staticmethod
    def _chunk(
        *, source: str, index: int, section: str | None, content: str,
        structural_path: str, start_line: int, end_line: int,
    ) -> DocumentChunk:
        document_id = stable_document_id(source)
        content_hash = chunk_content_hash(content)
        return DocumentChunk(
            index=index,
            source=source,
            section=section,
            content=content,
            documentId=document_id,
            chunkId=stable_chunk_id(
                document_id=document_id,
                structural_path=structural_path,
                content_hash=content_hash,
            ),
            contentHash=content_hash,
            structuralPath=structural_path,
            startLine=start_line,
            endLine=end_line,
        )

    def _split_markdown(self, document: LoadedDocument) -> list[DocumentChunk]:
        chunks: list[DocumentChunk] = []
        current_section: str | None = None
        current_path = "__preamble__"
        current_lines: list[str] = []
        current_start_line: int | None = None
        heading_stack: list[str] = []
        path_occurrences: dict[str, int] = {}

        def flush() -> None:
            nonlocal current_lines, current_start_line
            content = "\n".join(current_lines).strip()
            if not content:
                current_lines = []
                current_start_line = None
                return
            first_offset = next(i for i, line in enumerate(current_lines) if line.strip())
            last_offset = len(current_lines) - 1 - next(
                i for i, line in enumerate(reversed(current_lines)) if line.strip()
            )
            base_line = current_start_line or 1
            occurrence = path_occurrences.get(current_path, 0)
            path_occurrences[current_path] = occurrence + 1
            chunks.append(
                self._chunk(
                    source=document.source,
                    index=len(chunks),
                    section=current_section,
                    content=content,
                    structural_path=f"{current_path}#{occurrence}",
                    start_line=base_line + first_offset,
                    end_line=base_line + last_offset,
                )
            )
            current_lines = []
            current_start_line = None

        for line_number, line in enumerate(document.text.splitlines(), start=1):
            heading = self.heading_pattern.match(line)
            if heading:
                flush()
                level = len(heading.group(1))
                title = heading.group(2).strip()
                heading_stack = heading_stack[: level - 1]
                heading_stack.append(title)
                current_section = title
                current_path = " > ".join(heading_stack)
                continue
            if current_start_line is None:
                current_start_line = line_number
            current_lines.append(line)
        flush()
        return chunks


__all__ = [
    "DOCUMENT_ID_VERSION",
    "STRUCTURAL_CHUNKER_VERSION",
    "ChunkingStrategy",
    "LoadedDocument",
    "LoaderStrategy",
    "StructuralTextChunker",
    "Utf8TextLoader",
    "chunk_content_hash",
    "stable_chunk_id",
    "stable_document_id",
]
