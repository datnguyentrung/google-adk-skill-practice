"""Phase 6 — Facade `IngestionUseCase` cho tầng tool/agent.

Module này giữ interface ổn định cho phần còn lại của ứng dụng: một đối tượng duy
nhất bọc các bước ingestion (status, chạy end-to-end, validate patch, fill patch).
Mọi chi tiết về batch, workspace, validate và persistence nằm ở các module khác
trong package."""

from typing import Any
from app.core.schemas.ingestion.graph_patch import GraphPatchDraft

from app.services.ingestion.orchestration.state import (
    DEFAULT_MAX_RETRIES_PER_BATCH,
    IngestionRuntime,
)
from app.services.ingestion.orchestration.tools import (
    fill_graph_patch,
    get_ingestion_status,
    ingest_document_end_to_end,
    validate_graph_patch,
)


class IngestionUseCase:
    """
    Facade bọc các bước ingestion cho tầng tool/agent.
    """

    def status(
        self,
        ingestion_id: str,
        runtime: IngestionRuntime,
    ) -> dict[str, Any]:
        """
        Trả về trạng thái workspace của một phiên ingestion.
        """
        return get_ingestion_status(ingestion_id, runtime)

    async def ingest_end_to_end(
        self,
        artifact_name: str,
        runtime: IngestionRuntime,
        *,
        persist: bool = True,
        allow_partial_persistence: bool = False,
        max_retries_per_batch: int = DEFAULT_MAX_RETRIES_PER_BATCH,
    ) -> dict[str, Any]:
        """
        Chạy trọn pipeline ingestion cho một tài liệu.
        """
        return await ingest_document_end_to_end(
            artifact_name,
            runtime,
            persist=persist,
            allow_partial_persistence=allow_partial_persistence,
            max_retries_per_batch=max_retries_per_batch,
        )

    def validate_patch(
        self,
        graph_patch: GraphPatchDraft,
        runtime: IngestionRuntime,
    ) -> dict[str, Any]:
        """
        Kiểm định một graph patch mà không ghi dữ liệu.
        """
        return validate_graph_patch(graph_patch, runtime)

    async def fill_patch(
        self,
        graph_patch: GraphPatchDraft,
        runtime: IngestionRuntime,
    ) -> dict[str, Any]:
        """
        Ghi một graph patch đã được cấp quyền.
        """
        return await fill_graph_patch(graph_patch, runtime)
