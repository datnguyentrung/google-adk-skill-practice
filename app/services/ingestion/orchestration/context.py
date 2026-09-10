"""Phase 6 — Dựng ngữ cảnh truyền giữa các batch và cho prompt extraction.

Batch sau cần biết graph đã có gì để không tạo trùng node/edge, nhưng không thể mang
toàn bộ graph vào prompt. Module này dựng payload batch và phần ngữ cảnh graph đã
được rút gọn theo giới hạn ký tự cho mục đích đó."""

import json
from typing import Any

from app.core.schemas.ingestion.workspace import (
    IngestionWorkspace,
)
from app.services.ingestion.orchestration.state import (
    GRAPH_CONTEXT_MAX_CHARS,
    _get_workspace_service,
)


def _batch_payload(workspace: IngestionWorkspace, batch) -> dict[str, Any]:
    """
    Dựng payload mô tả một batch (chỉ số, chunk, phạm vi) để đưa vào prompt.
    """
    chunk_by_index = {chunk.index: chunk for chunk in workspace.chunks}
    return {
        "batchIndex": batch.index,
        "chunkIndexes": batch.chunk_indexes,
        "contentChars": batch.content_chars,
        "chunks": [
            chunk_by_index[index].model_dump(by_alias=True, exclude_none=True)
            for index in batch.chunk_indexes
        ],
    }


def _canonical_graph_context(
    workspace: IngestionWorkspace,
    before_batch_index: int,
) -> str:
    """
    Dựng ngữ cảnh graph của các batch trước (node/edge đã trích xuất) cho batch kế tiếp.
    """

    fragments = [
        batch.fragment
        for batch in workspace.batches
        if batch.index < before_batch_index and batch.fragment is not None
    ]
    if not fragments:
        return ""
    fragment = _get_workspace_service().merge_fragments(fragments)
    node_lines: list[str] = []
    edge_lines: list[str] = []
    for node in fragment.nodes:
        identity = {
            entry.property_name: entry.value
            for entry in node.properties
            if not isinstance(entry.value, (dict, list))
        }
        node_lines.append(f"- ref={node.temp_id}")
        node_lines.append(f"  class={node.class_name}")
        node_lines.append(
            "  identity=" + json.dumps(identity, ensure_ascii=False, sort_keys=True)
        )
    for edge in fragment.edges:
        edge_lines.append(
            f"- {edge.edge_name}: {edge.source_temp_id} -> {edge.target_temp_id}"
        )
    if not node_lines and not edge_lines:
        return ""
    lines = ["Existing canonical graph:"]
    if node_lines:
        lines.append("Nodes:")
        lines.extend(node_lines)
    if edge_lines:
        lines.append("Existing edges:")
        lines.extend(edge_lines)
    text = "\n".join(lines)
    if len(text) <= GRAPH_CONTEXT_MAX_CHARS:
        return text
    node_text = "\n".join(["Existing canonical graph:", "Nodes:", *node_lines])
    if len(node_text) <= GRAPH_CONTEXT_MAX_CHARS:
        return node_text
    trimmed: list[str] = []
    used = len("Existing canonical graph:\nNodes:")
    for line in node_lines:
        if used + len(line) + 1 > GRAPH_CONTEXT_MAX_CHARS:
            break
        trimmed.append(line)
        used += len(line) + 1
    return "\n".join(["Existing canonical graph:", "Nodes:", *trimmed])


def _compact_ontology_context(ontology_context: str) -> str:
    """
    Rút gọn phần mô tả ontology trước khi đưa vào prompt để tiết kiệm token.
    """
    return ontology_context
