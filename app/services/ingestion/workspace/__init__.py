"""Phase 2b — Chia batch và quản lý workspace ingestion theo phiên.

`IngestionWorkspaceService` chia tài liệu dài thành batch, nhận fragment của từng
batch và gộp dần thành một graph patch duy nhất.
"""

from app.services.ingestion.workspace.staged_ingestion import (
    ESTIMATED_CHARS_PER_TOKEN,
    MAX_BATCH_CHARS,
    MAX_BATCH_CHUNKS,
    MAX_BATCH_ESTIMATED_TOKENS,
    IngestionWorkspaceService,
    WorkspaceConflictError,
)

__all__ = [
    "ESTIMATED_CHARS_PER_TOKEN",
    "IngestionWorkspaceService",
    "MAX_BATCH_CHARS",
    "MAX_BATCH_CHUNKS",
    "MAX_BATCH_ESTIMATED_TOKENS",
    "WorkspaceConflictError",
]
