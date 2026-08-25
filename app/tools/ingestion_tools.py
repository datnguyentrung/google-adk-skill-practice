import hashlib
import json
import logging
import os
import re
import time
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
from typing import Any

from google.adk.tools import ToolContext
from google.genai import types

from app.core.schemas.ingestion.document import DocumentChunk
from app.core.schemas.ingestion.graph_patch import GraphPatchDraft, GraphPatchFragment
from app.core.schemas.ingestion.validation import ValidationIssue
from app.core.schemas.ingestion.workspace import (
    IngestionProvenance,
    IngestionRetryState,
    IngestionWorkspace,
)
from app.services.ingestion.fill_factory import create_fill_service
from app.services.ingestion.fill_service import FillValidationError
from app.services.ingestion.orchestrator import (
    BatchExtractor,
    GeminiBatchExtractor,
    InvalidGraphPatchFragmentError,
)
from app.services.ingestion.prepare_extraction_context import ExtractionContextService
from app.services.ingestion.staged_ingestion import (
    IngestionWorkspaceService,
    WorkspaceConflictError,
)
from app.services.ingestion.validate_graph_patch import (
    GraphPatchAssessment,
    GraphPatchValidationService,
)
from app.skills.skill_loader import skill_content_digest

logger = logging.getLogger(__name__)

ARTIFACT_DIGEST_STATE_KEY = "temp:ingestion_source_artifact_digest"
ARTIFACT_NAME_STATE_KEY = "temp:ingestion_source_artifact_name"
VALIDATED_FINGERPRINT_STATE_KEY = "temp:ingestion_validated_fingerprint"
SOURCE_CHUNKS_STATE_KEY = "temp:ingestion_source_chunks"
WORKSPACE_STATE_KEY = "temp:ingestion_workspace"
BATCH_PACE_SECONDS = float(os.getenv("INGESTION_BATCH_PACE_SECONDS", "12"))
DEFAULT_MAX_RETRIES_PER_BATCH = max(1, int(os.getenv("INGESTION_MAX_RETRIES_PER_BATCH", "3")))
INGESTION_SKILL_DIR = Path(__file__).resolve().parents[1] / "skills" / "ingestion"


@lru_cache(maxsize=1)
def _get_context_service() -> ExtractionContextService:
    return ExtractionContextService()


@lru_cache(maxsize=1)
def _get_validation_service() -> GraphPatchValidationService:
    return GraphPatchValidationService()


@lru_cache(maxsize=1)
def _get_workspace_service() -> IngestionWorkspaceService:
    return IngestionWorkspaceService()


@lru_cache(maxsize=1)
def _get_batch_extractor() -> BatchExtractor:
    return GeminiBatchExtractor()


def _orchestration_error_kind(exc: Exception) -> str:
    error_kind = getattr(exc, "error_kind", None)
    if isinstance(error_kind, str):
        return error_kind
    message = str(exc)
    if "response_schema" in message and "Invalid JSON payload" in message:
        return "llm_request_config"
    return "llm_extraction"


def _is_rate_limit_error(exc: Exception) -> bool:
    message = str(exc).upper()
    status_code = getattr(exc, "status_code", None)
    return status_code == 429 or (
        "429" in message and ("RESOURCE_EXHAUSTED" in message or "QUOTA" in message)
    )


def _rate_limit_retry_delay_seconds(exc: Exception, attempt: int) -> float:
    match = re.search(
        r"(?:Please retry in|retryDelay['\": ]+)[^0-9]*([0-9.]+)s",
        str(exc),
        flags=re.IGNORECASE,
    )
    server_delay = float(match.group(1)) if match else 0.0
    backoff = min(60.0, 8.0 * (2 ** max(0, attempt - 1)))
    return max(BATCH_PACE_SECONDS, server_delay, backoff)


def _is_retryable_extraction_error(exc: Exception) -> bool:
    return (
        isinstance(exc, InvalidGraphPatchFragmentError)
        or _is_rate_limit_error(exc)
        or bool(getattr(exc, "retryable", False))
    )


def _extractor_retry_error(exc: Exception) -> dict[str, Any]:
    return {
        "stage": "extractor_schema",
        "errorKind": _orchestration_error_kind(exc),
        "message": _orchestration_error_message(exc),
        "shapeSummary": getattr(exc, "summary", {}),
        "repairInstructions": (
            "Convert the response to the canonical GraphPatchFragment object. "
            "Return one top-level object with nodes, edges, coverage, warnings. "
            "Do not return an array. Do not use entities, chunkStatus, id, class, "
            "or a property map object. Each node property must be an entry with "
            "propertyName, value, and evidence."
        ),
    }


def _orchestration_error_message(exc: Exception) -> str:
    if _orchestration_error_kind(exc) == "llm_request_config":
        return (
            "Internal Gemini extractor request configuration is invalid; "
            "the uploaded document was not processed and no graph was persisted"
        )
    return str(exc)


def _delete_state(tool_context: ToolContext, key: str) -> None:
    if key in tool_context.state:
        del tool_context.state[key]


def _clear_validation_gate(tool_context: ToolContext) -> None:
    _delete_state(tool_context, VALIDATED_FINGERPRINT_STATE_KEY)


def _pace_next_model_turn(tool_context: ToolContext) -> None:
    """Space expensive Gemini turns to stay below per-minute input quotas."""
    if BATCH_PACE_SECONDS > 0 and isinstance(tool_context, ToolContext):
        time.sleep(BATCH_PACE_SECONDS)


def _skill_digest() -> str:
    return skill_content_digest(INGESTION_SKILL_DIR)


def _current_provenance(tool_context: ToolContext) -> IngestionProvenance:
    return IngestionProvenance(
        artifactDigest=tool_context.state.get(
            ARTIFACT_DIGEST_STATE_KEY,
            "MISSING",
        ),
        ontologyDigest=_get_validation_service().compiler.ontology_digest,
        skillDigest=_skill_digest(),
    )


def _load_workspace(tool_context: ToolContext) -> IngestionWorkspace | None:
    raw = tool_context.state.get(WORKSPACE_STATE_KEY)
    if raw is None:
        return None
    return IngestionWorkspace.model_validate(raw)


def _store_workspace(
    tool_context: ToolContext,
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
        }
    property_count = sum(len(node.properties) for node in fragment.nodes)
    evidence_count = sum(len(node.evidence) for node in fragment.nodes)
    evidence_count += sum(
        len(prop.evidence)
        for node in fragment.nodes
        for prop in node.properties
    )
    evidence_count += sum(len(edge.evidence) for edge in fragment.edges)
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
            1 for item in fragment.coverage if item.decision == "NOT_RELEVANT"
        ),
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
    }


def _fragment_fingerprint(fragment: GraphPatchFragment) -> str:
    material = json.dumps(
        fragment.model_dump(by_alias=True, mode="json"),
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _unchanged_retry_issue(
    workspace: IngestionWorkspace,
    batch_index: int,
    summary: dict[str, Any],
    fragment: GraphPatchFragment,
) -> ValidationIssue | None:
    previous = workspace.retry_states.get(str(batch_index))
    if previous is None:
        return None
    affected = summary["coverageNotEvidencedChunkIndexes"]
    same_coverage_failure = bool(affected) and (
        previous.coverage_not_evidenced_chunk_indexes == affected
    )
    same_fragment_failure = (
        previous.fragment_fingerprint == _fragment_fingerprint(fragment)
        and previous.error_codes == summary["codes"]
    )
    if not same_coverage_failure and not same_fragment_failure:
        return None
    return ValidationIssue(
        code="UNCHANGED_RETRY",
        message=(
            "Retry did not materially change the failing extraction. Do not submit "
            "the same fragment/error again; apply the repair instructions or stop."
        ),
        location=f"batches.{batch_index}",
    )


def _remember_retry_state(
    workspace: IngestionWorkspace,
    batch_index: int,
    fragment: GraphPatchFragment,
    summary: dict[str, Any],
) -> None:
    workspace.retry_states[str(batch_index)] = IngestionRetryState(
        batchIndex=batch_index,
        coverageNotEvidencedChunkIndexes=summary["coverageNotEvidencedChunkIndexes"],
        fragmentFingerprint=_fragment_fingerprint(fragment),
        errorCodes=summary["codes"],
    )


def _batch_retry_payload(
    workspace: IngestionWorkspace,
    batch_index: int,
) -> dict[str, Any]:
    batch = workspace.batches[batch_index]
    return {
        "retryRequired": True,
        "nextAction": "correct_and_resubmit_same_batch",
        "nextBatch": _batch_payload(workspace, batch),
    }


def _issue_code(issue: ValidationIssue) -> str:
    return issue.code.value if hasattr(issue.code, "value") else str(issue.code)


def _schema_error_locations(exc: Exception) -> list[str]:
    errors = getattr(exc, "errors", None)
    if callable(errors):
        return [
            ".".join(str(part) for part in item.get("loc", ()))
            for item in errors()
            if item.get("loc")
        ]
    locations: list[str] = []
    for line in str(exc).splitlines():
        stripped = line.strip()
        if re.match(r"^(nodes|edges|coverage|warnings)(\.|\b)", stripped):
            locations.append(stripped)
    return locations


def _batch_error_summary(
    issues: list[ValidationIssue],
    *,
    schema_error_locations: list[str] | None = None,
) -> dict[str, Any]:
    codes: list[str] = []
    coverage_not_evidenced: list[int] = []
    evidence_text_locations: list[str] = []
    for issue in issues:
        code = _issue_code(issue)
        if code not in codes:
            codes.append(code)
        if code == "COVERAGE_NOT_EVIDENCED":
            match = re.fullmatch(r"coverage\.(\d+)", issue.location)
            if match:
                coverage_not_evidenced.append(int(match.group(1)))
        elif code == "EVIDENCE_TEXT_NOT_IN_SOURCE":
            evidence_text_locations.append(issue.location)
    return {
        "codes": codes,
        "coverageNotEvidencedChunkIndexes": sorted(set(coverage_not_evidenced)),
        "evidenceTextNotInSourceLocations": evidence_text_locations,
        "schemaErrorLocations": schema_error_locations or [],
    }


def _affected_chunks_payload(
    workspace: IngestionWorkspace,
    chunk_indexes: list[int],
) -> list[dict[str, Any]]:
    chunk_by_index = {chunk.index: chunk for chunk in workspace.chunks}
    return [
        chunk_by_index[index].model_dump(by_alias=True, exclude_none=True)
        for index in sorted(set(chunk_indexes))
        if index in chunk_by_index
    ]


def _repair_instructions(summary: dict[str, Any]) -> str:
    instructions: list[str] = []
    codes = set(summary["codes"])
    if "BATCH_CONFLICT" in codes:
        instructions.append("Resolve the cross-batch conflict using the returned conflict object; do not resubmit the same scalar property/value conflict.")
    if "PROPERTY_VALUE_NOT_GROUNDED" in codes:
        instructions.append("For unsupported property values, copy the exact source wording/value or omit the property; do not paraphrase business facts.")
    if summary["evidenceTextNotInSourceLocations"]:
        instructions.append("Use verbatim evidence text from the cited chunk.")
    if "DERIVED_PROPERTY_REQUIRES_EDGE_EVIDENCE" in codes:
        instructions.append("Add the grounded relationship edge that supports the derived ruleType, or omit the unsupported rule node.")
    if summary["coverageNotEvidencedChunkIndexes"]:
        instructions.append("Do not resubmit unchanged coverage. For each coverageNotEvidenced chunk, add at least one grounded property/edge fact only when it contributes a distinct graph fact; otherwise mark it NOT_RELEVANT with a source-based reason.")
    if summary["schemaErrorLocations"]:
        instructions.append("Fix every schema error before resubmitting the same batch; evidence objects must include source, chunkIndex, section when present, and text.")
    return " ".join(instructions) or "Correct the batch validation errors and resubmit this same batch."


def _conflict_repair_instruction(conflict: dict[str, Any]) -> str:
    if conflict.get("propertyName") == "pskg:fee":
        return (
            "Do not add multiple independent fee types as scalar "
            "BankingProduct.pskg:fee values. Model each distinct fee/pricing "
            "fact as its own pskg:BusinessRule with pskg:businessRuleCondition "
            "and connect it from the product using pskg:hasSalesConditionRule. "
            "Keep exact source evidence and do not emit pskg:ruleType directly."
        )
    return (
        "If this is the same semantic property, keep one canonical value and "
        "merge evidence. If the source truly states different singleton values, "
        "stop and report the conflict instead of overwriting."
    )


def _conflict_payload(
    exc: Exception,
    *,
    batch_index: int,
) -> dict[str, Any] | None:
    conflict = getattr(exc, "conflict", None)
    if not isinstance(conflict, dict) or not conflict:
        return None
    payload = {"batchIndex": batch_index, **conflict}
    payload["repairInstruction"] = _conflict_repair_instruction(payload)
    return payload


def _batch_validation_response(
    workspace: IngestionWorkspace,
    batch_index: int,
    issues: list[ValidationIssue],
    *,
    schema_error_locations: list[str] | None = None,
    conflict: dict[str, Any] | None = None,
    fragment: GraphPatchFragment | None = None,
    retry_required: bool = True,
    next_action: str = "correct_and_resubmit_same_batch",
) -> dict[str, Any]:
    summary = _batch_error_summary(issues, schema_error_locations=schema_error_locations)
    retry = _batch_retry_payload(workspace, batch_index)
    preview = issues[:10]
    response = {
        "success": False,
        "stage": "batch_validation",
        "batchIndex": batch_index,
        "terminal": not retry_required,
        "retryRequired": retry_required,
        "nextAction": next_action,
        "errorCount": len(issues),
        "errorsTruncated": len(issues) > len(preview),
        "errors": [issue.model_dump(by_alias=True, exclude_none=True) for issue in preview],
        "errorSummary": summary,
        "conflict": conflict,
        "fragmentStats": _fragment_stats(fragment),
        "repairInstructions": (
            f"{_repair_instructions(summary)} {conflict['repairInstruction']}"
            if conflict and conflict.get("repairInstruction")
            else _repair_instructions(summary)
        ),
        "affectedChunkIndexes": summary["coverageNotEvidencedChunkIndexes"],
        "affectedChunks": _affected_chunks_payload(
            workspace,
            summary["coverageNotEvidencedChunkIndexes"],
        ),
    }
    if retry_required:
        response["nextBatch"] = retry["nextBatch"]
    if conflict is None:
        response.pop("conflict")
    return response

def _compact_ontology_context(ontology_context: str) -> str:
    """Keep only model-facing ontology identifiers needed for extraction."""
    lines: list[str] = []
    section: str | None = None
    for raw in ontology_context.splitlines():
        line = raw.strip()
        if line.startswith("CLASS:"):
            section = "class"
            lines.append(line)
        elif line in {"PROPERTIES:", "OUTGOING EDGES:"}:
            section = line
            lines.append(line)
        elif line.endswith(":"):
            section = None
        elif line.startswith("-") and section in {"PROPERTIES:", "OUTGOING EDGES:"}:
            lines.append(line)
    return "\n".join(lines)


def _workspace_precondition(
    ingestion_id: str,
    tool_context: ToolContext,
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
    tool_context: ToolContext,
    require_receipt: bool,
) -> dict[str, Any]:
    receipt = result.get("receipt")
    if not isinstance(receipt, dict):
        if require_receipt:
            raise RuntimeError("Fill service did not return a persisted graph receipt")
        return {"success": True, "stage": "completed", "terminal": True, **result}
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
        },
    )
    verified = bool(receipt.get("verified"))
    return {
        "success": verified,
        "stage": "completed" if verified else "readback",
        "terminal": True,
        "commitStatus": result.get("commitStatus", "committed"),
        "nodes": result.get("nodes", 0),
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
    validation_service: GraphPatchValidationService,
    artifact_stem: str,
    tool_context: ToolContext,
    require_receipt: bool,
    invalidate_gate: Callable[[], None],
    failure_message: str,
) -> dict[str, Any]:
    service = None
    result = None
    try:
        service = create_fill_service(validation_service=validation_service)
        result = service.fill(graph_patch, artifact_digest, source_chunks)
        logger.info(
            "FILL_RESULT nodes=%s edges=%s status=%s commit_status=%s",
            result.get("nodes", 0),
            result.get("edges", 0),
            result.get("status"),
            result.get("commitStatus"),
        )
        logger.info(
            "PERSIST_RESULT nodes_created=%s nodes_merged=%s edges_created=%s",
            result.get("nodes", 0),
            0,
            result.get("edges", 0),
        )
        return await _receipt_response(
            result,
            artifact_stem=artifact_stem,
            tool_context=tool_context,
            require_receipt=require_receipt,
        )
    except FillValidationError as exc:
        invalidate_gate()
        return {
            "success": False,
            "stage": "validation",
            "terminal": True,
            "validation": exc.result.model_dump(by_alias=True, exclude_none=True),
        }
    except Exception as exc:
        logger.exception(failure_message)
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
    tool_context: ToolContext,
) -> dict[str, Any]:
    """Load an artifact and prepare source-grounded extraction context."""

    _clear_validation_gate(tool_context)
    _delete_state(tool_context, ARTIFACT_DIGEST_STATE_KEY)
    _delete_state(tool_context, ARTIFACT_NAME_STATE_KEY)
    _delete_state(tool_context, SOURCE_CHUNKS_STATE_KEY)
    _delete_state(tool_context, WORKSPACE_STATE_KEY)

    try:
        artifact = await tool_context.load_artifact(filename=artifact_name)
        if artifact is None:
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
            "INGESTION_DOCUMENT document=%s total_chars=%s total_lines=%s total_chunks=%s",
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
        logger.exception("Failed to prepare extraction context for '%s'", artifact_name)
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
    tool_context: ToolContext,
) -> dict[str, Any]:
    """Start a bounded, retryable long-document ingestion workspace."""

    prepared = await prepare_extraction_context(artifact_name, tool_context)
    if not prepared.get("success"):
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
    for batch in workspace.batches:
        logger.info(
            "EXTRACTION_BATCH ingestion_id=%s batch=%s chunk_ids=%s input_chars=%s",
            workspace.ingestion_id,
            batch.index,
            batch.chunk_indexes,
            batch.content_chars,
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
        "ontologyCatalog": _compact_ontology_context(prepared["ontology_context"]),
        "artifactDigest": workspace.artifact_digest,
        "ontologyDigest": workspace.ontology_digest,
        "skillDigest": workspace.skill_digest,
        "nextBatch": _batch_payload(workspace, first_batch),
    }


def submit_ingestion_batch(
    ingestion_id: str,
    batch_index: int,
    graph_fragment: GraphPatchFragment,
    tool_context: ToolContext,
) -> dict[str, Any]:
    """Replace one batch fragment idempotently and return the next pending batch."""

    workspace, error = _workspace_precondition(ingestion_id, tool_context)
    if error is not None or workspace is None:
        return error
    try:
        fragment = GraphPatchFragment.model_validate(graph_fragment)
        logger.info(
            "EXTRACTION_RESULT ingestion_id=%s batch=%s stats=%s",
            ingestion_id,
            batch_index,
            _fragment_stats(fragment),
        )
        candidate = _get_workspace_service().submit(
            workspace,
            batch_index,
            fragment,
        )
        batch = workspace.batches[batch_index]
        expected_indexes = set(batch.chunk_indexes)
        batch_chunks = [
            chunk for chunk in workspace.chunks if chunk.index in expected_indexes
        ]
        grounding_issues = _get_validation_service().source_grounding.validate(
            fragment,
            batch_chunks,
        )
        if grounding_issues:
            summary = _batch_error_summary(grounding_issues)
            logger.info(
                "VALIDATION_RESULT ingestion_id=%s batch=%s valid_nodes=0 invalid_nodes=%s valid_edges=0 invalid_edges=%s errors=%s",
                ingestion_id,
                batch_index,
                len(fragment.nodes),
                len(fragment.edges),
                summary["codes"],
            )
            for issue in grounding_issues:
                logger.info(
                    "VALIDATION_REJECTION ingestion_id=%s batch=%s code=%s location=%s node=%s property=%s edge=%s",
                    ingestion_id,
                    batch_index,
                    _issue_code(issue),
                    issue.location,
                    issue.node_temp_id,
                    issue.property_name,
                    issue.edge_name,
                )
            unchanged_issue = _unchanged_retry_issue(
                workspace,
                batch_index,
                summary,
                fragment,
            )
            if unchanged_issue is not None:
                response = _batch_validation_response(
                    workspace,
                    batch_index,
                    [unchanged_issue, *grounding_issues],
                    fragment=fragment,
                    retry_required=False,
                    next_action="explicit_extraction_failure",
                )
                _store_workspace(tool_context, workspace)
                _pace_next_model_turn(tool_context)
                return response
            _remember_retry_state(workspace, batch_index, fragment, summary)
            _store_workspace(tool_context, workspace)
            response = _batch_validation_response(
                workspace,
                batch_index,
                grounding_issues,
                fragment=fragment,
            )
            _pace_next_model_turn(tool_context)
            return response
        candidate.retry_states.pop(str(batch_index), None)
        workspace = candidate
    except (ValueError, WorkspaceConflictError) as exc:
        conflict = _conflict_payload(exc, batch_index=batch_index)
        if conflict is not None:
            logger.info(
                "VALIDATION_REJECTION ingestion_id=%s batch=%s code=BATCH_CONFLICT node=%s property=%s existingValue=%r incomingValue=%r existingEvidence=%s incomingEvidence=%s",
                ingestion_id,
                batch_index,
                conflict.get("nodeTempId"),
                conflict.get("propertyName"),
                conflict.get("existingValue"),
                conflict.get("incomingValue"),
                conflict.get("existingEvidence"),
                conflict.get("incomingEvidence"),
            )
        issues = [
            ValidationIssue(
                code="BATCH_CONFLICT",
                message=str(exc),
                location=f"batches.{batch_index}",
                node_temp_id=(
                    conflict.get("nodeTempId")
                    if conflict is not None
                    else None
                ),
                property_name=(
                    conflict.get("propertyName")
                    if conflict is not None
                    else None
                ),
            )
        ]
        if 0 <= batch_index < len(workspace.batches):
            retry_fragment = (
                fragment
                if "fragment" in locals() and isinstance(fragment, GraphPatchFragment)
                else None
            )
            schema_locations = _schema_error_locations(exc)
            summary = _batch_error_summary(issues, schema_error_locations=schema_locations)
            if retry_fragment is not None:
                unchanged_issue = _unchanged_retry_issue(
                    workspace, batch_index, summary, retry_fragment
                )
                if unchanged_issue is not None:
                    response = _batch_validation_response(
                        workspace, batch_index, [unchanged_issue, *issues],
                        schema_error_locations=schema_locations, conflict=conflict,
                        fragment=retry_fragment, retry_required=False,
                        next_action="explicit_extraction_failure",
                    )
                    _store_workspace(tool_context, workspace)
                    _pace_next_model_turn(tool_context)
                    return response
                _remember_retry_state(workspace, batch_index, retry_fragment, summary)
                _store_workspace(tool_context, workspace)
            response = _batch_validation_response(
                workspace, batch_index, issues,
                schema_error_locations=schema_locations,
                conflict=conflict, fragment=retry_fragment,
            )
            _pace_next_model_turn(tool_context)
            return response
        return {
            "success": False,
            "stage": "batch_validation",
            "batchIndex": batch_index,
            "errors": [
                issue.model_dump(by_alias=True, exclude_none=True)
                for issue in issues
            ],
        }
    _store_workspace(tool_context, workspace)
    next_batch = _get_workspace_service().next_batch(workspace)
    processed = sum(batch.fragment is not None for batch in workspace.batches)
    logger.info(
        "VALIDATION_RESULT ingestion_id=%s batch=%s valid_nodes=%s invalid_nodes=0 valid_edges=%s invalid_edges=0",
        ingestion_id,
        batch_index,
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
    _pace_next_model_turn(tool_context)
    return response


def finalize_ingestion(
    ingestion_id: str,
    tool_context: ToolContext,
) -> dict[str, Any]:
    """Validate the complete merged workspace and lock its persistence fingerprint."""

    workspace, error = _workspace_precondition(ingestion_id, tool_context)
    if error is not None or workspace is None:
        return error
    pending = [batch.index for batch in workspace.batches if batch.fragment is None]
    if pending:
        return {
            "success": False,
            "stage": "batch_incomplete",
            "terminal": False,
            "errors": [
                ValidationIssue(
                    code="BATCH_INCOMPLETE",
                    message=f"Pending batch indexes: {pending}",
                    location="batches",
                ).model_dump(by_alias=True, exclude_none=True)
            ],
        }
    patch = _get_workspace_service().merged_patch(workspace)
    validation_service = _get_validation_service()
    assessment = validation_service.assess(
        patch,
        workspace.artifact_digest,
        workspace.chunks,
    )
    logger.info(
        "VALIDATION_RESULT ingestion_id=%s valid_nodes=%s invalid_nodes=%s valid_edges=%s invalid_edges=%s errors=%s readiness=%s",
        ingestion_id,
        assessment.result.node_count if assessment.result.valid_for_extraction else 0,
        assessment.result.node_count if not assessment.result.valid_for_extraction else 0,
        assessment.result.edge_count if assessment.result.valid_for_extraction else 0,
        assessment.result.edge_count if not assessment.result.valid_for_extraction else 0,
        [item.code.value for item in assessment.result.errors],
        [item.code.value for item in assessment.result.readiness_issues],
    )
    for issue in assessment.result.errors:
        logger.info(
            "VALIDATION_REJECTION ingestion_id=%s code=%s location=%s node=%s property=%s edge=%s",
            ingestion_id,
            _issue_code(issue),
            issue.location,
            issue.node_temp_id,
            issue.property_name,
            issue.edge_name,
        )
    workspace.finalized_patch = patch.model_dump(by_alias=True, mode="json")
    if (
        assessment.result.valid_for_extraction
        and assessment.result.valid_for_persistence
        and assessment.fingerprint is not None
    ):
        workspace.validated_fingerprint = assessment.fingerprint
    else:
        workspace.validated_fingerprint = None
    _store_workspace(tool_context, workspace)
    public = _public_assessment(assessment)
    return {
        "success": assessment.result.valid_for_extraction,
        "stage": (
            "ready_to_fill"
            if assessment.result.valid_for_persistence
            else "readiness_gate"
        ),
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
    tool_context: ToolContext,
) -> dict[str, Any]:
    """Persist only the finalized graph locked inside the named workspace."""

    workspace, error = _workspace_precondition(ingestion_id, tool_context)
    if error is not None or workspace is None:
        return error
    if workspace.validated_fingerprint is None or workspace.finalized_patch is None:
        return {
            "success": False,
            "stage": "validation_precondition",
            "terminal": True,
            "errors": [
                ValidationIssue(
                    code="VALIDATION_PRECONDITION",
                    message="finalize_ingestion must pass persistence readiness first",
                    location="ingestionId",
                ).model_dump(by_alias=True, exclude_none=True)
            ],
        }
    validation_service = _get_validation_service()
    candidate = validation_service.fingerprint_candidate(
        workspace.finalized_patch,
        workspace.artifact_digest,
    )
    if candidate != workspace.validated_fingerprint:
        workspace.validated_fingerprint = None
        _store_workspace(tool_context, workspace)
        return {
            "success": False,
            "stage": "validation_precondition",
            "terminal": True,
            "errors": [
                ValidationIssue(
                    code="VALIDATION_PRECONDITION",
                    message="Finalized graph fingerprint no longer matches the lock",
                    location="ingestionId",
                ).model_dump(by_alias=True, exclude_none=True)
            ],
        }
    def invalidate_workspace_gate() -> None:
        workspace.validated_fingerprint = None
        _store_workspace(tool_context, workspace)

    return await _persist_with_receipt(
        graph_patch=workspace.finalized_patch,
        artifact_digest=workspace.artifact_digest,
        source_chunks=workspace.chunks,
        validation_service=validation_service,
        artifact_stem=ingestion_id[:12],
        tool_context=tool_context,
        require_receipt=True,
        invalidate_gate=invalidate_workspace_gate,
        failure_message="Failed to persist finalized ingestion workspace",
    )


def get_ingestion_status(
    ingestion_id: str,
    tool_context: ToolContext,
) -> dict[str, Any]:
    """Return the current terminal/non-terminal state for an ingestion workspace."""

    workspace, error = _workspace_precondition(ingestion_id, tool_context)
    if error is not None or workspace is None:
        return {**error, "terminal": True}
    next_batch = _get_workspace_service().next_batch(workspace)
    status = {
        "success": True,
        "stage": (
            "ready_to_finalize"
            if next_batch is None
            else "batching"
        ),
        "terminal": False,
        "ingestionId": ingestion_id,
        "workspaceStats": _workspace_stats(workspace),
    }
    if next_batch is not None:
        status["nextBatch"] = _batch_payload(workspace, next_batch)
    return status


async def ingest_document_end_to_end(
    artifact_name: str,
    tool_context: ToolContext,
    persist: bool = True,
    max_retries_per_batch: int = DEFAULT_MAX_RETRIES_PER_BATCH,
) -> dict[str, Any]:
    """Run long-document ingestion to a real terminal state in one tool call."""

    begin = await begin_ingestion(artifact_name, tool_context)
    if not begin.get("success"):
        return {**begin, "terminal": True}

    ingestion_id = begin["ingestionId"]
    ontology_catalog = begin.get("ontologyCatalog", "")
    extractor = _get_batch_extractor()
    response: dict[str, Any] = begin
    processed_batches = 0

    while response.get("stage") == "batching":
        batch_payload = response.get("nextBatch")
        if not isinstance(batch_payload, dict):
            return {
                "success": False,
                "stage": "explicit_extraction_failure",
                "terminal": True,
                "ingestionId": ingestion_id,
                "errors": [
                    ValidationIssue(
                        code="ORCHESTRATION_FAILED",
                        message="Batching response did not include nextBatch",
                        location="nextBatch",
                    ).model_dump(by_alias=True, exclude_none=True)
                ],
                "workspaceStats": (
                    _workspace_stats(workspace)
                    if (workspace := _load_workspace(tool_context)) is not None
                    else {}
                ),
            }
        batch_index = int(batch_payload["batchIndex"])
        previous_error: dict[str, Any] | None = None
        for attempt in range(1, max_retries_per_batch + 1):
            try:
                fragment = extractor.extract_fragment(
                    batch_payload=batch_payload,
                    ontology_catalog=ontology_catalog,
                    previous_error=previous_error,
                )
            except Exception as exc:
                logger.exception(
                    "ORCHESTRATION_FAILED ingestion_id=%s batch=%s attempt=%s",
                    ingestion_id,
                    batch_index,
                    attempt,
                )
                if (
                    _is_retryable_extraction_error(exc)
                    and attempt < max_retries_per_batch
                ):
                    previous_error = _extractor_retry_error(exc)
                    if _is_rate_limit_error(exc):
                        delay = _rate_limit_retry_delay_seconds(exc, attempt)
                        logger.warning(
                            "INGESTION_RATE_LIMIT_BACKOFF ingestion_id=%s batch=%s attempt=%s delay_seconds=%.2f",
                            ingestion_id, batch_index, attempt, delay,
                        )
                        time.sleep(delay)
                    continue
                return {
                    "success": False,
                    "stage": "explicit_extraction_failure",
                    "terminal": True,
                    "ingestionId": ingestion_id,
                    "batchIndex": batch_index,
                    "attempt": attempt,
                    "errorKind": _orchestration_error_kind(exc),
                    "errors": [
                        ValidationIssue(
                            code="ORCHESTRATION_FAILED",
                            message=_orchestration_error_message(exc),
                            location=f"batches.{batch_index}.extract",
                        ).model_dump(by_alias=True, exclude_none=True)
                    ],
                    "workspaceStats": (
                        _workspace_stats(workspace)
                        if (workspace := _load_workspace(tool_context)) is not None
                        else {}
                    ),
                }
            response = submit_ingestion_batch(
                ingestion_id,
                batch_index,
                fragment,
                tool_context,
            )
            if response.get("success"):
                processed_batches = int(response.get("processedBatches", 0))
                break
            previous_error = {
                key: response.get(key)
                for key in (
                    "stage",
                    "batchIndex",
                    "errorSummary",
                    "repairInstructions",
                    "affectedChunkIndexes",
                )
                if key in response
            }
            if not response.get("retryRequired"):
                return {
                    **response,
                    "stage": "explicit_extraction_failure",
                    "terminal": True,
                    "ingestionId": ingestion_id,
                    "processedBatches": processed_batches,
                    "workspaceStats": (
                        _workspace_stats(workspace)
                        if (workspace := _load_workspace(tool_context)) is not None
                        else response.get("workspaceStats", {})
                    ),
                }
        else:
            return {
                **response,
                "success": False,
                "stage": "explicit_extraction_failure",
                "terminal": True,
                "ingestionId": ingestion_id,
                "batchIndex": batch_index,
                "processedBatches": processed_batches,
                "errors": response.get("errors", []),
                "message": (
                    f"Batch {batch_index} did not pass validation after "
                    f"{max_retries_per_batch} attempts"
                ),
                "workspaceStats": (
                    _workspace_stats(workspace)
                    if (workspace := _load_workspace(tool_context)) is not None
                    else response.get("workspaceStats", {})
                ),
            }

    finalized = finalize_ingestion(ingestion_id, tool_context)
    if finalized.get("stage") != "ready_to_fill":
        return {**finalized, "terminal": True}
    if not persist:
        return {
            **finalized,
            "stage": "ready_to_fill",
            "terminal": True,
            "persisted": False,
        }
    filled = await fill_ingestion(ingestion_id, tool_context)
    return {
        **filled,
        "terminal": True,
        "ingestionId": ingestion_id,
        "workspaceStats": (
            _workspace_stats(workspace)
            if (workspace := _load_workspace(tool_context)) is not None
            else finalized.get("workspaceStats", {})
        ),
    }


def validate_graph_patch(
    graph_patch: GraphPatchDraft,
    tool_context: ToolContext,
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
    tool_context: ToolContext,
) -> dict[str, Any]:
    """Persist only the invocation-scoped, validated graph patch."""

    artifact_digest = tool_context.state.get(ARTIFACT_DIGEST_STATE_KEY)
    validation_service = _get_validation_service()
    validated_fingerprint = tool_context.state.get(
        VALIDATED_FINGERPRINT_STATE_KEY
    )
    candidate_fingerprint = validation_service.fingerprint_candidate(
        graph_patch,
        artifact_digest,
    )
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

    source_chunks = tool_context.state.get(SOURCE_CHUNKS_STATE_KEY)
    assessment = validation_service.assess(
        graph_patch,
        artifact_digest,
        source_chunks,
    )
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


INGESTION_TOOLS = {
    "ingest_document_end_to_end": ingest_document_end_to_end,
    "begin_ingestion": begin_ingestion,
    "submit_ingestion_batch": submit_ingestion_batch,
    "finalize_ingestion": finalize_ingestion,
    "fill_ingestion": fill_ingestion,
    "get_ingestion_status": get_ingestion_status,
    "prepare_extraction_context": prepare_extraction_context,
    "validate_graph_patch": validate_graph_patch,
    "fill_graph_patch": fill_graph_patch,
}


def get_ingestion_tools() -> list:
    return list(INGESTION_TOOLS.values())
