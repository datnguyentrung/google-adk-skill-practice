"""Thống kê trạng thái và tiến độ ingestion."""

import math
from typing import Any

from app.core.schemas.ingestion.graph_patch import GraphPatchFragment
from app.core.schemas.ingestion.workspace import IngestionWorkspace


def _batch_stats(
    workspace: IngestionWorkspace,
    batch,
) -> dict[str, Any]:
    """Tổng hợp metadata của một batch."""

    chunk_by_index = {chunk.index: chunk for chunk in workspace.chunks}

    chunks = [chunk_by_index[index] for index in batch.chunk_indexes]
    estimated_input_tokens = max(1, math.ceil(batch.content_chars / 2.0))

    return {
        "batchIndex": batch.index,
        "chunkCount": len(chunks),
        "chunkIndexes": batch.chunk_indexes,
        "inputChars": batch.content_chars,
        "estimatedInputTokens": estimated_input_tokens,
        "chunkIds": [chunk.chunk_id for chunk in chunks if chunk.chunk_id],
    }


def _fragment_stats(
    fragment: GraphPatchFragment | None,
) -> dict[str, Any]:
    """Tổng hợp thống kê của một GraphPatchFragment."""

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


def _workspace_stats(
    workspace: IngestionWorkspace,
) -> dict[str, Any]:
    """Tổng hợp tiến độ của ingestion workspace."""

    processed = sum(batch.status == "STAGED" for batch in workspace.batches)

    return {
        "documentChunks": len(workspace.chunks),
        "batches": len(workspace.batches),
        "processedBatches": processed,
        "remainingBatches": len(workspace.batches) - processed,
        "stagedNodeCount": sum(b.node_count for b in workspace.batches),
        "stagedEdgeCount": sum(b.edge_count for b in workspace.batches),
        "skippedChunks": len(workspace.skipped_chunk_indexes),
        "warningCount": len(workspace.ingestion_warnings),
    }
