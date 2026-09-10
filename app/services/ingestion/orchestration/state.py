"""Phase 6 — Trạng thái phiên ingestion và cách truy cập service dùng chung.

Module này giữ các khoá session state, hợp đồng `IngestionRuntime` mà tool context
phải thoả, và các hàm đọc/ghi workspace trong state. Ngoài ra đây là nơi dựng (và
cache) các service dùng chung như DocumentPreparation, GraphValidation,
IngestionWorkspaceService và AdkGraphMapper."""

import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol

from app.core.schemas.ingestion.validation import ValidationIssue
from app.core.schemas.ingestion.workspace import (
    IngestionProvenance,
    IngestionWorkspace,
)
from app.services.ingestion.document.preparation import DocumentPreparation
from app.services.ingestion.mapping.graph_mapper import (
    AdkGraphMapper,
)
from app.services.ingestion.validation.graph_validation import (
    GraphValidation,
)
from app.services.ingestion.validation.semantic_judge import (
    create_default_semantic_grounding_judge,
)
from app.services.ingestion.workspace.staged_ingestion import (
    IngestionWorkspaceService,
)
from app.skills.skill_loader import skill_content_digest

logger = logging.getLogger(__name__)


class IngestionRuntime(Protocol):
    """
    Hợp đồng tối thiểu của tool context: có `state` và đọc/ghi được artifact.
    """

    state: dict[str, Any]

    async def load_artifact(self, filename: str): ...

    async def save_artifact(self, filename: str, artifact, **kwargs): ...


ARTIFACT_DIGEST_STATE_KEY = "temp:ingestion_source_artifact_digest"


ARTIFACT_NAME_STATE_KEY = "temp:ingestion_source_artifact_name"


VALIDATED_FINGERPRINT_STATE_KEY = "temp:ingestion_validated_fingerprint"


SOURCE_CHUNKS_STATE_KEY = "temp:ingestion_source_chunks"


WORKSPACE_STATE_KEY = "temp:ingestion_workspace"


DEFAULT_MAX_RETRIES_PER_BATCH = max(
    1, int(os.getenv("INGESTION_MAX_RETRIES_PER_BATCH", "3"))
)


GRAPH_CONTEXT_MAX_CHARS = 6000


FINAL_COVERAGE_REPAIR_ROUNDS = max(
    1, int(os.getenv("INGESTION_FINAL_COVERAGE_REPAIR_ROUNDS", "2"))
)


INGESTION_SKILL_DIR = Path(__file__).resolve().parents[2] / "skills" / "ingestion"


@lru_cache(maxsize=1)
def _get_context_service() -> DocumentPreparation:
    """
    Lấy (và cache) service chuẩn bị ngữ cảnh extraction.
    """
    return DocumentPreparation()


@lru_cache(maxsize=1)
def _get_validation_service() -> GraphValidation:
    """
    Lấy (và cache) cổng kiểm định graph patch.
    """
    return GraphValidation(
        semantic_grounding_judge=create_default_semantic_grounding_judge()
    )


@lru_cache(maxsize=1)
def _get_workspace_service() -> IngestionWorkspaceService:
    """
    Lấy (và cache) service quản lý workspace ingestion.
    """
    return IngestionWorkspaceService()


def _get_graph_mapper() -> AdkGraphMapper:
    """
    Lấy (và cache) mapper gọi LLM trích xuất graph patch.
    """
    validation_service = _get_validation_service()
    return AdkGraphMapper(
        registry=validation_service.validator.registry,
        compiler=validation_service.compiler,
        ontology_validator=validation_service.validator,
    )


def _delete_state(tool_context: IngestionRuntime, key: str) -> None:
    """
    Xoá một khoá khỏi session state nếu đang tồn tại.
    """
    if key in tool_context.state:
        # google.adk.sessions.state.State intentionally has no __delitem__.
        # ADK uses None as the state-delta tombstone for removing a key.
        tool_context.state[key] = None


def _clear_validation_gate(tool_context: IngestionRuntime) -> None:
    """
    Xoá fingerprint đã validate để buộc validate lại.
    """
    _delete_state(tool_context, VALIDATED_FINGERPRINT_STATE_KEY)


def _skill_digest() -> str:
    """
    Tính digest nội dung skill ingestion hiện tại.
    """
    return skill_content_digest(INGESTION_SKILL_DIR)


def _current_provenance(tool_context: IngestionRuntime) -> IngestionProvenance:
    """
    Dựng provenance của phiên hiện tại (artifact, ontology, skill).
    """
    return IngestionProvenance(
        artifactDigest=tool_context.state.get(
            ARTIFACT_DIGEST_STATE_KEY,
            "MISSING",
        ),
        ontologyDigest=_get_validation_service().compiler.ontology_digest,
        skillDigest=_skill_digest(),
    )


def _load_workspace(tool_context: IngestionRuntime) -> IngestionWorkspace | None:
    """
    Đọc workspace hiện tại từ session state.
    """
    raw = tool_context.state.get(WORKSPACE_STATE_KEY)
    if raw is None:
        return None
    return IngestionWorkspace.model_validate(raw)


def _store_workspace(
    tool_context: IngestionRuntime,
    workspace: IngestionWorkspace,
) -> None:
    """
    Ghi workspace vào session state.
    """
    tool_context.state[WORKSPACE_STATE_KEY] = workspace.model_dump(
        by_alias=True,
        mode="json",
    )


def _workspace_precondition(
    ingestion_id: str,
    tool_context: IngestionRuntime,
) -> tuple[IngestionWorkspace | None, dict[str, Any] | None]:
    """
    Kiểm tra workspace tồn tại và hợp lệ trước khi thực hiện thao tác.

    Returns:
        Bộ đôi `(workspace, lỗi)`: một trong hai luôn là None.
    """
    workspace = _load_workspace(tool_context)
    if workspace is None or workspace.ingestion_id != ingestion_id:
        return None, {
            "success": False,
            "stage": "workspace_precondition",
            "errors": [
                ValidationIssue(
                    code="WORKSPACE_PRECONDITION",
                    message="begin_ingestion must create the requested workspace first",
                    location="ingestionId",
                ).model_dump(by_alias=True, exclude_none=True)
            ],
        }
    current = _get_workspace_service().is_current(
        workspace,
        provenance=_current_provenance(tool_context),
    )
    if not current:
        workspace.validated_fingerprint = None
        _store_workspace(tool_context, workspace)
        return None, {
            "success": False,
            "stage": "workspace_precondition",
            "errors": [
                ValidationIssue(
                    code="WORKSPACE_PRECONDITION",
                    message=(
                        "Artifact, ontology, or ingestion skill changed; restart "
                        "with begin_ingestion"
                    ),
                    location="ingestionId",
                ).model_dump(by_alias=True, exclude_none=True)
            ],
        }
    return workspace, None
