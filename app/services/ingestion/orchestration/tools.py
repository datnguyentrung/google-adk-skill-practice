"""Phase 6 — Các bước chạy ingestion mà tầng tool/agent gọi vào.

Module này chứa các hàm entry point của pipeline: chuẩn bị ngữ cảnh, bắt đầu phiên,
submit từng batch, finalize, fill, kiểm tra trạng thái, validate patch và chạy một
mạch end-to-end. Mỗi hàm chịu trách nhiệm ghi log theo phase, dựng thông báo lỗi
thống nhất và cập nhật session state."""

import hashlib
import logging
from typing import Any
from app.core.schemas.ingestion.document import DocumentChunk
from app.core.schemas.ingestion.graph_patch import GraphPatchDraft, GraphPatchFragment
from app.core.schemas.ingestion.validation import ValidationIssue

from app.services.ingestion.orchestration.context import (
    _batch_payload,
    _canonical_graph_context,
)
from app.services.ingestion.orchestration.coverage import (
    _final_coverage_errors_by_batch,
    _repair_final_coverage_batches,
)
from app.services.ingestion.orchestration.errors import (
    _extractor_retry_error,
    _is_retryable_extraction_error,
    _orchestration_error_kind,
    _orchestration_error_message,
)
from app.services.ingestion.orchestration.receipts import (
    _persist_with_receipt,
    _public_assessment,
)
from app.services.ingestion.orchestration.state import (
    ARTIFACT_DIGEST_STATE_KEY,
    ARTIFACT_NAME_STATE_KEY,
    DEFAULT_MAX_RETRIES_PER_BATCH,
    FINAL_COVERAGE_REPAIR_ROUNDS,
    IngestionRuntime,
    SOURCE_CHUNKS_STATE_KEY,
    VALIDATED_FINGERPRINT_STATE_KEY,
    WORKSPACE_STATE_KEY,
    _clear_validation_gate,
    _current_provenance,
    _delete_state,
    _get_context_service,
    _get_graph_mapper,
    _get_validation_service,
    _get_workspace_service,
    _load_workspace,
    _store_workspace,
    _workspace_precondition,
)
from app.services.ingestion.orchestration.stats import (
    _batch_stats,
    _fragment_stats,
    _workspace_stats,
)
from app.services.ingestion.workspace.staged_ingestion import WorkspaceConflictError


logger = logging.getLogger(__name__)


async def prepare_extraction_context(
    artifact_name: str,
    tool_context: IngestionRuntime,
) -> dict[str, Any]:
    """
    Đọc artifact nguồn và chuẩn bị ngữ cảnh extraction, lưu vào session state.

    Args:
        artifact_name: Tên artifact tài liệu nguồn.
        tool_context: Tool context của phiên.

    Returns:
        Payload mô tả nguồn, số chunk và phần ontology rút gọn.
    """
    logger.info(
        "[PHASE:PREPARE_CONTEXT_START] Func: prepare_extraction_context | Document: '%s'",
        artifact_name,
    )
    _clear_validation_gate(tool_context)
    _delete_state(tool_context, ARTIFACT_DIGEST_STATE_KEY)
    _delete_state(tool_context, ARTIFACT_NAME_STATE_KEY)
    _delete_state(tool_context, SOURCE_CHUNKS_STATE_KEY)
    _delete_state(tool_context, WORKSPACE_STATE_KEY)

    try:
        artifact = await tool_context.load_artifact(filename=artifact_name)
        if artifact is None:
            logger.error(
                "[INGESTION_ERROR] Phase: PREPARE_CONTEXT | Func: prepare_extraction_context | "
                "Document: '%s' | Error: Artifact not found",
                artifact_name,
            )
            return {
                "success": False,
                "stage": "artifact_loading",
                "error": f"Artifact not found: {artifact_name}",
            }

        data: bytes
        mime_type: str | None = None
        if artifact.inline_data is not None:
            raw_data = artifact.inline_data.data
            mime_type = artifact.inline_data.mime_type
            if raw_data is None:
                logger.error(
                    "[INGESTION_ERROR] Phase: PREPARE_CONTEXT | Func: prepare_extraction_context | "
                    "Document: '%s' | Error: Artifact contains no binary data",
                    artifact_name,
                )
                return {
                    "success": False,
                    "stage": "artifact_loading",
                    "error": f"Artifact contains no binary data: {artifact_name}",
                }
            if isinstance(raw_data, bytes):
                data = raw_data
            elif isinstance(raw_data, bytearray):
                data = bytes(raw_data)
            else:
                logger.error(
                    "[INGESTION_ERROR] Phase: PREPARE_CONTEXT | Func: prepare_extraction_context | "
                    "Document: '%s' | Error: Unsupported artifact data type %s",
                    artifact_name,
                    type(raw_data).__name__,
                )
                return {
                    "success": False,
                    "stage": "artifact_loading",
                    "error": (
                        f"Unsupported artifact data type: {type(raw_data).__name__}"
                    ),
                }
        elif artifact.text is not None:
            data = artifact.text.encode("utf-8")
            mime_type = "text/plain"
        else:
            logger.error(
                "[INGESTION_ERROR] Phase: PREPARE_CONTEXT | Func: prepare_extraction_context | "
                "Document: '%s' | Error: Artifact does not contain supported inline data or text",
                artifact_name,
            )
            return {
                "success": False,
                "stage": "artifact_loading",
                "error": (
                    "Artifact does not contain supported inline data or text: "
                    f"{artifact_name}"
                ),
            }

        artifact_digest = hashlib.sha256(data).hexdigest()
        tool_context.state[ARTIFACT_DIGEST_STATE_KEY] = artifact_digest
        tool_context.state[ARTIFACT_NAME_STATE_KEY] = artifact_name

        context = _get_context_service().prepare_uploaded_document(
            filename=artifact_name,
            data=data,
            mime_type=mime_type,
        )
        try:
            source_text = data.decode("utf-8")
            total_lines = len(source_text.splitlines())
            total_chars = len(source_text)
        except UnicodeDecodeError:
            total_lines = None
            total_chars = len(data)
        logger.info(
            "[PHASE:PREPARE_CONTEXT_SUCCESS] Func: prepare_extraction_context | "
            "Document: '%s' | TotalChars: %s | TotalLines: %s | Chunks: %s",
            artifact_name,
            total_chars,
            total_lines,
            len(context.chunks),
        )
        tool_context.state[SOURCE_CHUNKS_STATE_KEY] = [
            chunk.model_dump(by_alias=True, exclude_none=True)
            for chunk in context.chunks
        ]
        return {
            "success": True,
            "stage": "completed",
            "chunkCount": len(context.chunks),
            **context.model_dump(by_alias=True, exclude_none=True),
        }
    except Exception as exc:
        logger.error(
            "[INGESTION_ERROR] Phase: PREPARE_CONTEXT | Func: prepare_extraction_context | "
            "Document: '%s' | Exception: %s",
            artifact_name,
            exc,
            exc_info=True,
        )
        _clear_validation_gate(tool_context)
        _delete_state(tool_context, ARTIFACT_DIGEST_STATE_KEY)
        _delete_state(tool_context, ARTIFACT_NAME_STATE_KEY)
        _delete_state(tool_context, SOURCE_CHUNKS_STATE_KEY)
        return {
            "success": False,
            "stage": "prepare_extraction_context",
            "error": str(exc),
        }


async def begin_ingestion(
    artifact_name: str,
    tool_context: IngestionRuntime,
) -> dict[str, Any]:
    """
    Bắt đầu một phiên ingestion: chuẩn bị ngữ cảnh rồi tạo workspace chia batch.

    Args:
        artifact_name: Tên artifact tài liệu nguồn.
        tool_context: Tool context của phiên.

    Returns:
        Payload gồm ingestion id, batch đầu tiên và thống kê workspace.
    """
    logger.info(
        "[PHASE:BEGIN_INGESTION_START] Func: begin_ingestion | Document: '%s'",
        artifact_name,
    )
    prepared = await prepare_extraction_context(artifact_name, tool_context)
    if not prepared.get("success"):
        logger.error(
            "[INGESTION_ERROR] Phase: BEGIN_INGESTION | Func: begin_ingestion | "
            "Document: '%s' | Error: Context preparation failed: %s",
            artifact_name,
            prepared.get("error"),
        )
        return prepared
    workspace = _get_workspace_service().begin(
        artifact_name=artifact_name,
        provenance=_current_provenance(tool_context),
        chunks=[
            DocumentChunk.model_validate(item)
            for item in tool_context.state[SOURCE_CHUNKS_STATE_KEY]
        ],
    )
    _store_workspace(tool_context, workspace)
    first_batch = workspace.batches[0]
    logger.info(
        "[PHASE:BEGIN_INGESTION_SUCCESS] Func: begin_ingestion | Document: '%s' | "
        "IngestionID: %s | Chunks: %s | Batches: %s",
        artifact_name,
        workspace.ingestion_id,
        len(workspace.chunks),
        len(workspace.batches),
    )
    return {
        "success": True,
        "stage": "batching",
        "terminal": False,
        "ingestionId": workspace.ingestion_id,
        "chunkCount": len(workspace.chunks),
        "batchCount": len(workspace.batches),
        "documentStats": _workspace_stats(workspace),
        "nextBatchStats": _batch_stats(workspace, first_batch),
        "artifactDigest": workspace.artifact_digest,
        "ontologyDigest": workspace.ontology_digest,
        "skillDigest": workspace.skill_digest,
        "nextBatch": _batch_payload(workspace, first_batch),
    }


def submit_ingestion_batch(
    ingestion_id: str,
    batch_index: int,
    graph_fragment: GraphPatchFragment,
    tool_context: IngestionRuntime,
) -> dict[str, Any]:
    """
    Nhận fragment của một batch và ghi vào workspace.

    Args:
        ingestion_id: Mã phiên ingestion.
        batch_index: Chỉ số batch.
        graph_fragment: Fragment do LLM trả về.
        tool_context: Tool context của phiên.

    Returns:
        Payload trạng thái kèm batch kế tiếp (nếu còn).
    """
    workspace, error = _workspace_precondition(ingestion_id, tool_context)
    if error is not None or workspace is None:
        logger.error(
            "[INGESTION_ERROR] Phase: BATCH_SUBMIT | Func: submit_ingestion_batch | "
            "IngestionID: %s | Batch: %s | Error: Workspace precondition failed",
            ingestion_id,
            batch_index,
        )
        return error
    try:
        fragment = GraphPatchFragment.model_validate(graph_fragment)
        workspace = _get_workspace_service().submit(workspace, batch_index, fragment)
        workspace.retry_states.pop(str(batch_index), None)
    except (ValueError, WorkspaceConflictError) as exc:
        issue = ValidationIssue(
            code="BATCH_CONFLICT",
            message=str(exc),
            location=f"batches.{batch_index}",
        )
        logger.warning(
            "[INGESTION_ERROR] Phase: BATCH_SUBMIT | Func: submit_ingestion_batch | "
            "IngestionID: %s | Batch: %s | Conflict: %s",
            ingestion_id,
            batch_index,
            exc,
        )
        return {
            "success": False,
            "stage": "batch_validation",
            "terminal": False,
            "batchIndex": batch_index,
            "retryRequired": True,
            "nextAction": "remap_same_batch",
            "errors": [issue.model_dump(by_alias=True, exclude_none=True)],
            "conflict": getattr(exc, "conflict", {}),
        }

    _store_workspace(tool_context, workspace)
    next_batch = _get_workspace_service().next_batch(workspace)
    processed = sum(batch.fragment is not None for batch in workspace.batches)
    logger.info(
        "[PHASE:BATCH_SUBMIT_SUCCESS] Func: submit_ingestion_batch | IngestionID: %s | "
        "Batch: %s | Processed: %s/%s | Nodes: %s | Edges: %s",
        ingestion_id,
        batch_index,
        processed,
        len(workspace.batches),
        len(fragment.nodes),
        len(fragment.edges),
    )
    response = {
        "success": True,
        "stage": "batching" if next_batch is not None else "ready_to_finalize",
        "terminal": False,
        "ingestionId": ingestion_id,
        "processedBatches": processed,
        "remainingBatches": len(workspace.batches) - processed,
        "fragmentStats": _fragment_stats(fragment),
        "workspaceStats": _workspace_stats(workspace),
    }
    if next_batch is not None:
        response["nextBatch"] = _batch_payload(workspace, next_batch)
    return response


def finalize_ingestion(
    ingestion_id: str,
    tool_context: IngestionRuntime,
) -> dict[str, Any]:
    """
    Gộp toàn bộ fragment và kiểm tra patch cuối cùng trước khi fill.

    Args:
        ingestion_id: Mã phiên ingestion.
        tool_context: Tool context của phiên.

    Returns:
        Payload kết quả validate và fingerprint của patch (nếu đạt).
    """
    logger.info(
        "[PHASE:FINALIZE_START] Func: finalize_ingestion | IngestionID: %s",
        ingestion_id,
    )
    workspace, error = _workspace_precondition(ingestion_id, tool_context)
    if error is not None or workspace is None:
        logger.error(
            "[INGESTION_ERROR] Phase: FINALIZE | Func: finalize_ingestion | "
            "IngestionID: %s | Error: Workspace precondition failed",
            ingestion_id,
        )
        return error
    pending = [batch.index for batch in workspace.batches if batch.fragment is None]
    if pending:
        issue = ValidationIssue(
            code="BATCH_INCOMPLETE",
            message=f"Pending batch indexes: {pending}",
            location="batches",
        )
        logger.error(
            "[INGESTION_ERROR] Phase: FINALIZE | Func: finalize_ingestion | "
            "IngestionID: %s | Pending batches: %s",
            ingestion_id,
            pending,
        )
        return {
            "success": False,
            "stage": "batch_incomplete",
            "terminal": False,
            "errors": [issue.model_dump(by_alias=True, exclude_none=True)],
        }
    try:
        patch = _get_workspace_service().merged_patch(workspace)
    except ValueError as exc:
        issue = ValidationIssue(
            code="GRAPH_MAPPING_UNSUPPORTED",
            message=str(exc),
            location="graphPatch",
        )
        logger.error(
            "[INGESTION_ERROR] Phase: FINALIZE | Func: finalize_ingestion | "
            "IngestionID: %s | Error: Graph patch merge failed: %s",
            ingestion_id,
            exc,
            exc_info=True,
        )
        return {
            "success": False,
            "stage": "validation",
            "terminal": True,
            "errors": [issue.model_dump(by_alias=True, exclude_none=True)],
        }

    assessment = _get_validation_service().assess(
        patch,
        workspace.artifact_digest,
        workspace.chunks,
    )
    workspace.finalized_patch = (
        patch.model_dump(by_alias=True, mode="json")
        if assessment.result.valid_for_extraction
        else None
    )
    workspace.validated_fingerprint = (
        assessment.fingerprint
        if assessment.result.valid_for_persistence
        else None
    )
    _store_workspace(tool_context, workspace)
    public = _public_assessment(assessment)
    stage = (
        "ready_to_fill"
        if assessment.result.valid_for_persistence
        else "readiness_gate"
        if assessment.result.valid_for_extraction
        else "validation"
    )
    logger.info(
        "[PHASE:FINALIZE_SUCCESS] Func: finalize_ingestion | IngestionID: %s | Stage: %s | "
        "ValidExtraction: %s | ValidPersistence: %s | Fingerprint: %s",
        ingestion_id,
        stage,
        assessment.result.valid_for_extraction,
        assessment.result.valid_for_persistence,
        assessment.fingerprint,
    )
    return {
        "success": assessment.result.valid_for_extraction,
        "stage": stage,
        "terminal": not assessment.result.valid_for_persistence,
        "ingestionId": ingestion_id,
        **public,
        "artifactDigest": workspace.artifact_digest,
        "ontologyDigest": workspace.ontology_digest,
        "skillDigest": workspace.skill_digest,
        "workspaceStats": _workspace_stats(workspace),
    }


async def fill_ingestion(
    ingestion_id: str,
    tool_context: IngestionRuntime,
    allow_partial_persistence: bool = False,
) -> dict[str, Any]:
    """
    Ghi patch đã finalize xuống Neo4j kèm receipt.

    Args:
        ingestion_id: Mã phiên ingestion.
        tool_context: Tool context của phiên.
        allow_partial_persistence: Cho phép ghi một phần khi patch chưa đầy đủ.

    Returns:
        Payload kết quả ghi kèm receipt.
    """
    logger.info(
        "[PHASE:FILL_START] Func: fill_ingestion | IngestionID: %s | AllowPartial: %s",
        ingestion_id,
        allow_partial_persistence,
    )
    workspace, error = _workspace_precondition(ingestion_id, tool_context)
    if error is not None or workspace is None:
        logger.error(
            "[INGESTION_ERROR] Phase: FILL | Func: fill_ingestion | IngestionID: %s | Error: Workspace precondition failed",
            ingestion_id,
        )
        return error
    if workspace.finalized_patch is None:
        logger.error(
            "[INGESTION_ERROR] Phase: FILL | Func: fill_ingestion | IngestionID: %s | Error: finalize_ingestion must run before fill_ingestion",
            ingestion_id,
        )
        return {"success": False, "stage": "validation_precondition", "terminal": True, "errors": [ValidationIssue(code="VALIDATION_PRECONDITION", message="finalize_ingestion must run before fill_ingestion", location="ingestionId").model_dump(by_alias=True, exclude_none=True)]}

    validation_service = _get_validation_service()
    assessment = validation_service.assess(workspace.finalized_patch, workspace.artifact_digest, workspace.chunks)
    if not assessment.result.valid_for_extraction or assessment.compiled_patch is None:
        logger.error(
            "[INGESTION_ERROR] Phase: FILL | Func: fill_ingestion | IngestionID: %s | Error: Finalized patch invalid for extraction",
            ingestion_id,
        )
        return {"success": False, "stage": "validation", "terminal": True, "validation": _public_assessment(assessment)}
    if not assessment.result.valid_for_persistence and not allow_partial_persistence:
        logger.error(
            "[INGESTION_ERROR] Phase: FILL | Func: fill_ingestion | IngestionID: %s | Error: Persistence readiness gate failed",
            ingestion_id,
        )
        return {"success": False, "stage": "validation_precondition", "terminal": True, "validation": _public_assessment(assessment), "errors": [ValidationIssue(code="VALIDATION_PRECONDITION", message="Persistence readiness failed; set allow_partial_persistence=true only for an explicit partial persistence commit requested by the user", location="ingestionId").model_dump(by_alias=True, exclude_none=True)]}

    expected_fingerprint = workspace.validated_fingerprint
    if allow_partial_persistence and not assessment.result.valid_for_persistence:
        expected_fingerprint = assessment.fingerprint
    if expected_fingerprint is None or assessment.fingerprint != expected_fingerprint:
        workspace.validated_fingerprint = None
        _store_workspace(tool_context, workspace)
        logger.error(
            "[INGESTION_ERROR] Phase: FILL | Func: fill_ingestion | IngestionID: %s | Error: Finalized graph fingerprint no longer matches",
            ingestion_id,
        )
        return {"success": False, "stage": "validation_precondition", "terminal": True, "errors": [ValidationIssue(code="VALIDATION_PRECONDITION", message="Finalized graph fingerprint no longer matches the validated extraction", location="ingestionId").model_dump(by_alias=True, exclude_none=True)]}

    def invalidate_workspace_gate() -> None:
        workspace.validated_fingerprint = None
        _store_workspace(tool_context, workspace)

    return await _persist_with_receipt(graph_patch=workspace.finalized_patch, artifact_digest=workspace.artifact_digest, source_chunks=workspace.chunks, validation_service=validation_service, artifact_stem=ingestion_id[:12], tool_context=tool_context, require_receipt=True, invalidate_gate=invalidate_workspace_gate, failure_message="Failed to persist finalized ingestion workspace", allow_partial_persistence=allow_partial_persistence)


def get_ingestion_status(
    ingestion_id: str,
    tool_context: IngestionRuntime,
) -> dict[str, Any]:
    """
    Trả về trạng thái hiện tại của một phiên ingestion.

    Args:
        ingestion_id: Mã phiên ingestion.
        tool_context: Tool context của phiên.
    """

    workspace, error = _workspace_precondition(ingestion_id, tool_context)
    if error is not None or workspace is None:
        return {**error, "terminal": True}
    next_batch = _get_workspace_service().next_batch(workspace)
    status = {
        "success": True,
        "stage": ("ready_to_finalize" if next_batch is None else "batching"),
        "terminal": False,
        "ingestionId": ingestion_id,
        "workspaceStats": _workspace_stats(workspace),
        "partial": bool(workspace.skipped_chunk_indexes),
        "skippedChunks": workspace.skipped_chunk_indexes,
        "ingestionWarnings": workspace.ingestion_warnings,
    }
    if next_batch is not None:
        status["nextBatch"] = _batch_payload(workspace, next_batch)
    return status


async def ingest_document_end_to_end(
    artifact_name: str,
    tool_context: IngestionRuntime,
    persist: bool = True,
    allow_partial_persistence: bool = False,
    max_retries_per_batch: int = DEFAULT_MAX_RETRIES_PER_BATCH,
) -> dict[str, Any]:
    """
    Chạy trọn pipeline ingestion cho một tài liệu trong một lần gọi.

    Args:
        artifact_name: Tên artifact tài liệu nguồn.
        tool_context: Tool context của phiên.
        persist: Có ghi xuống Neo4j hay chỉ dừng ở bước validate.
        allow_partial_persistence: Cho phép ghi một phần khi patch chưa đầy đủ.
        max_retries_per_batch: Số lần thử lại tối đa cho mỗi batch.

    Returns:
        Payload kết quả cuối cùng của phiên ingestion.
    """
    logger.info(
        "[PHASE:INGEST_END_TO_END_START] Func: ingest_document_end_to_end | Document: '%s' | Persist: %s | AllowPartial: %s",
        artifact_name,
        persist,
        allow_partial_persistence,
    )
    begin = await begin_ingestion(artifact_name, tool_context)
    if not begin.get("success"):
        logger.error(
            "[INGESTION_ERROR] Document '%s' failed at phase 'begin_ingestion'. Error: %s",
            artifact_name,
            begin.get("error"),
        )
        return {**begin, "terminal": True}

    ingestion_id = begin["ingestionId"]
    mapper = _get_graph_mapper()
    response: dict[str, Any] = begin

    while response.get("stage") == "batching":
        batch_payload = response.get("nextBatch")
        if not isinstance(batch_payload, dict):
            logger.error(
                "[INGESTION_ERROR] Document '%s' failed at phase 'batching'. Error: nextBatch payload missing",
                artifact_name,
            )
            return _terminal_mapping_failure(
                ingestion_id,
                -1,
                "Batching response did not include nextBatch",
                artifact_name=artifact_name,
            )
        batch_index = int(batch_payload["batchIndex"])
        workspace = _load_workspace(tool_context)
        graph_context = (
            _canonical_graph_context(workspace, batch_index) if workspace else ""
        )
        chunks = [
            DocumentChunk.model_validate(item)
            for item in batch_payload.get("chunks", [])
        ]
        previous_error: dict[str, Any] | None = None

        for attempt in range(1, max_retries_per_batch + 1):
            logger.info(
                "[PHASE:BATCH_MAPPING_ATTEMPT] Document: '%s' | Batch: %s | Attempt: %s/%s | Chunks: %s",
                artifact_name,
                batch_index,
                attempt,
                max_retries_per_batch,
                [c.index for c in chunks],
            )
            try:
                fragment = mapper.map_batch(
                    batch_payload=batch_payload,
                    chunks=chunks,
                    graph_context=graph_context,
                    previous_error=previous_error,
                )
            except Exception as exc:
                if _is_retryable_extraction_error(exc) and attempt < max_retries_per_batch:
                    logger.warning(
                        "[PHASE:BATCH_MAPPING_RETRY] Document: '%s' | Batch: %s | Attempt: %s/%s | Reason: %s",
                        artifact_name,
                        batch_index,
                        attempt,
                        max_retries_per_batch,
                        _orchestration_error_message(exc),
                    )
                    previous_error = _extractor_retry_error(exc)
                    continue
                logger.error(
                    "[INGESTION_ERROR] Document '%s' failed at phase 'direct_graph_mapping' (batch %s, attempt %s/%s). "
                    "ErrorKind: %s, Details: %s. Ingestion halted and no graph was persisted.",
                    artifact_name,
                    batch_index,
                    attempt,
                    max_retries_per_batch,
                    _orchestration_error_kind(exc),
                    _orchestration_error_message(exc),
                    exc_info=True,
                )
                return _terminal_mapping_failure(
                    ingestion_id,
                    batch_index,
                    _orchestration_error_message(exc),
                    error_kind=_orchestration_error_kind(exc),
                    artifact_name=artifact_name,
                )

            response = submit_ingestion_batch(
                ingestion_id,
                batch_index,
                fragment,
                tool_context,
            )
            if response.get("success"):
                break
            previous_error = {
                "stage": "batch_validation",
                "errors": response.get("errors", []),
                "conflict": response.get("conflict", {}),
                "candidateFragment": fragment.model_dump(
                    by_alias=True, mode="json", exclude_none=True
                ),
                "repairInstructions": (
                    "Repair candidateFragment minimally; do not remap the batch from scratch. Keep "
                    "merge strict. For an existing canonical node, remove only the incoming scalar "
                    "property that conflicts with an already-populated canonical value unless source "
                    "identity proves a distinct entity. Preserve all unrelated valid facts."
                ),
            }
            if attempt == max_retries_per_batch:
                logger.error(
                    "[INGESTION_ERROR] Document '%s' failed at phase 'batch_validation' (batch %s). "
                    "Batch conflict could not be resolved after %s retries. Errors: %s",
                    artifact_name,
                    batch_index,
                    max_retries_per_batch,
                    response.get("errors"),
                )
                return {
                    **response,
                    "stage": "explicit_extraction_failure",
                    "terminal": True,
                    "failureReason": "BATCH_CONFLICT",
                }
        else:
            logger.error(
                "[INGESTION_ERROR] Document '%s' failed at phase 'direct_graph_mapping' (batch %s). "
                "Retries exhausted without producing a valid fragment.",
                artifact_name,
                batch_index,
            )
            return _terminal_mapping_failure(
                ingestion_id,
                batch_index,
                "Direct graph mapping retries exhausted",
                artifact_name=artifact_name,
            )

    logger.info(
        "[PHASE:FINALIZE_START] Document: '%s' | IngestionID: %s",
        artifact_name,
        ingestion_id,
    )
    finalized = finalize_ingestion(ingestion_id, tool_context)
    for repair_round in range(1, FINAL_COVERAGE_REPAIR_ROUNDS + 1):
        if finalized.get("stage") == "ready_to_fill":
            break
        workspace = _load_workspace(tool_context)
        if workspace is None or not _final_coverage_errors_by_batch(finalized, workspace):
            break
        logger.warning(
            "[PHASE:FINAL_COVERAGE_REPAIR] Document: '%s' | Round: %s/%s | Errors: %s",
            artifact_name,
            repair_round,
            FINAL_COVERAGE_REPAIR_ROUNDS,
            [error.get("location") for error in finalized.get("errors", [])],
        )
        repaired = _repair_final_coverage_batches(
            ingestion_id=ingestion_id,
            tool_context=tool_context,
            mapper=mapper,
            finalized=finalized,
            max_retries_per_batch=max_retries_per_batch,
        )
        if not repaired:
            break
        finalized = finalize_ingestion(ingestion_id, tool_context)

    partial_override = bool(
        persist
        and allow_partial_persistence
        and finalized.get("stage") == "readiness_gate"
        and finalized.get("validForExtraction") is True
    )
    if finalized.get("stage") != "ready_to_fill" and not partial_override:
        logger.error(
            "[INGESTION_ERROR] Document '%s' failed at phase 'finalize_ingestion' (stage='%s'). "
            "Merged graph patch rejected at readiness gate. Details: %s",
            artifact_name,
            finalized.get("stage"),
            finalized.get("errors") or finalized.get("validation"),
        )
        return {**finalized, "terminal": True}
    if not persist:
        logger.info(
            "[PHASE:FINALIZE_SUCCESS] Document '%s' finalized successfully (dry-run, persist=False).",
            artifact_name,
        )
        return {
            **finalized,
            "stage": "ready_to_fill",
            "terminal": True,
            "persisted": False,
        }

    logger.info(
        "[PHASE:FILL_START] Document '%s' | IngestionID: %s | PartialOverride: %s",
        artifact_name,
        ingestion_id,
        partial_override,
    )
    filled = await fill_ingestion(
        ingestion_id,
        tool_context,
        allow_partial_persistence=partial_override,
    )
    workspace = _load_workspace(tool_context)
    stats = _workspace_stats(workspace) if workspace else {}
    if not filled.get("success"):
        logger.error(
            "[INGESTION_ERROR] Document '%s' failed at phase 'fill_ingestion'. "
            "Graph persistence to Neo4j database failed. Details: %s",
            artifact_name,
            filled,
        )
    else:
        logger.info(
            "[PHASE:INGEST_END_TO_END_SUCCESS] Document '%s' successfully ingested and persisted. "
            "IngestionID: %s | Nodes: %s | Edges: %s | CommitStatus: %s",
            artifact_name,
            ingestion_id,
            filled.get("nodes"),
            filled.get("edges"),
            filled.get("commitStatus"),
        )

    return {
        **filled,
        "terminal": True,
        "ingestionId": ingestion_id,
        "workspaceStats": stats,
    }


def _terminal_mapping_failure(
    ingestion_id: str,
    batch_index: int,
    message: str,
    *,
    error_kind: str = "llm_extraction",
    artifact_name: str | None = None,
) -> dict[str, Any]:
    """
    Dựng payload lỗi kết thúc phiên khi bước trích xuất thất bại không thể phục hồi.
    """
    issue = ValidationIssue(
        code="ORCHESTRATION_FAILED",
        message=message,
        location=f"batches.{batch_index}.extract",
    )
    doc_info = f"'{artifact_name}' " if artifact_name else ""
    logger.error(
        "[INGESTION_ERROR] Phase: DIRECT_GRAPH_MAPPING | Func: _terminal_mapping_failure | "
        "Document: %s | IngestionID: %s | Batch: %s | Stage: explicit_extraction_failure | "
        "ErrorKind: %s | Error: %s | Result: Extraction halted, no graph persisted.",
        doc_info.strip(),
        ingestion_id,
        batch_index,
        error_kind,
        message,
    )
    return {
        "success": False,
        "stage": "explicit_extraction_failure",
        "terminal": True,
        "ingestionId": ingestion_id,
        "batchIndex": batch_index,
        "errorKind": error_kind,
        "errors": [issue.model_dump(by_alias=True, exclude_none=True)],
    }


def validate_graph_patch(
    graph_patch: GraphPatchDraft,
    tool_context: IngestionRuntime,
) -> dict[str, Any]:
    """
    Kiểm định một graph patch do caller cung cấp mà không ghi dữ liệu.

    Args:
        graph_patch: Patch cần kiểm định.
        tool_context: Tool context của phiên.

    Returns:
        Kết quả kiểm định kèm fingerprint.
    """

    assessment = _get_validation_service().assess(
        graph_patch,
        tool_context.state.get(ARTIFACT_DIGEST_STATE_KEY),
        tool_context.state.get(SOURCE_CHUNKS_STATE_KEY),
    )
    if (
        assessment.result.valid_for_extraction
        and assessment.result.valid_for_persistence
        and assessment.fingerprint is not None
    ):
        tool_context.state[VALIDATED_FINGERPRINT_STATE_KEY] = assessment.fingerprint
    else:
        _clear_validation_gate(tool_context)
    return _public_assessment(assessment)


async def fill_graph_patch(
    graph_patch: GraphPatchDraft,
    tool_context: IngestionRuntime,
) -> dict[str, Any]:
    """
    Ghi một graph patch đã được cấp quyền trong phiên hiện tại.

    Args:
        graph_patch: Patch cần ghi.
        tool_context: Tool context của phiên.

    Returns:
        Payload kết quả ghi.
    """

    artifact_digest = tool_context.state.get(ARTIFACT_DIGEST_STATE_KEY)
    validation_service = _get_validation_service()
    validated_fingerprint = tool_context.state.get(VALIDATED_FINGERPRINT_STATE_KEY)
    source_chunks = tool_context.state.get(SOURCE_CHUNKS_STATE_KEY)
    assessment = validation_service.assess(graph_patch, artifact_digest, source_chunks)
    candidate_fingerprint = assessment.fingerprint
    if (
        validated_fingerprint is None
        or candidate_fingerprint is None
        or validated_fingerprint != candidate_fingerprint
    ):
        issue = ValidationIssue(
            code="VALIDATION_PRECONDITION",
            message=(
                "The current graph patch and artifact must pass validation in "
                "this invocation before fill_graph_patch can run"
            ),
            location="graphPatch",
        )
        return {
            "success": False,
            "stage": "validation_precondition",
            "errors": [issue.model_dump(by_alias=True, exclude_none=True)],
        }

    if not assessment.result.valid_for_persistence:
        _clear_validation_gate(tool_context)
        return {
            "success": False,
            "stage": "validation",
            "validation": _public_assessment(assessment),
        }

    return await _persist_with_receipt(
        graph_patch=graph_patch,
        artifact_digest=artifact_digest,
        source_chunks=source_chunks,
        validation_service=validation_service,
        artifact_stem=f"patch-{candidate_fingerprint[:12]}",
        tool_context=tool_context,
        require_receipt=False,
        invalidate_gate=lambda: _clear_validation_gate(tool_context),
        failure_message="Failed to persist GraphPatch to Neo4j",
    )
