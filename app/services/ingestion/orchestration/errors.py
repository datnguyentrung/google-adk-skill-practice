"""Phase 6 — Phân loại lỗi của pipeline ingestion.

Khi gọi model hoặc Neo4j thất bại, tầng orchestration cần biết lỗi đó là cấu hình
sai, hết quota hay lỗi tạm thời để quyết định thử lại hay dừng hẳn. Module này gom
các hàm phân loại và chuẩn hoá thông tin lỗi trả về cho caller."""

from typing import Any

from app.services.ingestion.mapping.graph_mapper import DirectGraphMappingError
from app.services.ingestion.validation.graph_validation import (
    InvalidGraphPatchFragmentError,
)


def _orchestration_error_kind(exc: Exception) -> str:
    """
    Suy ra loại lỗi (cấu hình, rate limit, hạ tầng...) từ exception.
    """
    error_kind = getattr(exc, "error_kind", None)
    if isinstance(error_kind, str):
        return error_kind
    if _is_extractor_configuration_error(exc):
        return "llm_request_config"
    message = str(exc)
    if "response_schema" in message and "Invalid JSON payload" in message:
        return "llm_request_config"
    return "llm_extraction"


def _is_extractor_configuration_error(exc: Exception) -> bool:
    """
    Cho biết lỗi có phải do cấu hình extractor sai (không thử lại được).
    """

    status_code = getattr(exc, "status_code", None)
    if status_code == 400:
        return True
    message = str(exc)
    return any(
        token in message
        for token in (
            "INVALID_ARGUMENT",
            "Unknown name",
            "response_schema",
            "responseJsonSchema",
        )
    )


def _is_rate_limit_error(exc: Exception) -> bool:
    """
    Cho biết lỗi có phải do vượt hạn mức model (nên chờ và thử lại).
    """
    message = str(exc).upper()
    status_code = getattr(exc, "status_code", None)
    return status_code == 429 or (
        "429" in message and ("RESOURCE_EXHAUSTED" in message or "QUOTA" in message)
    )


def _is_retryable_extraction_error(exc: Exception) -> bool:
    """
    Cho biết lỗi trích xuất có nên thử lại hay không.
    """
    if getattr(exc, "stage_local_retries_exhausted", False):
        return False
    return (
        isinstance(exc, (InvalidGraphPatchFragmentError, DirectGraphMappingError))
        or _is_rate_limit_error(exc)
        or bool(getattr(exc, "retryable", False))
    )


def _extractor_retry_error(exc: Exception) -> dict[str, Any]:
    """
    Dựng payload lỗi chuẩn để gửi lại cho model ở lần thử sau.
    """
    return {
        "stage": "direct_graph_mapping",
        "errorKind": _orchestration_error_kind(exc),
        "message": _orchestration_error_message(exc),
        "validation": getattr(exc, "summary", {}),
        "repairInstructions": (
            "Repair candidateFragment minimally; do not remap the batch from scratch. Preserve all "
            "valid nodes, edges, coverage, identities and evidence that are unrelated to the reported "
            "error. For SOURCE_LITERAL_NOT_GROUNDED, use one contiguous verbatim value from cited "
            "evidence or omit the optional property/node; never synthesize, summarize, join rows, "
            "or switch to another class merely to escape validation. For EVIDENCE_NOT_VERBATIM, "
            "copy an exact source substring."
        ),
    }


def _orchestration_error_message(exc: Exception) -> str:
    """
    Chuẩn hoá message lỗi trước khi trả về cho caller.
    """
    if _orchestration_error_kind(exc) == "llm_request_config":
        return (
            "Internal Gemini extractor request configuration is invalid; "
            "the uploaded document was not processed and no graph was persisted"
        )
    return str(exc)
