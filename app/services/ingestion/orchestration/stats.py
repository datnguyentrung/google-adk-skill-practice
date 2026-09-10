"""Phase 6 — Thống kê tiến độ ingestion để trả về cho caller và ghi log.

Các hàm ở đây tổng hợp số node/edge/coverage của một fragment, của một batch và của
cả workspace, giúp theo dõi tiến độ và chẩn đoán khi một batch trả về rỗng."""

from typing import Any

from app.core.schemas.ingestion.graph_patch import GraphPatchFragment
from app.core.schemas.ingestion.workspace import (
    IngestionWorkspace,
)


def _batch_stats(workspace: IngestionWorkspace, batch) -> dict[str, Any]:
    """
    Tổng hợp trạng thái của một batch (đã submit chưa, số node/edge...).
    """
    chunk_by_index = {chunk.index: chunk for chunk in workspace.chunks}
    chunks = [chunk_by_index[index] for index in batch.chunk_indexes]
    return {
        "batchIndex": batch.index,
        "chunkCount": len(chunks),
        "chunkIndexes": batch.chunk_indexes,
        "inputChars": batch.content_chars,
        "chunkIds": [chunk.chunk_id for chunk in chunks if chunk.chunk_id],
    }


def _fragment_stats(fragment: GraphPatchFragment | None) -> dict[str, Any]:
    """
    Tổng hợp số lượng node, edge và coverage của một fragment.
    """
    if fragment is None:
        return {
            "nodes": 0,
            "edges": 0,
            "properties": 0,
            "evidenceItems": 0,
            "coverage": 0,
            "mappedCoverage": 0,
            "notRelevantCoverage": 0,
            "coverageDispositions": {},
        }
    property_count = sum(len(node.properties) for node in fragment.nodes)
    evidence_count = sum(len(node.evidence) for node in fragment.nodes)
    evidence_count += sum(
        len(prop.evidence) for node in fragment.nodes for prop in node.properties
    )
    evidence_count += sum(len(edge.evidence) for edge in fragment.edges)
    dispositions: dict[str, int] = {}
    for item in fragment.coverage:
        dispositions[item.decision] = dispositions.get(item.decision, 0) + 1
    return {
        "nodes": len(fragment.nodes),
        "edges": len(fragment.edges),
        "properties": property_count,
        "evidenceItems": evidence_count,
        "coverage": len(fragment.coverage),
        "mappedCoverage": sum(
            1 for item in fragment.coverage if item.decision == "MAPPED"
        ),
        "notRelevantCoverage": sum(
            1
            for item in fragment.coverage
            if item.decision in {"NO_RELEVANT_FACT", "NOT_RELEVANT"}
        ),
        "coverageDispositions": dispositions,
    }


def _workspace_stats(workspace: IngestionWorkspace) -> dict[str, Any]:
    """
    Tổng hợp tiến độ của toàn workspace (batch đã xong, node/edge tích luỹ).
    """
    processed = sum(batch.fragment is not None for batch in workspace.batches)
    return {
        "documentChunks": len(workspace.chunks),
        "batches": len(workspace.batches),
        "processedBatches": processed,
        "remainingBatches": len(workspace.batches) - processed,
        "candidateNodes": sum(
            len(batch.fragment.nodes)
            for batch in workspace.batches
            if batch.fragment is not None
        ),
        "candidateEdges": sum(
            len(batch.fragment.edges)
            for batch in workspace.batches
            if batch.fragment is not None
        ),
        "cacheHitBatches": sum(
            1 for batch in workspace.batches if batch.extraction_cache_hit
        ),
        "cacheEligibleBatches": sum(
            1 for batch in workspace.batches if batch.extraction_cache_key is not None
        ),
        "skippedChunks": len(workspace.skipped_chunk_indexes),
        "warningCount": len(workspace.ingestion_warnings),
    }
