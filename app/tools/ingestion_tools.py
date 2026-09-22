"""Primitive tools for the ingestion Skill.

The Skill owns workflow/orchestration.
This module only exposes deterministic ingestion capabilities.
"""

import hashlib
import json
import logging
import os
from typing import Any, Literal

from google.adk.tools import ToolContext

from app.core.schemas.ingestion.document import DocumentChunk
from app.core.schemas.ingestion.graph_patch import GraphPatchDraft, GraphPatchFragment
from app.core.schemas.ingestion.validation import ValidationIssue
from app.core.schemas.ingestion.workspace import IngestionWorkspace
from app.core.trace_logger import trace_pprint
from app.services.ingestion.document.strategies import stable_document_id
from app.services.ingestion.incremental.runtime import lifecycle_from_workspace
from app.services.ingestion.orchestration.artifact import (
    load_and_prepare_artifact_context,
)
from app.services.ingestion.orchestration.context import _batch_payload, _batch_summary
from app.services.ingestion.orchestration.receipts import (
    _persist_with_receipt,
    _public_assessment,
)
from app.services.ingestion.orchestration.state import (
    ARTIFACT_DIGEST_STATE_KEY,
    SOURCE_CHUNKS_STATE_KEY,
    VALIDATED_FINGERPRINT_STATE_KEY,
    WORKSPACE_STATE_KEY,
    _clear_validation_gate,
    _current_provenance,
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
from app.services.ingestion.persistence.service import create_graph_persistence
from app.services.ingestion.workspace.staged_ingestion import WorkspaceConflictError

logger = logging.getLogger(__name__)
MAX_REPAIR_ATTEMPTS = 2
MAX_BATCH_VALIDATION_ATTEMPTS = max(
    1, int(os.getenv("INGESTION_MAX_BATCH_VALIDATION_ATTEMPTS", "2"))
)


def _handle_validation_failure(
    workspace: IngestionWorkspace,
    batch_index: int,
    tool_context: ToolContext,
    errors: list[dict[str, Any]],
    stage: str = "batch_validation",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    current_attempt = workspace.validation_attempts_by_batch.get(batch_index, 0) + 1
    workspace.validation_attempts_by_batch[batch_index] = current_attempt

    if current_attempt > MAX_BATCH_VALIDATION_ATTEMPTS:
        workspace.status = "FAILED"
        workspace.terminal_error_code = "BATCH_VALIDATION_RETRY_LIMIT_EXCEEDED"
        if batch_index not in workspace.repair_batch_indexes:
            workspace.repair_batch_indexes = sorted(
                [*workspace.repair_batch_indexes, batch_index]
            )
        _store_workspace(tool_context, workspace)
        response = {
            "success": False,
            "stage": "explicit_extraction_failure",
            "terminal": True,
            "nextAction": "explicit_extraction_failure",
            "ingestionId": workspace.ingestion_id,
            "batchIndex": batch_index,
            "attempt": current_attempt,
            "maxAttempts": MAX_BATCH_VALIDATION_ATTEMPTS,
            "affectedBatchIndexes": [batch_index],
            "affectedChunkIndexes": (
                workspace.batches[batch_index].chunk_indexes
                if 0 <= batch_index < len(workspace.batches)
                else []
            ),
            "repairBatchIndexes": workspace.repair_batch_indexes,
            "repairAttemptsByBatch": workspace.repair_attempts_by_batch,
            "validationAttemptsByBatch": workspace.validation_attempts_by_batch,
            "maxRepairAttempts": MAX_REPAIR_ATTEMPTS,
            "maxBatchValidationAttempts": MAX_BATCH_VALIDATION_ATTEMPTS,
            "errors": [
                {
                    "code": "BATCH_VALIDATION_RETRY_LIMIT_EXCEEDED",
                    "message": (
                        f"Batch {batch_index} exceeded maximum validation retry attempts "
                        f"({MAX_BATCH_VALIDATION_ATTEMPTS})"
                    ),
                },
                *errors,
            ],
        }
        if extra:
            response.update(extra)
        logger.warning(
            "[INGESTION_ERROR] Phase: BATCH_SUBMIT | Func: submit_ingestion_batch | "
            "IngestionID: %s | Batch: %s | Terminal validation failure: %s",
            workspace.ingestion_id,
            batch_index,
            response["errors"][0]["message"],
        )
        return response

    _store_workspace(tool_context, workspace)
    response = {
        "success": False,
        "ingestionId": workspace.ingestion_id,
        "stage": stage,
        "terminal": False,
        "batchIndex": batch_index,
        "retryRequired": True,
        "nextAction": "remap_same_batch",
        "attempt": current_attempt,
        "maxAttempts": MAX_BATCH_VALIDATION_ATTEMPTS,
        "affectedBatchIndexes": [batch_index],
        "repairBatchIndexes": workspace.repair_batch_indexes,
        "repairAttemptsByBatch": workspace.repair_attempts_by_batch,
        "validationAttemptsByBatch": workspace.validation_attempts_by_batch,
        "maxRepairAttempts": MAX_REPAIR_ATTEMPTS,
        "maxBatchValidationAttempts": MAX_BATCH_VALIDATION_ATTEMPTS,
        "errors": errors,
    }
    if extra:
        response.update(extra)
    return response


def _repair_terminal_error(
    workspace, readiness_fingerprint: str, repair_batch_indexes: list[int]
) -> str | None:
    if not repair_batch_indexes:
        return "UNREPAIRABLE_READINESS_ISSUE"
    if (
        workspace.last_finalized_revision == workspace.staging_revision
        or workspace.last_readiness_fingerprint == readiness_fingerprint
    ):
        return "UNCHANGED_RETRY"
    if any(
        workspace.repair_attempts_by_batch.get(index, 0) >= MAX_REPAIR_ATTEMPTS
        for index in repair_batch_indexes
    ):
        return "REPAIR_LIMIT_EXCEEDED"
    return None


async def prepare_extraction_context(
    artifact_name: str,
    tool_context: ToolContext,
) -> dict[str, Any]:
    """Load and prepare the source document for ingestion."""
    return await load_and_prepare_artifact_context(artifact_name, tool_context)


async def begin_ingestion(
    artifact_name: str,
    tool_context: ToolContext,
    reset: bool = False,
) -> dict[str, Any]:
    """Prepare the source and create the staged batch-ingestion workspace."""
    logger.info(
        "[PHASE:BEGIN_INGESTION_START] Func: begin_ingestion | Document: '%s' | Reset: %s",
        artifact_name,
        reset,
    )

    # Resume Guard: Return active workspace status if ingestion is already in progress for this document
    raw_workspace = tool_context.state.get(WORKSPACE_STATE_KEY)
    existing_workspace = _load_workspace(tool_context)
    restart_reason = None
    if reset and existing_workspace is not None:
        from app.services.ingestion.incremental.staging_store import (
            IngestionStagingStore,
        )

        reset_store = IngestionStagingStore()
        try:
            reset_store.purge_staging(existing_workspace.ingestion_id)
        finally:
            reset_store.close()
        existing_workspace = None
        restart_reason = "EXPLICIT_USER_RESET"
    elif existing_workspace is not None and existing_workspace.status == "FAILED":
        from app.services.ingestion.incremental.staging_store import (
            IngestionStagingStore,
        )

        failed_store = IngestionStagingStore()
        try:
            failed_store.purge_staging(existing_workspace.ingestion_id)
        finally:
            failed_store.close()
        existing_workspace = None
        restart_reason = "TERMINAL_REPAIR_RESTART"
    elif (
        existing_workspace is not None
        and isinstance(raw_workspace, dict)
        and int(raw_workspace.get("stagingSchemaVersion", 1)) < 2
        and existing_workspace.status != "COMMITTED"
    ):
        from app.services.ingestion.incremental.staging_store import (
            IngestionStagingStore,
        )

        legacy_store = IngestionStagingStore()
        try:
            legacy_store.purge_staging(existing_workspace.ingestion_id)
        finally:
            legacy_store.close()
        existing_workspace = None
        restart_reason = "STAGING_SCHEMA_MIGRATION"

    if (
        not reset
        and existing_workspace is not None
        and existing_workspace.artifact_name == artifact_name
        and existing_workspace.status != "COMMITTED"
    ):
        current = _get_workspace_service().is_current(
            existing_workspace,
            provenance=_current_provenance(tool_context),
        )
        if current:
            logger.info(
                "[PHASE:BEGIN_INGESTION_RESUMED] Active workspace found for artifact '%s' (ingestionId=%s, status=%s). Returning status.",
                artifact_name,
                existing_workspace.ingestion_id,
                existing_workspace.status,
            )
            status = get_ingestion_status(existing_workspace.ingestion_id, tool_context)
            status["resumed"] = True
            return status

    prepared = await prepare_extraction_context(artifact_name, tool_context)
    if not prepared.get("success"):
        logger.error(
            "[INGESTION_ERROR] Phase: BEGIN_INGESTION | Func: begin_ingestion | "
            "Document: '%s' | Error: Context preparation failed: %s",
            artifact_name,
            prepared.get("error"),
        )
        return prepared

    chunks = [
        DocumentChunk.model_validate(item)
        for item in tool_context.state[SOURCE_CHUNKS_STATE_KEY]
    ]

    workspace = _get_workspace_service().begin(
        artifact_name=artifact_name,
        provenance=_current_provenance(tool_context),
        chunks=chunks,
    )

    from app.services.ingestion.incremental.staging_store import IngestionStagingStore

    store = IngestionStagingStore()
    try:
        store.purge_staging(workspace.ingestion_id)
    finally:
        store.close()

    _store_workspace(tool_context, workspace)

    first_batch = _get_workspace_service().next_batch(workspace)
    if first_batch is None:
        response = {
            "success": True,
            "stage": "ready_to_finalize",
            "terminal": False,
            "ingestionId": workspace.ingestion_id,
            "chunkCount": len(workspace.chunks),
            "batchCount": len(workspace.batches),
            "documentStats": _workspace_stats(workspace),
            "artifactDigest": workspace.artifact_digest,
            "ontologyDigest": workspace.ontology_digest,
            "skillDigest": workspace.skill_digest,
            "documentId": workspace.provenance.document_id,
            "configSignature": workspace.provenance.config_signature,
            "ingestionSignature": workspace.provenance.ingestion_signature,
            "sourceVersionId": workspace.provenance.source_version_id,
        }
        if restart_reason:
            response["restartReason"] = restart_reason
        return response

    logger.info(
        "[PHASE:BEGIN_INGESTION_SUCCESS] Func: begin_ingestion | Document: '%s' | "
        "IngestionID: %s | Chunks: %s | Batches: %s",
        artifact_name,
        workspace.ingestion_id,
        len(workspace.chunks),
        len(workspace.batches),
    )

    response = {
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
        "documentId": workspace.provenance.document_id,
        "configSignature": workspace.provenance.config_signature,
        "ingestionSignature": workspace.provenance.ingestion_signature,
        "sourceVersionId": workspace.provenance.source_version_id,
        "nextBatch": _batch_summary(first_batch),
    }
    if restart_reason:
        response["restartReason"] = restart_reason
    return response


def submit_ingestion_batch(
    ingestion_id: str,
    batch_index: int,
    graph_fragment: GraphPatchFragment,
    tool_context: ToolContext,
) -> dict[str, Any]:
    """Validate and incrementally stage one GraphPatchFragment."""
    workspace, error = _workspace_precondition(ingestion_id, tool_context)
    if error is not None or workspace is None:
        logger.error(
            "[INGESTION_ERROR] Phase: BATCH_SUBMIT | Func: submit_ingestion_batch | "
            "IngestionID: %s | Batch: %s | Error: Workspace precondition failed",
            ingestion_id,
            batch_index,
        )
        return error or {
            "success": False,
            "stage": "workspace_precondition",
            "terminal": False,
        }

    if workspace.status in ("FAILED", "COMMITTED"):
        return get_ingestion_status(ingestion_id, tool_context)

    if batch_index < 0 or batch_index >= len(workspace.batches):
        return {
            "success": False,
            "stage": "batch_validation",
            "terminal": True,
            "ingestionId": ingestion_id,
            "batchIndex": batch_index,
            "errors": [
                {
                    "code": "INVALID_BATCH_INDEX",
                    "message": f"Unknown batch index: {batch_index}",
                }
            ],
        }

    stored_batch = workspace.batches[batch_index]
    is_repair = stored_batch.status == "STAGED"

    try:
        fragment = GraphPatchFragment.model_validate(graph_fragment)
    except Exception as exc:
        trace_pprint(
            f"[TRACE][GRAPH_FRAGMENT] Failed to validate GraphPatchFragment for Batch {batch_index}: {exc}"
        )
        return _handle_validation_failure(
            workspace=workspace,
            batch_index=batch_index,
            tool_context=tool_context,
            errors=[{"code": "FRAGMENT_PARSE_ERROR", "message": str(exc)}],
        )

    try:
        _get_workspace_service()._validate_fragment_scope(
            workspace.batches[batch_index],
            fragment,
        )
    except (ValueError, WorkspaceConflictError) as exc:
        issue = ValidationIssue(
            code="BATCH_CONFLICT",
            message=str(exc),
            location=f"batches.{batch_index}",
        )
        trace_pprint(
            f"[TRACE][VALIDATION] Scope validation FAILED for Batch {batch_index}: {exc}"
        )
        logger.warning(
            "[INGESTION_ERROR] Phase: BATCH_SUBMIT | Func: submit_ingestion_batch | "
            "IngestionID: %s | Batch: %s | Conflict: %s",
            ingestion_id,
            batch_index,
            exc,
        )
        return _handle_validation_failure(
            workspace=workspace,
            batch_index=batch_index,
            tool_context=tool_context,
            errors=[issue.model_dump(by_alias=True, exclude_none=True)],
            extra={"conflict": getattr(exc, "conflict", {})},
        )

    term_issues = _get_validation_service().validate_fragment_terms(fragment)
    if term_issues:
        trace_pprint(
            f"[TRACE][VALIDATION] Ontology term validation FAILED for Batch {batch_index}:",
            [
                issue.model_dump(by_alias=True, exclude_none=True)
                for issue in term_issues
            ],
        )
        return _handle_validation_failure(
            workspace=workspace,
            batch_index=batch_index,
            tool_context=tool_context,
            errors=[
                issue.model_dump(by_alias=True, exclude_none=True)
                for issue in term_issues
            ],
        )

    try:
        from app.core.schemas.ingestion.graph_patch import (
            Evidence,
            ExtractedNode,
            ExtractedProperty,
        )
        from app.services.ingestion.incremental.accumulator import decompose_fragment
        from app.services.ingestion.incremental.pending_edges import (
            resolve_pending_edges,
        )
        from app.services.ingestion.incremental.staging_store import (
            IngestionStagingStore,
        )
        from app.services.ingestion.workspace.staged_ingestion import (
            IngestionWorkspaceService,
        )

        staging_store = IngestionStagingStore()
        try:
            # 1. Fetch existing staged entities using legacy find_relevant_entities
            staged_candidates = staging_store.find_relevant_entities(
                ingestion_id=ingestion_id,
                keywords=None,
                limit=500,
            )

            # 2. Build merged_nodes map and initialize temp_id_aliases
            merged_nodes: dict[str, ExtractedNode] = {}
            temp_id_aliases: dict[str, str] = {}
            dummy_ev = [Evidence(source="staged", chunk_index=0, text="staged")]
            for cand in staged_candidates:
                canonical_ref = f"entity:{cand['entityKey']}"
                if cand.get("tempId"):
                    temp_id_aliases[cand["tempId"]] = canonical_ref
                temp_id_aliases[cand["entityKey"]] = canonical_ref
                temp_id_aliases[canonical_ref] = canonical_ref

                props = [
                    ExtractedProperty(
                        property_name=p_name,
                        value=p_val,
                        evidence=dummy_ev,
                    )
                    for p_name, p_val in cand["properties"].items()
                    if p_val is not None
                ]
                merged_nodes[cand["entityKey"]] = ExtractedNode(
                    temp_id=canonical_ref,
                    class_name=cand["className"],
                    properties=props,
                    evidence=dummy_ev,
                    confidence=1.0,
                )

            # 3. Canonicalize incoming batch nodes via _canonical_temp_id
            for node in fragment.nodes:
                canonical_id = IngestionWorkspaceService._canonical_temp_id(
                    node, merged_nodes
                )
                temp_id_aliases[node.temp_id] = canonical_id

            # 4. Canonicalize fragment edges via _merge_edges
            fragment.edges = IngestionWorkspaceService._merge_edges(
                [fragment], temp_id_aliases
            )

            # 5. Decompose canonicalized fragment into batch facts
            decomposed = decompose_fragment(
                fragment,
                ingestion_id=ingestion_id,
                batch_index=batch_index,
            )

            # 6. Stage facts and resolve pending edges
            staging_store.stage_batch_facts(
                ingestion_id=ingestion_id,
                source_version_id=workspace.provenance.source_version_id,
                batch_index=batch_index,
                entities=decomposed["entities"],
                properties=decomposed["properties"],
                edges=decomposed["edges"],
                coverage=decomposed["coverage"],
                conflicts=decomposed["conflicts"],
                pending_edges=decomposed["pendingEdges"],
            )
            resolve_pending_edges(ingestion_id)
        finally:
            staging_store.close()

    except Exception as exc:
        logger.exception(
            "[INGESTION_ERROR] Phase: BATCH_SUBMIT | Func: submit_ingestion_batch | "
            "IngestionID: %s | Batch: %s | Staging failed: %s",
            ingestion_id,
            batch_index,
            exc,
        )
        return _handle_validation_failure(
            workspace=workspace,
            batch_index=batch_index,
            tool_context=tool_context,
            stage="staging_failure",
            errors=[{"code": "STAGING_FAILED", "message": str(exc)}],
        )

    workspace.validation_attempts_by_batch.pop(batch_index, None)
    if is_repair:
        stored_batch.retry_count += 1
        workspace.repair_attempts_by_batch[batch_index] = stored_batch.retry_count
    workspace.staging_revision += 1
    stored_batch = workspace.batches[batch_index]
    stored_batch.status = "STAGED"
    stored_batch.node_count = len(fragment.nodes)
    stored_batch.edge_count = len(fragment.edges)
    stored_batch.coverage_count = len(fragment.coverage)
    stored_batch.fragment = None

    _store_workspace(tool_context, workspace)

    next_batch = _get_workspace_service().next_batch(workspace)
    processed = sum(batch.status == "STAGED" for batch in workspace.batches)

    logger.info(
        "[PHASE:BATCH_SUBMIT_SUCCESS] Func: submit_ingestion_batch | "
        "IngestionID: %s | Batch: %s | Processed: %s/%s | Nodes: %s | Edges: %s",
        ingestion_id,
        batch_index,
        processed,
        len(workspace.batches),
        len(fragment.nodes),
        len(fragment.edges),
    )

    response: dict[str, Any] = {
        "success": True,
        "stage": "batching" if next_batch is not None else "ready_to_finalize",
        "terminal": False,
        "ingestionId": ingestion_id,
        "processedBatches": processed,
        "remainingBatches": len(workspace.batches) - processed,
        "fragmentStats": _fragment_stats(fragment),
        "workspaceStats": _workspace_stats(workspace),
    }

    if workspace.repair_batch_indexes:
        response["repairBatchIndexes"] = workspace.repair_batch_indexes
        response["repairAttemptsByBatch"] = workspace.repair_attempts_by_batch
        response["maxRepairAttempts"] = MAX_REPAIR_ATTEMPTS

    if next_batch is not None:
        response["nextBatch"] = _batch_payload(workspace, next_batch)
        response["nextBatch"] = _batch_summary(next_batch)

    return response


def finalize_ingestion(
    ingestion_id: str,
    tool_context: ToolContext,
) -> dict[str, Any]:
    """Validate accumulated persistent staging and determine fill readiness."""
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
        return error or {
            "success": False,
            "stage": "workspace_precondition",
            "terminal": False,
        }

    if workspace.status in ("FAILED", "COMMITTED"):
        return get_ingestion_status(ingestion_id, tool_context)

    pending = [batch.index for batch in workspace.batches if batch.status != "STAGED"]
    if pending:
        issue = ValidationIssue(
            code="BATCH_INCOMPLETE",
            message=f"Pending batch indexes: {pending}",
            location="batches",
        )
        return {
            "success": False,
            "stage": "batch_incomplete",
            "terminal": False,
            "ingestionId": ingestion_id,
            "errors": [issue.model_dump(by_alias=True, exclude_none=True)],
        }

    try:
        from app.services.ingestion.incremental.staging_store import (
            IngestionStagingStore,
        )

        store = IngestionStagingStore()
        try:
            summary = store.get_staging_summary(ingestion_id)
            staged_chunks = set(store.get_staged_coverage_indexes(ingestion_id))
            issue_batch_indexes = (
                store.get_issue_batch_indexes(ingestion_id)
                if hasattr(store, "get_issue_batch_indexes")
                else {"pendingEdges": [], "conflicts": []}
            )
            readiness_issues = store.validate_product_offer_has_offer(ingestion_id)

            validation_service = _get_validation_service()
            rule_deriving_edges = sorted(
                validation_service.registry.edge_names_deriving_property(
                    "pskg:ruleType"
                )
            )
            readiness_issues.extend(
                store.validate_edge_derived_property_relationships(
                    ingestion_id,
                    class_name="pskg:BusinessRule",
                    property_name="pskg:ruleType",
                    deriving_edge_names=rule_deriving_edges,
                )
            )
        finally:
            store.close()

    except Exception as exc:
        logger.exception(
            "[INGESTION_ERROR] Phase: FINALIZE | Func: finalize_ingestion | "
            "IngestionID: %s | Error: failed to read persistent staging",
            ingestion_id,
        )
        workspace.validated_fingerprint = None
        workspace.status = "PROCESSING"
        _store_workspace(tool_context, workspace)
        return {
            "success": False,
            "stage": "staging_read_failure",
            "terminal": False,
            "ingestionId": ingestion_id,
            "errors": [
                {
                    "code": "STAGING_READ_FAILED",
                    "message": str(exc),
                }
            ],
        }

    readiness_issues: list[dict[str, Any]]

    expected_chunks = {chunk.index for chunk in workspace.chunks}
    missing_chunks = sorted(expected_chunks - staged_chunks)

    if missing_chunks:
        readiness_issues.append(
            {
                "code": "MISSING_CHUNK_COVERAGE",
                "message": (
                    f"Missing chunk coverage for chunk indexes: {missing_chunks}"
                ),
                "chunkIndexes": missing_chunks,
            }
        )

    pending_edge_count = int(summary.get("pendingEdgeCount", 0) or 0)
    if pending_edge_count > 0:
        readiness_issues.append(
            {
                "code": "PENDING_EDGES",
                "message": (f"{pending_edge_count} edge(s) have unresolved endpoints"),
                "batchIndexes": issue_batch_indexes.get("pendingEdges", []),
            }
        )

    conflict_count = int(summary.get("conflictCount", 0) or 0)
    if conflict_count > 0:
        readiness_issues.append(
            {
                "code": "UNRESOLVED_CONFLICTS",
                "message": (f"{conflict_count} property conflict(s) remain"),
                "batchIndexes": issue_batch_indexes.get("conflicts", []),
            }
        )

    repair_batch_indexes = sorted(
        {
            batch.index
            for batch in workspace.batches
            for chunk_idx in batch.chunk_indexes
            if chunk_idx in missing_chunks
        }
        | {
            int(issue["batchIndex"])
            for issue in readiness_issues
            if issue.get("batchIndex") is not None
        }
        | {
            int(batch_index)
            for issue in readiness_issues
            for batch_index in issue.get("batchIndexes", [])
            if batch_index is not None
        }
    )

    affected_chunk_indexes = sorted(
        {
            chunk_index
            for batch in workspace.batches
            if batch.index in repair_batch_indexes
            for chunk_index in batch.chunk_indexes
        }
        | set(missing_chunks)
    )
    readiness_fingerprint = hashlib.sha256(
        json.dumps(readiness_issues, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]

    fingerprint_input = (
        f"{ingestion_id}|"
        f"entities={summary.get('entityCount', 0)}|"
        f"edges={summary.get('edgeCount', 0)}|"
        f"coverage={summary.get('coverageCount', 0)}|"
        f"pendingEdges={pending_edge_count}|"
        f"conflicts={conflict_count}|"
        f"batches={len(workspace.batches)}|"
        f"revision={workspace.staging_revision}"
    )
    staging_fingerprint = hashlib.sha256(fingerprint_input.encode("utf-8")).hexdigest()[
        :16
    ]

    valid_for_persistence = not readiness_issues

    terminal_error_code = (
        None
        if valid_for_persistence
        else _repair_terminal_error(
            workspace, readiness_fingerprint, repair_batch_indexes
        )
    )

    workspace.last_finalized_revision = workspace.staging_revision
    workspace.last_readiness_fingerprint = (
        readiness_fingerprint if readiness_issues else None
    )
    workspace.repair_batch_indexes = repair_batch_indexes
    workspace.last_readiness_issues = readiness_issues

    if terminal_error_code is not None:
        workspace.validated_fingerprint = None
        workspace.status = "FAILED"
        workspace.terminal_error_code = terminal_error_code
        _store_workspace(tool_context, workspace)
        terminal_result = {
            "success": False,
            "stage": "explicit_extraction_failure",
            "terminal": True,
            "nextAction": "explicit_extraction_failure",
            "ingestionId": ingestion_id,
            "errors": [
                {
                    "code": terminal_error_code,
                    "message": "Ingestion repair did not make bounded progress",
                }
            ],
            "readinessIssues": readiness_issues,
            "readinessFingerprint": readiness_fingerprint,
            "affectedBatchIndexes": repair_batch_indexes,
            "affectedChunkIndexes": affected_chunk_indexes,
            "repairAttemptsByBatch": workspace.repair_attempts_by_batch,
            "maxRepairAttempts": MAX_REPAIR_ATTEMPTS,
            "validationAttemptsByBatch": workspace.validation_attempts_by_batch,
            "maxBatchValidationAttempts": MAX_BATCH_VALIDATION_ATTEMPTS,
        }
        trace_pprint(
            f"[TRACE][REPAIR_TERMINATED] Ingestion ID {ingestion_id}:",
            terminal_result,
        )
        return terminal_result

    workspace.validated_fingerprint = (
        staging_fingerprint if valid_for_persistence else None
    )
    workspace.terminal_error_code = None
    workspace.status = "READY" if valid_for_persistence else "PROCESSING"
    _store_workspace(tool_context, workspace)

    stage = "ready_to_fill" if valid_for_persistence else "repair_required"

    finalize_summary = {
        "ingestion_id": ingestion_id,
        "stage": stage,
        "ready_for_persistence": valid_for_persistence,
        "entity_count": summary.get("entityCount", 0),
        "edge_count": summary.get("edgeCount", 0),
        "pending_edge_count": pending_edge_count,
        "conflict_count": conflict_count,
        "staged_batches": f"{summary.get('stagedBatchCount', 0)}/{len(workspace.batches)}",
        "fingerprint": staging_fingerprint,
        "readiness_issues": readiness_issues,
    }
    trace_pprint(
        f"[TRACE][FINALIZE] Finalize status for Ingestion ID {ingestion_id}:",
        finalize_summary,
    )

    logger.info(
        "[PHASE:FINALIZE_SUCCESS] Func: finalize_ingestion | "
        "IngestionID: %s | Stage: %s | Entities: %s | Edges: %s | "
        "PendingEdges: %s | Conflicts: %s | RepairBatches: %s | Fingerprint: %s",
        ingestion_id,
        stage,
        summary.get("entityCount", 0),
        summary.get("edgeCount", 0),
        pending_edge_count,
        conflict_count,
        repair_batch_indexes,
        staging_fingerprint,
    )

    return {
        "success": True,
        "stage": stage,
        "ready": valid_for_persistence,
        "terminal": False,
        "terminal": not valid_for_persistence,
        "ingestionId": ingestion_id,
        "validForExtraction": True,
        "validForPersistence": valid_for_persistence,
        "repairBatchIndexes": repair_batch_indexes,
        "affectedBatchIndexes": repair_batch_indexes,
        "affectedChunkIndexes": affected_chunk_indexes,
        "nextAction": "fill" if valid_for_persistence else "repair_batches",
        "readinessFingerprint": readiness_fingerprint,
        "repairAttemptsByBatch": workspace.repair_attempts_by_batch,
        "maxRepairAttempts": MAX_REPAIR_ATTEMPTS,
        "validationAttemptsByBatch": workspace.validation_attempts_by_batch,
        "maxBatchValidationAttempts": MAX_BATCH_VALIDATION_ATTEMPTS,
        "nodeCount": summary.get("entityCount", 0),
        "edgeCount": summary.get("edgeCount", 0),
        "pendingEdgeCount": pending_edge_count,
        "conflictCount": conflict_count,
        "coverageCount": summary.get("coverageCount", 0),
        "stagedBatchCount": summary.get("stagedBatchCount", 0),
        "fingerprint": staging_fingerprint,
        "readinessIssues": readiness_issues,
        "artifactDigest": workspace.artifact_digest,
        "ontologyDigest": workspace.ontology_digest,
        "skillDigest": workspace.skill_digest,
        "workspaceStats": _workspace_stats(workspace),
    }


async def fill_ingestion(
    ingestion_id: str,
    tool_context: ToolContext,
) -> dict[str, Any]:
    """Promote finalized persistent staging into the domain graph."""
    trace_pprint(
        f"[TRACE][FILL] fill_ingestion invoked for Ingestion ID: {ingestion_id}"
    )

    workspace, error = _workspace_precondition(ingestion_id, tool_context)
    if error is not None or workspace is None:
        logger.error(
            "[INGESTION_ERROR] Phase: FILL | Func: fill_ingestion | "
            "IngestionID: %s | Error: Workspace precondition failed",
            ingestion_id,
        )
        return error or {
            "success": False,
            "stage": "workspace_precondition",
            "terminal": True,
        }

    if workspace.validated_fingerprint is None or workspace.status != "READY":
        trace_pprint(
            f"[TRACE][FILL] Precondition failed for Ingestion ID {ingestion_id}:",
            {
                "status": workspace.status,
                "fingerprint": workspace.validated_fingerprint,
            },
        )
        return {
            "success": False,
            "stage": "validation_precondition",
            "terminal": True,
            "ingestionId": ingestion_id,
            "errors": [
                ValidationIssue(
                    code="VALIDATION_PRECONDITION",
                    message=("finalize_ingestion must succeed before fill_ingestion"),
                    location="ingestionId",
                ).model_dump(by_alias=True, exclude_none=True)
            ],
        }

    persistence = create_graph_persistence()
    try:
        source_lifecycle = lifecycle_from_workspace(workspace)
        result = persistence.fill_staged_ingestion(
            ingestion_id,
            source_lifecycle,
            source_chunks=workspace.chunks,
        )
    except Exception as exc:
        workspace.validated_fingerprint = None
        workspace.status = "PROCESSING"
        _store_workspace(tool_context, workspace)
        logger.exception(
            "[INGESTION_ERROR] Phase: FILL | Func: fill_ingestion | "
            "IngestionID: %s | Error: %s",
            ingestion_id,
            exc,
        )
        return {
            "success": False,
            "stage": "persistence_failure",
            "terminal": True,
            "ingestionId": ingestion_id,
            "errors": [
                ValidationIssue(
                    code="PERSISTENCE_FAILED",
                    message=str(exc),
                    location="ingestionId",
                ).model_dump(by_alias=True, exclude_none=True)
            ],
        }
    finally:
        persistence.close()

    workspace.status = "COMMITTED"
    workspace.validated_fingerprint = None
    _store_workspace(tool_context, workspace)

    logger.info(
        "[PHASE:FILL_SUCCESS] Func: fill_ingestion | "
        "IngestionID: %s | Nodes: %s | Edges: %s | Status: %s",
        ingestion_id,
        result.get("nodes", 0),
        result.get("edges", 0),
        result.get("commitStatus"),
    )

    return {
        "success": True,
        "stage": "committed",
        "terminal": True,
        "ingestionId": ingestion_id,
        **result,
    }


def get_ingestion_status(
    ingestion_id: str,
    tool_context: ToolContext,
) -> dict[str, Any]:
    """Return the current staged-ingestion state."""
    workspace, error = _workspace_precondition(ingestion_id, tool_context)
    if error is not None or workspace is None:
        return {
            **(
                error
                or {
                    "success": False,
                    "stage": "workspace_precondition",
                }
            ),
            "terminal": True,
        }

    if workspace.status == "COMMITTED":
        return {
            "success": True,
            "stage": "completed",
            "terminal": True,
            "ingestionId": ingestion_id,
            "workspaceStats": _workspace_stats(workspace),
        }

    if workspace.status == "FAILED":
        return {
            "success": False,
            "stage": "explicit_extraction_failure",
            "terminal": True,
            "nextAction": "explicit_extraction_failure",
            "ingestionId": ingestion_id,
            "errors": [
                {
                    "code": workspace.terminal_error_code,
                    "message": "Ingestion repair terminated without progress",
                    "message": (
                        "Batch validation retry limit exceeded"
                        if workspace.terminal_error_code == "BATCH_VALIDATION_RETRY_LIMIT_EXCEEDED"
                        else "Ingestion repair terminated without progress"
                    ),
                }
            ],
            "readinessFingerprint": workspace.last_readiness_fingerprint,
            "repairBatchIndexes": workspace.repair_batch_indexes,
            "affectedBatchIndexes": workspace.repair_batch_indexes,
            "affectedChunkIndexes": sorted(
                chunk_index
                for batch in workspace.batches
                if batch.index in workspace.repair_batch_indexes
                for chunk_index in batch.chunk_indexes
            ),
            "readinessIssues": workspace.last_readiness_issues,
            "repairAttemptsByBatch": workspace.repair_attempts_by_batch,
            "maxRepairAttempts": MAX_REPAIR_ATTEMPTS,
            "validationAttemptsByBatch": workspace.validation_attempts_by_batch,
            "maxBatchValidationAttempts": MAX_BATCH_VALIDATION_ATTEMPTS,
            "workspaceStats": _workspace_stats(workspace),
        }

    if workspace.status == "READY":
        return {
            "success": True,
            "stage": "ready_to_fill",
            "terminal": False,
            "nextAction": "fill",
            "ingestionId": ingestion_id,
            "repairAttemptsByBatch": workspace.repair_attempts_by_batch,
            "maxRepairAttempts": MAX_REPAIR_ATTEMPTS,
            "validationAttemptsByBatch": workspace.validation_attempts_by_batch,
            "maxBatchValidationAttempts": MAX_BATCH_VALIDATION_ATTEMPTS,
            "workspaceStats": _workspace_stats(workspace),
        }

    next_batch = _get_workspace_service().next_batch(workspace)

    status: dict[str, Any] = {
        "success": True,
        "stage": (
            "repair_required"
            if next_batch is None and workspace.last_readiness_fingerprint
            else ("ready_to_finalize" if next_batch is None else "batching")
        ),
        "stage": "ready_to_finalize" if next_batch is None else "batching",
        "terminal": False,
        "nextAction": (
            "repair_batches"
            if next_batch is None and workspace.last_readiness_fingerprint
            else ("finalize" if next_batch is None else "process_batch")
        ),
        "ingestionId": ingestion_id,
        "workspaceStats": _workspace_stats(workspace),
        "partial": bool(workspace.skipped_chunk_indexes),
        "skippedChunks": workspace.skipped_chunk_indexes,
        "ingestionWarnings": workspace.ingestion_warnings,
        "readinessFingerprint": workspace.last_readiness_fingerprint,
        "repairBatchIndexes": workspace.repair_batch_indexes,
        "affectedBatchIndexes": workspace.repair_batch_indexes,
        "affectedChunkIndexes": sorted(
            chunk_index
            for batch in workspace.batches
            if batch.index in workspace.repair_batch_indexes
            for chunk_index in batch.chunk_indexes
        ),
        "readinessIssues": workspace.last_readiness_issues,
        "repairAttemptsByBatch": workspace.repair_attempts_by_batch,
        "maxRepairAttempts": MAX_REPAIR_ATTEMPTS,
        "validationAttemptsByBatch": workspace.validation_attempts_by_batch,
        "maxBatchValidationAttempts": MAX_BATCH_VALIDATION_ATTEMPTS,
    }

    if next_batch is not None:
        status["nextBatch"] = _batch_payload(workspace, next_batch)
        status["nextBatch"] = _batch_summary(next_batch)

    return status


def get_ingestion_batch(
    ingestion_id: str,
    batch_index: int,
    tool_context: ToolContext,
) -> dict[str, Any]:
    """Retrieve payload, chunks, and canonical graph context for a specific batch."""
    workspace, error = _workspace_precondition(ingestion_id, tool_context)
    if error is not None or workspace is None:
        return error or {
            "success": False,
            "stage": "workspace_precondition",
            "terminal": True,
        }

    if workspace.status in ("FAILED", "COMMITTED"):
        return get_ingestion_status(ingestion_id, tool_context)

    if batch_index < 0 or batch_index >= len(workspace.batches):
        return {
            "success": False,
            "stage": "batch_validation",
            "terminal": True,
            "batchIndex": batch_index,
            "errors": [
                {
                    "code": "INVALID_BATCH_INDEX",
                    "message": f"Unknown batch index: {batch_index}",
                }
            ],
        }

    target_batch = workspace.batches[batch_index]
    return {
        "success": True,
        "stage": "batch_retrieved",
        "terminal": False,
        "nextAction": "submit_batch",
        "ingestionId": ingestion_id,
        "batch": _batch_payload(workspace, target_batch),
        "repairBatchIndexes": workspace.repair_batch_indexes,
        "affectedBatchIndexes": workspace.repair_batch_indexes,
        "affectedChunkIndexes": sorted(
            chunk_index
            for batch in workspace.batches
            if batch.index in workspace.repair_batch_indexes
            for chunk_index in batch.chunk_indexes
        ),
        "readinessIssues": workspace.last_readiness_issues,
        "readinessFingerprint": workspace.last_readiness_fingerprint,
        "repairAttemptsByBatch": workspace.repair_attempts_by_batch,
        "maxRepairAttempts": MAX_REPAIR_ATTEMPTS,
        "validationAttemptsByBatch": workspace.validation_attempts_by_batch,
        "maxBatchValidationAttempts": MAX_BATCH_VALIDATION_ATTEMPTS,
    }


def validate_graph_patch(
    graph_patch: GraphPatchDraft,
    tool_context: ToolContext,
) -> dict[str, Any]:
    """Validate a caller-supplied small graph patch without writing."""
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
    tool_context: ToolContext,
) -> dict[str, Any]:
    """Persist a caller-supplied patch validated in the current invocation."""
    artifact_digest = tool_context.state.get(ARTIFACT_DIGEST_STATE_KEY)
    source_chunks = tool_context.state.get(SOURCE_CHUNKS_STATE_KEY)

    validation_service = _get_validation_service()
    validated_fingerprint = tool_context.state.get(VALIDATED_FINGERPRINT_STATE_KEY)

    assessment = validation_service.assess(
        graph_patch,
        artifact_digest,
        source_chunks,
    )
    candidate_fingerprint = assessment.fingerprint

    if (
        validated_fingerprint is None
        or candidate_fingerprint is None
        or validated_fingerprint != candidate_fingerprint
    ):
        issue = ValidationIssue(
            code="VALIDATION_PRECONDITION",
            message=(
                "The current graph patch and artifact must pass validation "
                "in this invocation before fill_graph_patch can run"
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


def delete_document(
    artifact_name: str,
    if_missing: Literal["error", "ignore"] = "error",
) -> dict[str, Any]:
    """Delete current source ownership and clean up unowned graph facts."""
    service = create_graph_persistence()
    try:
        result = service.source_store.delete_document(
            stable_document_id(artifact_name),
            mapper=service.writer.mapper,
            if_missing=if_missing,
        )
    finally:
        service.close()

    return {
        "success": True,
        "stage": "completed",
        "operation": "delete",
        **result,
    }


INGESTION_TOOLS = {
    "begin_ingestion": begin_ingestion,
    "get_ingestion_batch": get_ingestion_batch,
    "submit_ingestion_batch": submit_ingestion_batch,
    "finalize_ingestion": finalize_ingestion,
    "fill_ingestion": fill_ingestion,
    "get_ingestion_status": get_ingestion_status,
    "delete_document": delete_document,
    "validate_graph_patch": validate_graph_patch,
    "fill_graph_patch": fill_graph_patch,
}


def get_ingestion_tools() -> list:
    """Return public ingestion primitive tools."""
    return list(INGESTION_TOOLS.values())
