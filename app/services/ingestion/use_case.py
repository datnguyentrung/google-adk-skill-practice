import hashlib
import json
import logging
import os
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol

from google.genai import types

from app.core.schemas.ingestion.document import DocumentChunk
from app.core.schemas.ingestion.graph_patch import GraphPatchDraft, GraphPatchFragment
from app.core.schemas.ingestion.validation import ValidationIssue
from app.core.schemas.ingestion.workspace import (
    IngestionProvenance,
    IngestionWorkspace,
)
from app.services.ingestion.graph_persistence import (
    FillValidationError,
    create_graph_persistence,
)
from app.services.ingestion.document_preparation import DocumentPreparation
from app.services.ingestion.graph_validation import (
    GraphPatchAssessment,
    GraphValidation,
    InvalidGraphPatchFragmentError,
)
from app.services.ingestion.graph_validation import (
    create_default_semantic_grounding_judge,
)
from app.services.ingestion.semantic_placement import (
    DirectGraphMappingError,
    SemanticGraphMapper,
)
from app.services.ingestion.staged_ingestion import (
    IngestionWorkspaceService,
    WorkspaceConflictError,
)
from app.skills.skill_loader import skill_content_digest

logger = logging.getLogger(__name__)


class IngestionRuntime(Protocol):
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
INGESTION_SKILL_DIR = Path(__file__).resolve().parents[2] / "skills" / "ingestion"

@lru_cache(maxsize=1)
def _get_context_service() -> DocumentPreparation:
    return DocumentPreparation()


@lru_cache(maxsize=1)
def _get_validation_service() -> GraphValidation:
    return GraphValidation(
        semantic_grounding_judge=create_default_semantic_grounding_judge()
    )


@lru_cache(maxsize=1)
def _get_workspace_service() -> IngestionWorkspaceService:
    return IngestionWorkspaceService()


def _get_semantic_graph_mapper() -> SemanticGraphMapper:
    validation_service = _get_validation_service()
    return SemanticGraphMapper(
        registry=validation_service.validator.registry,
        compiler=validation_service.compiler,
        ontology_validator=validation_service.validator,
    )


def _orchestration_error_kind(exc: Exception) -> str:
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
    """HTTP 400 INVALID_ARGUMENT from schema/config is deterministic, not retryable."""

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
    message = str(exc).upper()
    status_code = getattr(exc, "status_code", None)
    return status_code == 429 or (
        "429" in message and ("RESOURCE_EXHAUSTED" in message or "QUOTA" in message)
    )


def _is_retryable_extraction_error(exc: Exception) -> bool:
    if getattr(exc, "stage_local_retries_exhausted", False):
        return False
    return (
        isinstance(exc, (InvalidGraphPatchFragmentError, DirectGraphMappingError))
        or _is_rate_limit_error(exc)
        or bool(getattr(exc, "retryable", False))
    )


def _extractor_retry_error(exc: Exception) -> dict[str, Any]:
    return {
        "stage": "direct_graph_mapping",
        "errorKind": _orchestration_error_kind(exc),
        "message": _orchestration_error_message(exc),
        "validation": getattr(exc, "summary", {}),
        "repairInstructions": (
            "Return one GraphPatchFragment using only the supplied ontology technical names. "
            "Correct the reported schema, ontology, edge-direction, coverage, or evidence defects."
        ),
    }


def _orchestration_error_message(exc: Exception) -> str:
    if _orchestration_error_kind(exc) == "llm_request_config":
        return (
            "Internal Gemini extractor request configuration is invalid; "
            "the uploaded document was not processed and no graph was persisted"
        )
    return str(exc)


def _delete_state(tool_context: IngestionRuntime, key: str) -> None:
    if key in tool_context.state:
        del tool_context.state[key]


def _clear_validation_gate(tool_context: IngestionRuntime) -> None:
    _delete_state(tool_context, VALIDATED_FINGERPRINT_STATE_KEY)


def _skill_digest() -> str:
    return skill_content_digest(INGESTION_SKILL_DIR)


def _current_provenance(tool_context: IngestionRuntime) -> IngestionProvenance:
    return IngestionProvenance(
        artifactDigest=tool_context.state.get(
            ARTIFACT_DIGEST_STATE_KEY,
            "MISSING",
        ),
        ontologyDigest=_get_validation_service().compiler.ontology_digest,
        skillDigest=_skill_digest(),
    )


def _load_workspace(tool_context: IngestionRuntime) -> IngestionWorkspace | None:
    raw = tool_context.state.get(WORKSPACE_STATE_KEY)
    if raw is None:
        return None
    return IngestionWorkspace.model_validate(raw)


def _store_workspace(
    tool_context: IngestionRuntime,
    workspace: IngestionWorkspace,
) -> None:
    tool_context.state[WORKSPACE_STATE_KEY] = workspace.model_dump(
        by_alias=True,
        mode="json",
    )


def _batch_payload(workspace: IngestionWorkspace, batch) -> dict[str, Any]:
    chunk_by_index = {chunk.index: chunk for chunk in workspace.chunks}
    return {
        "batchIndex": batch.index,
        "chunkIndexes": batch.chunk_indexes,
        "contentChars": batch.content_chars,
        "chunks": [
            chunk_by_index[index].model_dump(by_alias=True, exclude_none=True)
            for index in batch.chunk_indexes
        ],
    }


def _canonical_graph_context(
    workspace: IngestionWorkspace,
    before_batch_index: int,
) -> str:
    """Compact canonical snapshot of accepted batches for extractor reference."""

    fragments = [
        batch.fragment
        for batch in workspace.batches
        if batch.index < before_batch_index and batch.fragment is not None
    ]
    if not fragments:
        return ""
    fragment = _get_workspace_service().merge_fragments(fragments)
    node_lines: list[str] = []
    edge_lines: list[str] = []
    for node in fragment.nodes:
        identity = {
            entry.property_name: entry.value
            for entry in node.properties
            if not isinstance(entry.value, (dict, list))
        }
        node_lines.append(f"- ref={node.temp_id}")
        node_lines.append(f"  class={node.class_name}")
        node_lines.append(
            "  identity="
            + json.dumps(identity, ensure_ascii=False, sort_keys=True)
        )
    for edge in fragment.edges:
        edge_lines.append(
            f"- {edge.edge_name}: {edge.source_temp_id} -> {edge.target_temp_id}"
        )
    if not node_lines and not edge_lines:
        return ""
    lines = ["Existing canonical graph:"]
    if node_lines:
        lines.append("Nodes:")
        lines.extend(node_lines)
    if edge_lines:
        lines.append("Existing edges:")
        lines.extend(edge_lines)
    text = "\n".join(lines)
    if len(text) <= GRAPH_CONTEXT_MAX_CHARS:
        return text
    node_text = "\n".join(["Existing canonical graph:", "Nodes:", *node_lines])
    if len(node_text) <= GRAPH_CONTEXT_MAX_CHARS:
        return node_text
    trimmed: list[str] = []
    used = len("Existing canonical graph:\nNodes:")
    for line in node_lines:
        if used + len(line) + 1 > GRAPH_CONTEXT_MAX_CHARS:
            break
        trimmed.append(line)
        used += len(line) + 1
    return "\n".join(["Existing canonical graph:", "Nodes:", *trimmed])


def _batch_stats(workspace: IngestionWorkspace, batch) -> dict[str, Any]:
    chunk_by_index = {chunk.index: chunk for chunk in workspace.chunks}
    chunks = [chunk_by_index[index] for index in batch.chunk_indexes]
    return {
        "batchIndex": batch.index,
        "chunkCount": len(chunks),
        "chunkIndexes": batch.chunk_indexes,
        "inputChars": batch.content_chars,
        "chunkIds": [chunk.chunk_id for chunk in chunks if chunk.chunk_id],
    }


def _fragment_stats(fragment: GraphPatchFragment | None) -> dict[str, Any]:
    if fragment is None:
        return {
            "nodes": 0,
            "edges": 0,
            "properties": 0,
            "evidenceItems": 0,
            "coverage": 0,
            "mappedCoverage": 0,
            "notRelevantCoverage": 0,
            "coverageDispositions": {},
        }
    property_count = sum(len(node.properties) for node in fragment.nodes)
    evidence_count = sum(len(node.evidence) for node in fragment.nodes)
    evidence_count += sum(
        len(prop.evidence) for node in fragment.nodes for prop in node.properties
    )
    evidence_count += sum(len(edge.evidence) for edge in fragment.edges)
    dispositions: dict[str, int] = {}
    for item in fragment.coverage:
        dispositions[item.decision] = dispositions.get(item.decision, 0) + 1
    return {
        "nodes": len(fragment.nodes),
        "edges": len(fragment.edges),
        "properties": property_count,
        "evidenceItems": evidence_count,
        "coverage": len(fragment.coverage),
        "mappedCoverage": sum(
            1 for item in fragment.coverage if item.decision == "MAPPED"
        ),
        "notRelevantCoverage": sum(
            1
            for item in fragment.coverage
            if item.decision in {"NO_RELEVANT_FACT", "NOT_RELEVANT"}
        ),
        "coverageDispositions": dispositions,
    }


def _workspace_stats(workspace: IngestionWorkspace) -> dict[str, Any]:
    processed = sum(batch.fragment is not None for batch in workspace.batches)
    return {
        "documentChunks": len(workspace.chunks),
        "batches": len(workspace.batches),
        "processedBatches": processed,
        "remainingBatches": len(workspace.batches) - processed,
        "candidateNodes": sum(
            len(batch.fragment.nodes)
            for batch in workspace.batches
            if batch.fragment is not None
        ),
        "candidateEdges": sum(
            len(batch.fragment.edges)
            for batch in workspace.batches
            if batch.fragment is not None
        ),
        "skippedChunks": len(workspace.skipped_chunk_indexes),
        "warningCount": len(workspace.ingestion_warnings),
    }


def _compact_ontology_context(ontology_context: str) -> str:
    """Return the full ontology schema context for model-facing extraction."""
    return ontology_context


def _workspace_precondition(
    ingestion_id: str,
    tool_context: IngestionRuntime,
) -> tuple[IngestionWorkspace | None, dict[str, Any] | None]:
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


async def _receipt_response(
    result: dict[str, Any],
    *,
    artifact_stem: str,
    tool_context: IngestionRuntime,
    require_receipt: bool,
) -> dict[str, Any]:
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
) -> dict[str, Any]:
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
        fill_kwargs = ({"allow_partial_persistence": True} if allow_partial_persistence else {})
        result = service.fill(graph_patch, artifact_digest, source_chunks, **fill_kwargs)
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
        logger.error(
            "[INGESTION_ERROR] Phase: PERSIST_WITH_RECEIPT | Func: _persist_with_receipt | Stem: %s | Error: Fill validation failed: %s",
            artifact_stem,
            exc,
            exc_info=True,
        )
        return {
            "success": False,
            "stage": "validation",
            "terminal": True,
            "validation": exc.result.model_dump(by_alias=True, exclude_none=True),
        }
    except Exception as exc:
        logger.error(
            "[INGESTION_ERROR] Phase: PERSIST_WITH_RECEIPT | Func: _persist_with_receipt | Stem: %s | Message: %s | Exception: %s",
            artifact_stem,
            failure_message,
            exc,
            exc_info=True,
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
    return assessment.result.model_dump(by_alias=True, exclude_none=True)


async def prepare_extraction_context(
    artifact_name: str,
    tool_context: IngestionRuntime,
) -> dict[str, Any]:
    """Load an artifact and prepare source-grounded extraction context."""
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
    """Start a bounded, retryable long-document ingestion workspace."""
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
    """Store one mapper-owned batch fragment; strict validation happens at finalize."""
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
    """Run the single strict graph-validation boundary for the merged document."""
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
    """Persist a finalized graph, with explicit partial persistence when allowed."""
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
    """Return the current terminal/non-terminal state for an ingestion workspace."""

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
    """Map batches, validate once at the document boundary, then optionally persist."""
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
    mapper = _get_semantic_graph_mapper()
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
                "repairInstructions": "Return a replacement fragment for the same batch that resolves the merge conflict.",
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
    """Assess extraction correctness and persistence readiness without writing."""

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
    """Persist only the invocation-scoped, validated graph patch."""

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


class IngestionUseCase:
    """Application workflow for document ingestion, independent of ADK tools."""

    def status(
        self,
        ingestion_id: str,
        runtime: IngestionRuntime,
    ) -> dict[str, Any]:
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
        return validate_graph_patch(graph_patch, runtime)

    async def fill_patch(
        self,
        graph_patch: GraphPatchDraft,
        runtime: IngestionRuntime,
    ) -> dict[str, Any]:
        return await fill_graph_patch(graph_patch, runtime)
