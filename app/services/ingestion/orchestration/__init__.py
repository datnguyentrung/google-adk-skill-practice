"""Phase 6 — Điều phối pipeline ingestion và các bước dành cho tool/agent.

Package này gom trạng thái phiên (`state`), phân loại lỗi (`errors`), ngữ cảnh giữa
các batch (`context`), thống kê (`stats`), sửa coverage (`coverage`), ghi kèm biên
lai (`receipts`) và các bước entry point (`tools`). `IngestionUseCase` là facade cho
tầng tool/agent.
"""

from app.services.ingestion.orchestration.state import (
    DEFAULT_MAX_RETRIES_PER_BATCH,
    IngestionRuntime,
)
from app.services.ingestion.orchestration.tools import (
    apply_ingestion_changes,
    begin_ingestion,
    delete_ingestion_document,
    fill_graph_patch,
    fill_ingestion,
    finalize_ingestion,
    get_ingestion_status,
    ingest_document_end_to_end,
    prepare_extraction_context,
    submit_ingestion_batch,
    update_ingestion_document,
    validate_graph_patch,
)
from app.services.ingestion.orchestration.use_case import IngestionUseCase

__all__ = [
    "DEFAULT_MAX_RETRIES_PER_BATCH",
    "IngestionRuntime",
    "IngestionUseCase",
    "apply_ingestion_changes",
    "begin_ingestion",
    "delete_ingestion_document",
    "fill_graph_patch",
    "fill_ingestion",
    "finalize_ingestion",
    "get_ingestion_status",
    "ingest_document_end_to_end",
    "prepare_extraction_context",
    "submit_ingestion_batch",
    "update_ingestion_document",
    "validate_graph_patch",
]
