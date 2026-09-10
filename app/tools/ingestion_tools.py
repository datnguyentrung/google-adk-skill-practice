from __future__ import annotations

from functools import lru_cache
from typing import Any, Literal

from google.adk.tools import ToolContext

from app.core.schemas.ingestion.graph_patch import GraphPatchDraft
from app.services.ingestion.orchestration import (
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


async def update_document(
    artifact_name: str,
    tool_context: ToolContext,
    persist: bool = True,
    allow_partial_persistence: bool = False,
    max_retries_per_batch: int = DEFAULT_MAX_RETRIES_PER_BATCH,
    if_missing: Literal["error", "ingest"] = "error",
) -> dict[str, Any]:
    """Incrementally update a logical Markdown source."""
    return await _get_use_case().update_document(
        artifact_name,
        tool_context,
        persist=persist,
        allow_partial_persistence=allow_partial_persistence,
        max_retries_per_batch=max_retries_per_batch,
        if_missing=if_missing,
    )


def delete_document(
    artifact_name: str,
    if_missing: Literal["error", "ignore"] = "error",
) -> dict[str, Any]:
    """Delete current source ownership and cleanup only unowned graph facts."""
    return _get_use_case().delete_document(artifact_name, if_missing=if_missing)


async def apply_changes(
    added: list[str],
    modified: list[str],
    deleted: list[str],
    tool_context: ToolContext,
    persist: bool = True,
    allow_partial_persistence: bool = False,
    max_retries_per_batch: int = DEFAULT_MAX_RETRIES_PER_BATCH,
) -> dict[str, Any]:
    """Apply added/modified/deleted Markdown sources in one operation."""
    return await _get_use_case().apply_changes(
        added=added,
        modified=modified,
        deleted=deleted,
        runtime=tool_context,
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
    "update_document": update_document,
    "delete_document": delete_document,
    "apply_changes": apply_changes,
    "get_ingestion_status": get_ingestion_status,
    "validate_graph_patch": validate_graph_patch,
    "fill_graph_patch": fill_graph_patch,
}


def get_ingestion_tools() -> list:
    return list(INGESTION_TOOLS.values())
