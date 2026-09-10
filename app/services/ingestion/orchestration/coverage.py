"""Phase 6 — Sửa lỗi coverage ở bước finalize.

Sau khi đã gộp mọi batch, một số chunk có thể vẫn chưa được chứng minh là đã trích
xuất. Module này tìm các batch còn thiếu coverage và yêu cầu model trích xuất lại
đúng những batch đó trước khi patch được coi là hoàn tất."""

import logging
from typing import Any

from app.core.schemas.ingestion.document import DocumentChunk
from app.core.schemas.ingestion.workspace import (
    IngestionWorkspace,
)
from app.services.ingestion.mapping.graph_mapper import (
    AdkGraphMapper,
)
from app.services.ingestion.orchestration.context import (
    _batch_payload,
    _canonical_graph_context,
)
from app.services.ingestion.orchestration.errors import (
    _extractor_retry_error,
    _is_retryable_extraction_error,
    _orchestration_error_message,
)
from app.services.ingestion.orchestration.state import IngestionRuntime, _load_workspace

logger = logging.getLogger(__name__)


def _final_coverage_errors_by_batch(
    finalized: dict[str, Any],
    workspace: IngestionWorkspace,
) -> dict[int, list[dict[str, Any]]]:
    """
    Nhóm các lỗi coverage của patch cuối theo từng batch để biết cần sửa batch nào.
    """
    errors = finalized.get("errors")
    if not isinstance(errors, list) or not errors:
        return {}
    if any(
        not isinstance(error, dict) or error.get("code") != "COVERAGE_NOT_EVIDENCED"
        for error in errors
    ):
        return {}

    chunk_to_batch = {
        chunk_index: batch.index
        for batch in workspace.batches
        for chunk_index in batch.chunk_indexes
    }
    routed: dict[int, list[dict[str, Any]]] = {}
    for error in errors:
        location = str(error.get("location") or "")
        if not location.startswith("coverage."):
            return {}
        try:
            chunk_index = int(location.split(".", 1)[1])
        except ValueError:
            return {}
        batch_index = chunk_to_batch.get(chunk_index)
        if batch_index is None:
            return {}
        routed.setdefault(batch_index, []).append(error)
    return routed


def _repair_final_coverage_batches(
    *,
    ingestion_id: str,
    tool_context: IngestionRuntime,
    mapper: AdkGraphMapper,
    finalized: dict[str, Any],
    max_retries_per_batch: int,
) -> bool:
    """
    Gọi lại model cho các batch thiếu coverage và gộp fragment mới vào workspace.

    Returns:
        `True` nếu có batch được sửa thành công, ngược lại `False`.
    """
    workspace = _load_workspace(tool_context)
    if workspace is None:
        return False
    errors_by_batch = _final_coverage_errors_by_batch(finalized, workspace)
    if not errors_by_batch:
        return False

    for batch_index, final_errors in sorted(errors_by_batch.items()):
        workspace = _load_workspace(tool_context)
        if workspace is None:
            return False
        batch = workspace.batches[batch_index]
        current_fragment = batch.fragment
        if current_fragment is None:
            return False
        batch_payload = _batch_payload(workspace, batch)
        chunks = [
            DocumentChunk.model_validate(item)
            for item in batch_payload.get("chunks", [])
        ]
        affected_chunk_indexes = {
            int(str(error["location"]).split(".", 1)[1])
            for error in final_errors
        }
        previous_error: dict[str, Any] = {
            "stage": "final_coverage_validation",
            "errors": final_errors,
            "candidateFragment": current_fragment.model_dump(
                by_alias=True, mode="json", exclude_none=True
            ),
            "repairInstructions": (
                "Repair this same candidateFragment minimally. Re-read the unresolved chunks named "
                "in errors against the ontology class/property/edge definitions. A prior "
                "UNSUPPORTED_BY_ONTOLOGY or AMBIGUOUS decision is invalid when a direct ontology "
                "representation exists. Do not clear a final coverage error merely by relabeling "
                "the affected chunk as NOT_RELEVANT or NO_RELEVANT_FACT. If no representation can "
                "be justified, keep the chunk unresolved so final validation remains explicit. "
                "Preserve unrelated valid nodes, edges, evidence, coverage and canonical identities; "
                "do not regenerate unrelated content."
            ),
        }

        for attempt in range(1, max_retries_per_batch + 1):
            workspace = _load_workspace(tool_context)
            if workspace is None:
                return False
            graph_context = _canonical_graph_context(workspace, batch_index)
            logger.info(
                "[PHASE:FINAL_COVERAGE_REPAIR_ATTEMPT] Batch: %s | Attempt: %s/%s | Errors: %s",
                batch_index,
                attempt,
                max_retries_per_batch,
                [error.get("location") for error in final_errors],
            )
            try:
                repaired = mapper.map_batch(
                    batch_payload=batch_payload,
                    chunks=chunks,
                    graph_context=graph_context,
                    previous_error=previous_error,
                )
            except Exception as exc:
                if _is_retryable_extraction_error(exc) and attempt < max_retries_per_batch:
                    retry_feedback = _extractor_retry_error(exc)
                    validation = retry_feedback.get("validation", {})
                    candidate_fragment = (
                        validation.get("candidateFragment")
                        if isinstance(validation, dict)
                        else None
                    )
                    deterministic_errors = (
                        validation.get("errors", [])
                        if isinstance(validation, dict)
                        else []
                    )
                    previous_error = {
                        "stage": "final_coverage_validation",
                        "errors": [*final_errors, *deterministic_errors],
                        "validation": retry_feedback,
                        "candidateFragment": current_fragment.model_dump(
                            by_alias=True, mode="json", exclude_none=True
                        ),
                        "rejectedFragment": candidate_fragment,
                        "repairInstructions": (
                            "Continue the same FINAL COVERAGE REPAIR. First fix the reported "
                            "deterministic schema/evidence/grounding errors, then still resolve "
                            "every original coverage target. Do not drop or relabel targets to "
                            "escape validation, and preserve unrelated valid graph content."
                        ),
                    }
                    continue
                logger.error(
                    "[INGESTION_ERROR] Phase: FINAL_COVERAGE_REPAIR | Batch: %s | Error: %s",
                    batch_index,
                    _orchestration_error_message(exc),
                    exc_info=True,
                )
                return False

            coverage_by_index = {
                item.chunk_index: item.decision for item in repaired.coverage
            }
            baseline_coverage_by_index = {
                item.chunk_index: item.decision for item in current_fragment.coverage
            }
            changed_outside_scope = sorted(
                chunk_index
                for chunk_index, baseline_decision in baseline_coverage_by_index.items()
                if chunk_index not in affected_chunk_indexes
                and coverage_by_index.get(chunk_index) != baseline_decision
            )
            if changed_outside_scope:
                previous_error = {
                    "stage": "final_coverage_validation",
                    "errors": [
                        {
                            "code": "FINAL_COVERAGE_REPAIR_SCOPE_VIOLATION",
                            "location": f"coverage.{chunk_index}",
                            "message": (
                                "Final coverage repair changed a non-target coverage decision. "
                                "Restore the baseline decision and preserve the corresponding "
                                "valid graph evidence; only repair the explicitly targeted chunks."
                            ),
                        }
                        for chunk_index in changed_outside_scope
                    ],
                    "candidateFragment": current_fragment.model_dump(
                        by_alias=True, mode="json", exclude_none=True
                    ),
                    "rejectedFragment": repaired.model_dump(
                        by_alias=True, mode="json", exclude_none=True
                    ),
                    "repairInstructions": (
                        "Restore every non-target coverage entry exactly to the baseline and keep "
                        "its valid graph facts. Repair only the final coverage targets."
                    ),
                }
                if attempt < max_retries_per_batch:
                    continue
                return False

            unresolved = sorted(
                chunk_index
                for chunk_index in affected_chunk_indexes
                if coverage_by_index.get(chunk_index)
                not in {"MAPPED", "DUPLICATE_EVIDENCE"}
            )
            if unresolved:
                previous_error = {
                    "stage": "final_coverage_validation",
                    "errors": [
                        {
                            "code": "FINAL_COVERAGE_REPAIR_NOT_RESOLVED",
                            "location": f"coverage.{chunk_index}",
                            "message": (
                                "This chunk still has no resolved graph accounting after final "
                                "coverage repair. Emit grounded ontology facts and mark MAPPED when "
                                "a representation exists, or use DUPLICATE_EVIDENCE only when the "
                                "same fact is already represented."
                            ),
                        }
                        for chunk_index in unresolved
                    ],
                    "candidateFragment": current_fragment.model_dump(
                        by_alias=True, mode="json", exclude_none=True
                    ),
                    "rejectedFragment": repaired.model_dump(
                        by_alias=True, mode="json", exclude_none=True
                    ),
                    "repairInstructions": (
                        "Repair candidateFragment minimally. Re-evaluate each listed source chunk "
                        "against the ontology and canonical graph context. An unresolved coverage "
                        "decision is not a successful repair. Preserve unrelated valid graph facts."
                    ),
                }
                if attempt < max_retries_per_batch:
                    continue
                return False

            # Import cục bộ để phá vòng import tools ↔ coverage: `tools` cần hàm
            # `_repair_final_coverage_batches`, còn hàm này cần `submit_ingestion_batch`.
            from app.services.ingestion.orchestration.tools import submit_ingestion_batch

            response = submit_ingestion_batch(
                ingestion_id, batch_index, repaired, tool_context
            )
            if response.get("success"):
                break
            previous_error = {
                "stage": "final_coverage_validation",
                "errors": response.get("errors", []),
                "conflict": response.get("conflict", {}),
                "candidateFragment": current_fragment.model_dump(
                    by_alias=True, mode="json", exclude_none=True
                ),
                "rejectedFragment": repaired.model_dump(
                    by_alias=True, mode="json", exclude_none=True
                ),
                "repairInstructions": (
                    "Repair the baseline fragment minimally so it can replace the existing batch "
                    "without merge conflict. Preserve all non-target valid facts and coverage "
                    "decisions exactly; keep identity resolution strict."
                ),
            }
        else:
            return False
    return True
