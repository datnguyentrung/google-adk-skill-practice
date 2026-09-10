"""Phase 6 — Ghi graph kèm 'biên lai' (receipt) và chuẩn hoá kết quả trả về.

Mỗi lần fill cần một bằng chứng cho biết đã ghi đúng dữ liệu: số node/relationship,
fingerprint, thời điểm ghi. Module này thực hiện bước ghi kèm receipt và chuyển kết
quả đánh giá của cổng validate thành payload trả về cho tool."""

import json
import logging
from collections.abc import Callable
from typing import Any

from google.genai import types

from app.core.schemas.ingestion.validation import ValidationIssue
from app.services.ingestion.orchestration.state import IngestionRuntime
from app.services.ingestion.persistence.service import (
    FillValidationError,
    create_graph_persistence,
)
from app.services.ingestion.validation.graph_validation import (
    GraphPatchAssessment,
    GraphValidation,
)

logger = logging.getLogger(__name__)


async def _receipt_response(
    result: dict[str, Any],
    *,
    artifact_stem: str,
    tool_context: IngestionRuntime,
    require_receipt: bool,
) -> dict[str, Any]:
    """
    Bọc kết quả fill thành response có receipt và lưu receipt vào artifact.

    Args:
        result: Kết quả của bước fill.
        artifact_stem: Tên artifact dùng để đặt tên receipt.
        tool_context: Tool context chứa state và API lưu artifact.
        require_receipt: Có bắt buộc phải lưu receipt hay không.
    """
    receipt = result.get("receipt")
    partial_persistence = bool(result.get("partialPersistence", False))
    persistence_mode = result.get("persistenceMode", "strict")
    readiness_issues_ignored = result.get("readinessIssuesIgnored", [])
    if not isinstance(receipt, dict):
        if require_receipt:
            raise RuntimeError("Fill service did not return a persisted graph receipt")
        return {
            "success": True,
            "stage": "completed",
            "terminal": True,
            "partialPersistence": partial_persistence,
            "persistenceMode": persistence_mode,
            "readinessIssuesIgnored": readiness_issues_ignored,
            **result,
        }
    artifact_name = f"ingestion-receipt-{artifact_stem}.json"
    artifact_version = await tool_context.save_artifact(
        filename=artifact_name,
        artifact=types.Part(
            text=json.dumps(
                receipt,
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        ),
        custom_metadata={
            "receiptVersion": str(receipt.get("version", "1")),
            "commitStatus": str(result.get("commitStatus", "committed")),
            "partialPersistence": str(partial_persistence).lower(),
            "persistenceMode": str(persistence_mode),
            "readinessIssuesIgnored": readiness_issues_ignored,
            "documentId": str(result.get("documentId") or ""),
            "sourceVersionId": str(result.get("sourceVersionId") or ""),
            "sourceVersionStatus": str(result.get("sourceVersionStatus") or ""),
        },
    )
    commit_status = result.get("commitStatus")
    committed = commit_status == "committed"
    verified = bool(receipt.get("verified"))
    persisted_nodes = int(result.get("nodes", 0) or 0)
    success = committed and verified and persisted_nodes > 0
    stage = "completed" if success else ("readback" if committed else "persistence")
    return {
        "success": success,
        "stage": stage,
        "terminal": True,
        "commitStatus": commit_status,
        "partialPersistence": partial_persistence,
        "persistenceMode": persistence_mode,
        "readinessIssuesIgnored": readiness_issues_ignored,
        "nodes": persisted_nodes,
        "edges": result.get("edges", 0),
        "labelDistribution": receipt.get("labelDistribution", {}),
        "relationshipTypeDistribution": receipt.get(
            "relationshipTypeDistribution",
            {},
        ),
        "verificationStatus": "verified" if verified else "mismatch",
        "mismatchCount": len(receipt.get("mismatches", [])),
        "artifactName": artifact_name,
        "artifactVersion": artifact_version,
        "documentId": result.get("documentId"),
        "sourceVersionId": result.get("sourceVersionId"),
        "sourceVersionStatus": result.get("sourceVersionStatus"),
    }


async def _persist_with_receipt(
    *,
    graph_patch,
    artifact_digest: str | None,
    source_chunks,
    validation_service: GraphValidation,
    artifact_stem: str,
    tool_context: IngestionRuntime,
    require_receipt: bool,
    invalidate_gate: Callable[[], None],
    failure_message: str,
    allow_partial_persistence: bool = False,
    source_lifecycle=None,
    extraction_cache_entries=None,
) -> dict[str, Any]:
    """
    Ghi patch xuống Neo4j kèm receipt và xử lý khi bước ghi thất bại.

    Args:
        graph_patch: Patch sẽ ghi.
        artifact_digest: Digest artifact nguồn.
        source_chunks: Chunk nguồn dùng để validate.
        validation_service: Cổng kiểm định graph patch.
        artifact_stem: Tên artifact dùng để đặt tên receipt.
        tool_context: Tool context của phiên.
        require_receipt: Có bắt buộc phải lưu receipt hay không.
        invalidate_gate: Hàm xoá fingerprint đã validate khi ghi thất bại.
        failure_message: Message lỗi hiển thị khi ghi thất bại.
        allow_partial_persistence: Cho phép ghi một phần.
    """
    service = None
    result = None
    try:
        logger.info(
            "[PHASE:PERSIST_WITH_RECEIPT_START] Func: _persist_with_receipt | Stem: %s | RequireReceipt: %s | AllowPartial: %s",
            artifact_stem,
            require_receipt,
            allow_partial_persistence,
        )
        service = create_graph_persistence(validation=validation_service)
        fill_kwargs = (
            {"allow_partial_persistence": True} if allow_partial_persistence else {}
        )
        if source_lifecycle is not None:
            fill_kwargs["source_lifecycle"] = source_lifecycle
            fill_kwargs["extraction_cache_entries"] = extraction_cache_entries or []
        result = service.fill(
            graph_patch, artifact_digest, source_chunks, **fill_kwargs
        )
        logger.info(
            "[PHASE:PERSIST_WITH_RECEIPT_SUCCESS] Func: _persist_with_receipt | Stem: %s | Nodes: %s | Edges: %s | Status: %s | CommitStatus: %s",
            artifact_stem,
            result.get("nodes", 0),
            result.get("edges", 0),
            result.get("status"),
            result.get("commitStatus"),
        )
        return await _receipt_response(
            result,
            artifact_stem=artifact_stem,
            tool_context=tool_context,
            require_receipt=require_receipt,
        )
    except FillValidationError as exc:
        invalidate_gate()
        logger.exception(
            "[INGESTION_ERROR] Phase: PERSIST_WITH_RECEIPT | Func: _persist_with_receipt | Stem: %s | Fill validation failed",
            artifact_stem,
        )
        return {
            "success": False,
            "stage": "validation",
            "terminal": True,
            "validation": exc.result.model_dump(by_alias=True, exclude_none=True),
        }
    except Exception as exc:
        logger.exception(
            "[INGESTION_ERROR] Phase: PERSIST_WITH_RECEIPT | Func: _persist_with_receipt | Stem: %s | Message: %s",
            artifact_stem,
            failure_message,
        )
        if isinstance(result, dict) and result.get("commitStatus") == "committed":
            return {
                "success": False,
                "stage": "receipt_artifact",
                "terminal": True,
                "commitStatus": "committed",
                "errors": [
                    ValidationIssue(
                        code="RECEIPT_ARTIFACT_FAILED",
                        message=str(exc),
                        location="receipt",
                    ).model_dump(by_alias=True, exclude_none=True)
                ],
            }
        return {
            "success": False,
            "stage": "persistence",
            "terminal": True,
            "errors": [
                ValidationIssue(
                    code="NEO4J_WRITE_FAILED",
                    message=str(exc),
                    location="persistence",
                ).model_dump(by_alias=True, exclude_none=True)
            ],
        }
    finally:
        if service is not None:
            service.close()


def _public_assessment(assessment: GraphPatchAssessment) -> dict[str, Any]:
    """
    Chuyển `GraphPatchAssessment` thành payload trả về cho tool.
    """
    return assessment.result.model_dump(by_alias=True, exclude_none=True)
