"""Phase 6 — Dựng ngữ cảnh truyền giữa các batch và cho prompt extraction.

Batch sau cần biết graph đã có gì để không tạo trùng node/edge, nhưng không thể mang
toàn bộ graph vào prompt. Module này dựng payload batch và phần ngữ cảnh graph đã
được rút gọn theo giới hạn ký tự cho mục đích đó."""

import json
import math
from typing import Any

from app.core.schemas.ingestion.workspace import (
    IngestionWorkspace,
)
from app.services.ingestion.orchestration.state import (
    GRAPH_CONTEXT_MAX_CHARS,
)


def _batch_payload(workspace: IngestionWorkspace, batch) -> dict[str, Any]:
    """
    Dựng payload mô tả một batch (chỉ số, chunk, phạm vi) để đưa vào prompt.
    """
    chunk_by_index = {chunk.index: chunk for chunk in workspace.chunks}
    canonical_context = _canonical_graph_context(workspace, batch.index) or ""
    total_input_chars = batch.content_chars + len(canonical_context)
    estimated_input_tokens = max(1, math.ceil(total_input_chars / 2.0))

    return {
        "batchIndex": batch.index,
        "chunkIndexes": batch.chunk_indexes,
        "contentChars": batch.content_chars,
        "totalInputChars": total_input_chars,
        "estimatedInputTokens": estimated_input_tokens,
        "chunks": [
            chunk_by_index[index].model_dump(by_alias=True, exclude_none=True)
            for index in batch.chunk_indexes
        ],
        "canonicalGraphContext": canonical_context,
    }


def _batch_summary(batch) -> dict[str, Any]:
    """
    Dựng mô tả gọn của batch để trả trong workflow/status mà không nhét full chunk
    content vào history. Payload đầy đủ được lấy qua `get_ingestion_batch`.
    """
    estimated_input_tokens = max(1, math.ceil(batch.content_chars / 2.0))
    return {
        "batchIndex": batch.index,
        "chunkIndexes": batch.chunk_indexes,
        "contentChars": batch.content_chars,
        "estimatedInputTokens": estimated_input_tokens,
    }


def _canonical_graph_context(
    workspace: IngestionWorkspace,
    before_batch_index: int,
) -> str:
    """
    Dựng ngữ cảnh graph dựa trên Candidate Retrieval từ IngestionStagingStore.
    """
    # Extract keywords from current batch chunks
    batch = (
        workspace.batches[before_batch_index]
        if before_batch_index < len(workspace.batches)
        else None
    )
    if not batch:
        return ""

    chunk_by_index = {chunk.index: chunk for chunk in workspace.chunks}
    batch_text = " ".join(
        [
            chunk_by_index[idx].content
            for idx in batch.chunk_indexes
            if idx in chunk_by_index
        ]
    )
    words = [w.strip() for w in batch_text.split() if len(w.strip()) > 3]
    keywords = list(dict.fromkeys(words))[:30]

    try:
        from app.services.ingestion.incremental.staging_store import (
            IngestionStagingStore,
        )

        store = IngestionStagingStore()
        candidates = store.find_relevant_entities(
            workspace.ingestion_id, keywords, limit=20
        )
        store.close()
        if candidates:
            lines = ["Relevant existing entities from persistent staging:"]
            for cand in candidates:
                ref_key = (
                    f"entity:{cand['entityKey']}"
                    if not cand["entityKey"].startswith("entity:")
                    else cand["entityKey"]
                )
                lines.append(f"- ref={ref_key}")
                lines.append(f"  class={cand['className']}")
                lines.append(
                    "  identity="
                    + json.dumps(cand["properties"], ensure_ascii=False, sort_keys=True)
                )
            return "\n".join(lines)[:GRAPH_CONTEXT_MAX_CHARS]
    except Exception:
        pass
    return ""
