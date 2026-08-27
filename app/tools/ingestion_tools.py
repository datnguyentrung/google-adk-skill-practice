from __future__ import annotations

from functools import lru_cache
from typing import Any

from google.adk.tools import ToolContext

from app.core.schemas.ingestion.graph_patch import GraphPatchDraft
from app.services.ingestion.use_case import (
    DEFAULT_MAX_RETRIES_PER_BATCH,
    IngestionUseCase,
)


@lru_cache(maxsize=1)
def _get_use_case() -> IngestionUseCase:
    return IngestionUseCase()


async def ingest_document_end_to_end(
    artifact_name: str,
    tool_context: ToolContext,
    persist: bool = True,
    allow_partial_persistence: bool = False,
    max_retries_per_batch: int = DEFAULT_MAX_RETRIES_PER_BATCH,
) -> dict[str, Any]:
    """Run long-document ingestion to a real terminal state in one tool call."""

    return await _get_use_case().ingest_end_to_end(
        artifact_name,
        tool_context,
        persist=persist,
        allow_partial_persistence=allow_partial_persistence,
        max_retries_per_batch=max_retries_per_batch,
    )


def get_ingestion_status(
    ingestion_id: str,
    tool_context: ToolContext,
) -> dict[str, Any]:
    """Return the current terminal/non-terminal ingestion workspace state."""

    return _get_use_case().status(ingestion_id, tool_context)


def validate_graph_patch(
    graph_patch: GraphPatchDraft,
    tool_context: ToolContext,
) -> dict[str, Any]:
    """Assess extraction correctness and persistence readiness without writing."""

    return _get_use_case().validate_patch(graph_patch, tool_context)


async def fill_graph_patch(
    graph_patch: GraphPatchDraft,
    tool_context: ToolContext,
) -> dict[str, Any]:
    """Persist only the invocation-scoped, validated graph patch."""

    return await _get_use_case().fill_patch(graph_patch, tool_context)


INGESTION_TOOLS = {
    "ingest_document_end_to_end": ingest_document_end_to_end,
    "get_ingestion_status": get_ingestion_status,
    "validate_graph_patch": validate_graph_patch,
    "fill_graph_patch": fill_graph_patch,
}


def get_ingestion_tools() -> list:
    return list(INGESTION_TOOLS.values())
