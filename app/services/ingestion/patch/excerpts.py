"""Phase 3 — Canonical hoá trích dẫn (evidence excerpt) theo định dạng của chunk.

LLM thường trả về trích dẫn lệch về khoảng trắng, bullet hoặc ô của bảng markdown.
Các hàm ở đây chuẩn hoá trích dẫn về đúng dạng xuất hiện trong chunk nguồn để phép
so khớp nguyên văn ở các bước sau không bị fail sai."""

import re
from app.core.schemas.ingestion.document import DocumentChunk
from app.core.schemas.ingestion.graph_patch import GraphPatchFragment


def _canonical_whitespace_excerpt(chunk: DocumentChunk, quote: str) -> str:
    """
    Chuẩn hoá trích dẫn khi khác biệt chỉ nằm ở khoảng trắng/xuống dòng.
    """
    if not quote.strip():
        return quote
    parts = [re.escape(part) for part in re.split(r"\s+", quote.strip()) if part]
    if not parts:
        return quote
    pattern = r"\s+".join(parts)
    for surface in (chunk.section or "", chunk.content):
        match = re.search(pattern, surface, flags=re.MULTILINE)
        if match is not None:
            return match.group(0)
    return quote


def _canonical_bullet_excerpt(chunk: DocumentChunk, quote: str) -> str:
    """
    Chuẩn hoá trích dẫn nằm trong gạch đầu dòng (bullet) của markdown.
    """
    if quote in chunk.content or quote in (chunk.section or ""):
        return quote
    for line in reversed(quote.splitlines()):
        candidate = line.strip()
        if candidate.startswith("- ") and candidate in chunk.content:
            return candidate
    return quote


def _canonical_markdown_excerpt(chunk: DocumentChunk, quote: str) -> str:
    """
    Chuẩn hoá trích dẫn theo cú pháp markdown (in đậm, in nghiêng, code...).
    """
    if quote in chunk.content or quote in (chunk.section or ""):
        return quote

    def plain(value: str) -> str:
        value = re.sub(r"(\*\*|__|`)", "", value)
        return re.sub(r"\s+", " ", value).strip()

    target = plain(quote)
    if not target:
        return quote
    for line in chunk.content.splitlines():
        if target in plain(line):
            return line.strip()
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", chunk.content) if part.strip()]
    for paragraph in paragraphs:
        if target in plain(paragraph):
            return paragraph
    return quote


def _canonical_table_excerpt(chunk: DocumentChunk, quote: str) -> str:
    """
    Chuẩn hoá trích dẫn nằm trong một dòng bảng markdown.
    """
    normalized = quote.replace("\r\n", "\n").replace("\r", "\n").strip()
    if "|" not in normalized:
        return quote
    for line in chunk.content.splitlines():
        row = line.strip()
        if row.startswith("|") and row.endswith("|") and normalized in row:
            return row
    return quote


def _all_evidence(fragment: GraphPatchFragment):
    """
    Liệt kê toàn bộ evidence của fragment (node, property và edge).
    """
    for node_index, node in enumerate(fragment.nodes):
        for item in node.evidence:
            yield f"nodes.{node_index}.evidence", item
        for property_index, prop in enumerate(node.properties):
            for item in prop.evidence:
                yield f"nodes.{node_index}.properties.{property_index}.evidence", item
    for edge_index, edge in enumerate(fragment.edges):
        for item in edge.evidence:
            yield f"edges.{edge_index}.evidence", item
