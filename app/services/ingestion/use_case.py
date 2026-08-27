import asyncio
import hashlib
import json
import logging
import os
import random
import re
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol

import httpx
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
from app.services.ingestion.business_rule_edges import (
    RULE_TYPE_PROPERTY,
    business_rule_edge_issues,
    fragments_with_replacement,
)
from app.services.ingestion.fragment_grounding_repair import repair_fragment_grounding
from app.services.ingestion.orchestrator import (
    BatchExtractor,
    GeminiBatchExtractor,
    InvalidGraphPatchFragmentError,
)
from app.services.ingestion.prepare_extraction_context import ExtractionContextService
from app.services.ingestion.relationship_reconciliation import (
    RelationshipReconciler,
    reconciliation_triggered,
)
from app.services.ingestion.staged_ingestion import (
    IngestionWorkspaceService,
    WorkspaceConflictError,
)
from app.services.ingestion.validate_graph_patch import (
    GraphPatchAssessment,
    GraphPatchValidationService,
)
from app.services.ingestion.semantic_grounding import (
    create_default_semantic_grounding_judge,
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
BATCH_PACE_SECONDS = float(os.getenv("INGESTION_BATCH_PACE_SECONDS", "12"))
DEFAULT_MAX_RETRIES_PER_BATCH = max(
    1, int(os.getenv("INGESTION_MAX_RETRIES_PER_BATCH", "3"))
)
DEFAULT_MAX_TRANSPORT_RETRIES = max(
    1, int(os.getenv("INGESTION_MAX_TRANSPORT_RETRIES", "3"))
)
DEFAULT_MAX_RATE_LIMIT_RETRIES = max(
    1, int(os.getenv("INGESTION_MAX_RATE_LIMIT_RETRIES", "3"))
)
INGESTION_TRANSPORT_RETRY_BASE_SECONDS = max(
    0.0, float(os.getenv("INGESTION_TRANSPORT_RETRY_BASE_SECONDS", "2.0"))
)
INGESTION_TRANSPORT_RETRY_MAX_SECONDS = max(
    0.0, float(os.getenv("INGESTION_TRANSPORT_RETRY_MAX_SECONDS", "60.0"))
)
INGESTION_TRANSPORT_RETRY_JITTER_SECONDS = max(
    0.0, float(os.getenv("INGESTION_TRANSPORT_RETRY_JITTER_SECONDS", "1.0"))
)
GRAPH_CONTEXT_MAX_CHARS = 6000
INGESTION_SKILL_DIR = Path(__file__).resolve().parents[2] / "skills" / "ingestion"

_SENSITIVE_TRACE_PATTERN = re.compile(
    r"(?i)(authorization\s*[:=]\s*\S+|api[_-]?key\s*[:=]\s*\S+|"
    r"token\s*[:=]\s*\S+|password\s*[:=]\s*\S+|pin\s*[:=]\s*\S+|"
    r"otp\s*[:=]\s*\S+)"
)


@dataclass(frozen=True)
class TargetedRepairScope:
    rejected_nodes: frozenset[str]
    rejected_node_evidence: frozenset[str]
    rejected_properties: frozenset[tuple[str, str]]
    rejected_edges: frozenset[tuple[str, str, str]]
    rejected_coverage_chunks: frozenset[int]


def _safe_trace_preview(value: Any, *, limit: int = 500) -> str:
    text = value if isinstance(value, str) else repr(value)
    text = _SENSITIVE_TRACE_PATTERN.sub("<REDACTED>", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    half = max(1, limit // 2)
    return f"{text[:half]} ... {text[-half:]}"


def _trace_evidence(items) -> list[dict[str, Any]]:
    return [
        {
            "chunkIndex": item.chunk_index,
            "source": item.source,
            "section": item.section,
            "text": _safe_trace_preview(item.text),
        }
        for item in items
    ]


def _trace_node_properties(node) -> list[str]:
    return [
        f"{prop.property_name}={_safe_trace_preview(prop.value, limit=220)!r}"
        for prop in node.properties
    ]


def _trace_fragment_details(
    fragment: GraphPatchFragment,
    *,
    tag: str,
    ingestion_id: str | None = None,
    batch_index: int | None = None,
    attempt: int | None = None,
) -> None:
    logger.debug(
        "[%s] ingestion_id=%s batch=%s attempt=%s stats=%s",
        tag,
        ingestion_id,
        batch_index,
        attempt,
        _fragment_stats(fragment),
    )
    for node in fragment.nodes:
        logger.debug(
            "[EXTRACTION_NODE] ingestion_id=%s batch=%s attempt=%s node_id=%s "
            "class=%s property_count=%s evidence_count=%s origin=%s",
            ingestion_id,
            batch_index,
            attempt,
            node.temp_id,
            node.class_name,
            len(node.properties),
            len(node.evidence),
            "REPAIR_GENERATED" if "REPAIR" in tag else "LLM_DERIVED",
        )
        for prop_index, prop in enumerate(node.properties):
            logger.debug(
                "[EXTRACTION_PROPERTY] ingestion_id=%s batch=%s attempt=%s "
                "node_id=%s property_index=%s property=%s value=%r "
                "evidence_count=%s evidence=%s origin=%s",
                ingestion_id,
                batch_index,
                attempt,
                node.temp_id,
                prop_index,
                prop.property_name,
                _safe_trace_preview(prop.value),
                len(prop.evidence),
                _trace_evidence(prop.evidence),
                "REPAIR_GENERATED" if "REPAIR" in tag else "LLM_DERIVED",
            )
    for edge in fragment.edges:
        logger.debug(
            "[EXTRACTION_EDGE] ingestion_id=%s batch=%s attempt=%s edge=%s "
            "source_node=%s target_node=%s evidence_count=%s evidence=%s",
            ingestion_id,
            batch_index,
            attempt,
            edge.edge_name,
            edge.source_temp_id,
            edge.target_temp_id,
            len(edge.evidence),
            _trace_evidence(edge.evidence),
        )
    for item in fragment.coverage:
        logger.debug(
            "[COVERAGE_RESULT] ingestion_id=%s batch=%s attempt=%s chunk_id=%s "
            "disposition=%s reason=%r",
            ingestion_id,
            batch_index,
            attempt,
            item.chunk_index,
            item.decision,
            _safe_trace_preview(item.reason),
        )


def _log_duplicate_property_warnings(
    fragment: GraphPatchFragment,
    *,
    ingestion_id: str | None = None,
    batch_index: int | None = None,
    attempt: int | None = None,
) -> list[dict[str, Any]]:
    duplicates: list[dict[str, Any]] = []
    for node in fragment.nodes:
        counts = Counter(prop.property_name for prop in node.properties)
        for property_name, count in sorted(counts.items()):
            if count <= 1:
                continue
            values = [
                _safe_trace_preview(prop.value)
                for prop in node.properties
                if prop.property_name == property_name
            ]
            duplicate = {
                "node": node.temp_id,
                "property": property_name,
                "count": count,
                "values": values,
            }
            duplicates.append(duplicate)
            logger.warning(
                "[DUPLICATE_PROPERTY_WARNING] ingestion_id=%s batch=%s attempt=%s "
                "node_id=%s property=%s count=%s values=%s",
                ingestion_id,
                batch_index,
                attempt,
                node.temp_id,
                property_name,
                count,
                values,
            )
    return duplicates


@lru_cache(maxsize=1)
def _get_context_service() -> ExtractionContextService:
    return ExtractionContextService()


@lru_cache(maxsize=1)
def _get_validation_service() -> GraphPatchValidationService:
    return GraphPatchValidationService(
        semantic_grounding_judge=create_default_semantic_grounding_judge()
    )


@lru_cache(maxsize=1)
def _get_workspace_service() -> IngestionWorkspaceService:
    return IngestionWorkspaceService()


@lru_cache(maxsize=1)
def _get_batch_extractor() -> BatchExtractor:
    return GeminiBatchExtractor()


@lru_cache(maxsize=1)
def _get_relationship_reconciler() -> RelationshipReconciler:
    return RelationshipReconciler()


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


_TRANSIENT_TRANSPORT_ERRORS: tuple[type[Exception], ...] = (
    httpx.RemoteProtocolError,
    httpx.ConnectError,
    httpx.TimeoutException,
    httpx.ReadError,
    httpx.WriteError,
)

_JITTER_RNG = random.Random()


def _is_transient_transport_error(exc: Exception) -> bool:
    """Return True only for conservative transient transport failures.

    Deterministic/config-oriented transport errors such as
    httpx.LocalProtocolError or httpx.UnsupportedProtocol are intentionally
    excluded and fail fast.
    """

    return isinstance(exc, _TRANSIENT_TRANSPORT_ERRORS)


def _transport_retry_delay_seconds(
    attempt: int,
    *,
    rng: random.Random | None = None,
) -> float:
    """Exponential backoff with bounded jitter for transient transport retries."""

    base = INGESTION_TRANSPORT_RETRY_BASE_SECONDS
    cap = INGESTION_TRANSPORT_RETRY_MAX_SECONDS
    jitter_cap = INGESTION_TRANSPORT_RETRY_JITTER_SECONDS
    exponential = base * (2 ** max(0, attempt - 1))
    delay = min(cap, exponential)
    jitter_source = rng if rng is not None else _JITTER_RNG
    jitter = (
        jitter_source.uniform(0.0, min(jitter_cap, delay))
        if jitter_cap > 0
        else 0.0
    )
    return min(cap, delay + jitter)


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


def _delete_state(tool_context: IngestionRuntime, key: str) -> None:
    if key in tool_context.state:
        del tool_context.state[key]


def _clear_validation_gate(tool_context: IngestionRuntime) -> None:
    _delete_state(tool_context, VALIDATED_FINGERPRINT_STATE_KEY)


def _pace_next_model_turn(tool_context: IngestionRuntime) -> None:
    """Space expensive Gemini turns to stay below per-minute input quotas."""
    if (
        BATCH_PACE_SECONDS > 0
        and hasattr(tool_context, "load_artifact")
        and hasattr(tool_context, "save_artifact")
    ):
        time.sleep(BATCH_PACE_SECONDS)


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

    node_lines: list[str] = []
    edge_lines: list[str] = []
    for batch in workspace.batches:
        if batch.index >= before_batch_index or batch.fragment is None:
            continue
        fragment = batch.fragment
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


def _fragment_fingerprint(fragment: GraphPatchFragment) -> str:
    material = json.dumps(
        fragment.model_dump(by_alias=True, mode="json"),
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _edge_key(edge) -> tuple[str, str, str]:
    return (edge.edge_name, edge.source_temp_id, edge.target_temp_id)


def _evidence_cites_any(evidence_items, chunk_indexes: frozenset[int]) -> bool:
    return any(item.chunk_index in chunk_indexes for item in evidence_items)


def _node_cites_rejected_coverage(
    node,
    rejected_scope: TargetedRepairScope,
) -> bool:
    return _evidence_cites_any(
        node.evidence,
        rejected_scope.rejected_coverage_chunks,
    ) or any(
        _evidence_cites_any(prop.evidence, rejected_scope.rejected_coverage_chunks)
        for prop in node.properties
    )


def _issues_from_response(response: dict[str, Any]) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for item in response.get("errors", []) or []:
        try:
            issues.append(ValidationIssue.model_validate(item))
        except Exception:  # noqa: BLE001
            logger.debug("Skipping unparsable validation issue in repair scope: %r", item)
    return issues


def _issue_fact_trace(
    issue: ValidationIssue,
    fragment: GraphPatchFragment | None,
) -> dict[str, Any]:
    if fragment is None:
        return {}
    payload: dict[str, Any] = {}
    node_index_match = re.search(r"nodes\.(\d+)", issue.location)
    prop_index_match = re.search(r"nodes\.\d+\.properties\.(\d+)", issue.location)
    edge_index_match = re.search(r"edges\.(\d+)", issue.location)
    if node_index_match:
        node_index = int(node_index_match.group(1))
        if 0 <= node_index < len(fragment.nodes):
            node = fragment.nodes[node_index]
            payload["node_id"] = node.temp_id
            payload["class"] = node.class_name
            if prop_index_match:
                prop_index = int(prop_index_match.group(1))
                payload["property_index"] = prop_index
                if 0 <= prop_index < len(node.properties):
                    prop = node.properties[prop_index]
                    payload["property"] = prop.property_name
                    payload["value"] = _safe_trace_preview(prop.value)
                    payload["evidence_chunk_ids"] = [
                        item.chunk_index for item in prop.evidence
                    ]
                    payload["evidence_texts"] = [
                        _safe_trace_preview(item.text) for item in prop.evidence
                    ]
            else:
                payload["evidence_chunk_ids"] = [
                    item.chunk_index for item in node.evidence
                ]
                payload["evidence_texts"] = [
                    _safe_trace_preview(item.text) for item in node.evidence
                ]
    elif issue.node_temp_id is not None:
        node = next(
            (item for item in fragment.nodes if item.temp_id == issue.node_temp_id),
            None,
        )
        if node is not None:
            payload["node_id"] = node.temp_id
            payload["class"] = node.class_name
            prop = next(
                (
                    item
                    for item in node.properties
                    if item.property_name == issue.property_name
                ),
                None,
            )
            if prop is not None:
                payload["property"] = prop.property_name
                payload["value"] = _safe_trace_preview(prop.value)
                payload["evidence_chunk_ids"] = [
                    item.chunk_index for item in prop.evidence
                ]
                payload["evidence_texts"] = [
                    _safe_trace_preview(item.text) for item in prop.evidence
                ]
    if edge_index_match:
        edge_index = int(edge_index_match.group(1))
        if 0 <= edge_index < len(fragment.edges):
            edge = fragment.edges[edge_index]
            payload.update(
                {
                    "edge": edge.edge_name,
                    "source_node": edge.source_temp_id,
                    "target_node": edge.target_temp_id,
                    "evidence_chunk_ids": [
                        item.chunk_index for item in edge.evidence
                    ],
                    "evidence_texts": [
                        _safe_trace_preview(item.text) for item in edge.evidence
                    ],
                }
            )
    return payload


def _log_validation_rejections(
    issues: list[ValidationIssue],
    *,
    ingestion_id: str,
    batch_index: int | None = None,
    fragment: GraphPatchFragment | None = None,
) -> None:
    for issue in issues:
        trace = _issue_fact_trace(issue, fragment)
        logger.info(
            "[VALIDATION_REJECTION] ingestion_id=%s batch=%s code=%s location=%s "
            "node_id=%s property=%s property_index=%s edge=%s value=%r "
            "evidence_chunk_ids=%s evidence_texts=%s reason=%r",
            ingestion_id,
            batch_index,
            _issue_code(issue),
            issue.location,
            trace.get("node_id", issue.node_temp_id),
            trace.get("property", issue.property_name),
            trace.get("property_index"),
            trace.get("edge", issue.edge_name),
            trace.get("value"),
            trace.get("evidence_chunk_ids"),
            trace.get("evidence_texts"),
            _safe_trace_preview(issue.message),
        )


def _rejected_scope_from_issues(
    fragment: GraphPatchFragment,
    issues: list[ValidationIssue],
) -> TargetedRepairScope:
    rejected_nodes: set[str] = set()
    rejected_node_evidence: set[str] = set()
    rejected_properties: set[tuple[str, str]] = set()
    rejected_edges: set[tuple[str, str, str]] = set()
    rejected_coverage_chunks: set[int] = set()

    node_by_index = {index: node for index, node in enumerate(fragment.nodes)}
    edge_by_index = {index: edge for index, edge in enumerate(fragment.edges)}
    edges_by_name = {}
    for edge in fragment.edges:
        edges_by_name.setdefault(edge.edge_name, []).append(edge)

    whole_node_codes = {
        "DUPLICATE_TEMP_ID",
        "UNKNOWN_CLASS",
        "PROPERTY_DOMAIN_MISMATCH",
    }
    whole_edge_codes = {
        "DANGLING_REFERENCE",
        "UNKNOWN_EDGE",
        "EDGE_DOMAIN_MISMATCH",
        "EDGE_RANGE_MISMATCH",
        "EDGE_RELATION_NOT_GROUNDED",
    }

    for issue in issues:
        code = _issue_code(issue)
        coverage_match = re.fullmatch(r"coverage\.(\d+)(?:\..*)?", issue.location)
        if coverage_match:
            rejected_coverage_chunks.add(int(coverage_match.group(1)))
            continue

        prop_match = re.match(r"nodes\.(\d+)\.properties\.(\d+)", issue.location)
        if prop_match:
            node = node_by_index.get(int(prop_match.group(1)))
            if node is not None:
                prop_index = int(prop_match.group(2))
                if 0 <= prop_index < len(node.properties):
                    rejected_properties.add(
                        (node.temp_id, node.properties[prop_index].property_name)
                    )
                    continue

        if issue.node_temp_id and issue.property_name:
            rejected_properties.add((issue.node_temp_id, issue.property_name))
            continue

        edge_match = re.match(r"edges\.(\d+)", issue.location)
        if edge_match:
            edge = edge_by_index.get(int(edge_match.group(1)))
            if edge is not None:
                rejected_edges.add(_edge_key(edge))
                continue

        if issue.edge_name:
            for edge in edges_by_name.get(issue.edge_name, []):
                rejected_edges.add(_edge_key(edge))
            continue

        node_evidence_match = re.match(r"nodes\.(\d+)\.evidence(?:\.|$)", issue.location)
        if node_evidence_match:
            node = node_by_index.get(int(node_evidence_match.group(1)))
            if node is not None:
                rejected_node_evidence.add(node.temp_id)
                continue

        node_match = re.fullmatch(r"nodes\.(\d+)(?:\.(?:tempId|className))?", issue.location)
        if node_match and code in whole_node_codes:
            node = node_by_index.get(int(node_match.group(1)))
            if node is not None:
                rejected_nodes.add(node.temp_id)
            continue

        if code in whole_edge_codes and issue.edge_name:
            for edge in edges_by_name.get(issue.edge_name, []):
                rejected_edges.add(_edge_key(edge))

    return TargetedRepairScope(
        rejected_nodes=frozenset(rejected_nodes),
        rejected_node_evidence=frozenset(rejected_node_evidence),
        rejected_properties=frozenset(rejected_properties),
        rejected_edges=frozenset(rejected_edges),
        rejected_coverage_chunks=frozenset(rejected_coverage_chunks),
    )


def _accepted_repair_context(
    fragment: GraphPatchFragment,
    rejected_scope: TargetedRepairScope,
) -> dict[str, Any]:
    nodes = []
    for node in fragment.nodes:
        if node.temp_id in rejected_scope.rejected_nodes:
            continue
        accepted_properties = [
            prop
            for prop in node.properties
            if (node.temp_id, prop.property_name)
            not in rejected_scope.rejected_properties
        ]
        nodes.append(
            {
                **node.model_dump(by_alias=True, mode="json"),
                "properties": [
                    prop.model_dump(by_alias=True, mode="json")
                    for prop in accepted_properties
                ],
            }
        )
    return {
        "nodes": nodes,
        "edges": [
            edge.model_dump(by_alias=True, mode="json")
            for edge in fragment.edges
            if _edge_key(edge) not in rejected_scope.rejected_edges
        ],
        "coverage": [
            coverage.model_dump(by_alias=True, mode="json")
            for coverage in fragment.coverage
            if coverage.chunk_index not in rejected_scope.rejected_coverage_chunks
        ],
    }


def _rejected_candidate_context(
    fragment: GraphPatchFragment,
    rejected_scope: TargetedRepairScope,
) -> dict[str, Any]:
    nodes = []
    for node in fragment.nodes:
        rejected_properties = [
            prop
            for prop in node.properties
            if (node.temp_id, prop.property_name)
            in rejected_scope.rejected_properties
        ]
        if node.temp_id in rejected_scope.rejected_nodes:
            nodes.append(node.model_dump(by_alias=True, mode="json"))
        elif rejected_properties or node.temp_id in rejected_scope.rejected_node_evidence:
            nodes.append(
                {
                    "tempId": node.temp_id,
                    "className": node.class_name,
                    "properties": [
                        prop.model_dump(by_alias=True, mode="json")
                        for prop in rejected_properties
                    ],
                    "evidence": [
                        item.model_dump(by_alias=True, mode="json")
                        for item in node.evidence
                    ],
                    "confidence": node.confidence,
                }
            )
    return {
        "nodes": nodes,
        "edges": [
            edge.model_dump(by_alias=True, mode="json")
            for edge in fragment.edges
            if _edge_key(edge) in rejected_scope.rejected_edges
        ],
        "coverage": [
            coverage.model_dump(by_alias=True, mode="json")
            for coverage in fragment.coverage
            if coverage.chunk_index in rejected_scope.rejected_coverage_chunks
        ],
    }


def _merge_targeted_repair(
    base_fragment: GraphPatchFragment,
    repair_fragment: GraphPatchFragment,
    rejected_scope: TargetedRepairScope,
    *,
    trace_context: dict[str, Any] | None = None,
) -> GraphPatchFragment:
    trace_context = trace_context or {}
    batch_index = trace_context.get("batch")
    attempt = trace_context.get("attempt")
    ingestion_id = trace_context.get("ingestion_id")
    before_property_count = sum(len(node.properties) for node in base_fragment.nodes)
    action_counts: Counter[str] = Counter()
    merged = base_fragment.model_copy(deep=True)
    repair_nodes = {node.temp_id: node for node in repair_fragment.nodes}

    merged_nodes = []
    for node in merged.nodes:
        repair_node = repair_nodes.pop(node.temp_id, None)
        logger.debug(
            "[REPAIR_NODE_BEFORE] ingestion_id=%s batch=%s attempt=%s node_id=%s "
            "properties=%s",
            ingestion_id,
            batch_index,
            attempt,
            node.temp_id,
            _trace_node_properties(node),
        )
        if node.temp_id in rejected_scope.rejected_nodes:
            if repair_node is not None:
                action_counts["REPLACE"] += len(repair_node.properties)
                logger.debug(
                    "[REPAIR_MERGE] ingestion_id=%s batch=%s attempt=%s node_id=%s "
                    "property=%s action=REPLACE old_value=%s new_value=%s reason=whole_node",
                    ingestion_id,
                    batch_index,
                    attempt,
                    node.temp_id,
                    "*",
                    _trace_node_properties(node),
                    _trace_node_properties(repair_node),
                )
                merged_nodes.append(repair_node.model_copy(deep=True))
            else:
                action_counts["REMOVE"] += len(node.properties)
                logger.debug(
                    "[REPAIR_MERGE] ingestion_id=%s batch=%s attempt=%s node_id=%s "
                    "property=%s action=REMOVE old_value=%s new_value=%s reason=whole_node",
                    ingestion_id,
                    batch_index,
                    attempt,
                    node.temp_id,
                    "*",
                    _trace_node_properties(node),
                    None,
                )
            continue
        if repair_node is not None:
            if node.temp_id in rejected_scope.rejected_node_evidence:
                node.evidence = [
                    item.model_copy(deep=True) for item in repair_node.evidence
                ]
            repair_props = {
                prop.property_name: prop for prop in repair_node.properties
            }
            next_properties = []
            for prop in node.properties:
                is_rejected_property = (
                    node.temp_id,
                    prop.property_name,
                ) in rejected_scope.rejected_properties
                if is_rejected_property and prop.property_name in repair_props:
                    replacement = repair_props[prop.property_name].model_copy(deep=True)
                    next_properties.append(replacement)
                    action_counts["REPLACE"] += 1
                    logger.debug(
                        "[REPAIR_MERGE] ingestion_id=%s batch=%s attempt=%s "
                        "node_id=%s property=%s action=REPLACE old_value=%r "
                        "new_value=%r existing_same_property_count=%s",
                        ingestion_id,
                        batch_index,
                        attempt,
                        node.temp_id,
                        prop.property_name,
                        _safe_trace_preview(prop.value),
                        _safe_trace_preview(replacement.value),
                        sum(
                            1
                            for item in node.properties
                            if item.property_name == prop.property_name
                        ),
                    )
                    continue
                if is_rejected_property:
                    action_counts["REMOVE"] += 1
                    logger.debug(
                        "[REPAIR_MERGE] ingestion_id=%s batch=%s attempt=%s "
                        "node_id=%s property=%s action=REMOVE old_value=%r "
                        "new_value=%s",
                        ingestion_id,
                        batch_index,
                        attempt,
                        node.temp_id,
                        prop.property_name,
                        _safe_trace_preview(prop.value),
                        None,
                    )
                    continue
                next_properties.append(prop)
                action_counts["PRESERVE"] += 1
                logger.debug(
                    "[REPAIR_MERGE] ingestion_id=%s batch=%s attempt=%s "
                    "node_id=%s property=%s action=PRESERVE old_value=%r new_value=%s",
                    ingestion_id,
                    batch_index,
                    attempt,
                    node.temp_id,
                    prop.property_name,
                    _safe_trace_preview(prop.value),
                    None,
                )
            node.properties = next_properties
            existing_property_names = {prop.property_name for prop in node.properties}
            for prop in repair_node.properties:
                is_rejected_property = (
                    node.temp_id,
                    prop.property_name,
                ) in rejected_scope.rejected_properties
                is_new_coverage_fact = (
                    prop.property_name not in existing_property_names
                    and _evidence_cites_any(
                        prop.evidence,
                        rejected_scope.rejected_coverage_chunks,
                    )
                )
                if is_rejected_property or is_new_coverage_fact:
                    action = (
                        "APPEND"
                        if prop.property_name in existing_property_names
                        else "ADD"
                    )
                    action_counts[action] += 1
                    logger.debug(
                        "[REPAIR_MERGE] ingestion_id=%s batch=%s attempt=%s "
                        "node_id=%s property=%s action=%s old_value=%s new_value=%r "
                        "existing_same_property_count=%s cites_rejected_coverage=%s",
                        ingestion_id,
                        batch_index,
                        attempt,
                        node.temp_id,
                        prop.property_name,
                        action,
                        None,
                        _safe_trace_preview(prop.value),
                        sum(
                            1
                            for item in node.properties
                            if item.property_name == prop.property_name
                        ),
                        _evidence_cites_any(
                            prop.evidence,
                            rejected_scope.rejected_coverage_chunks,
                        ),
                    )
                    if action == "APPEND":
                        logger.warning(
                            "[REPAIR_MERGE] ingestion_id=%s batch=%s attempt=%s "
                            "node_id=%s property=%s action=APPEND "
                            "existing_same_property_count=%s",
                            ingestion_id,
                            batch_index,
                            attempt,
                            node.temp_id,
                            prop.property_name,
                            sum(
                                1
                                for item in node.properties
                                if item.property_name == prop.property_name
                            ),
                        )
                    node.properties.append(prop.model_copy(deep=True))
                    existing_property_names.add(prop.property_name)
                else:
                    action_counts["SKIP"] += 1
                    logger.debug(
                        "[REPAIR_MERGE] ingestion_id=%s batch=%s attempt=%s "
                        "node_id=%s property=%s action=SKIP new_value=%r "
                        "cites_rejected_coverage=%s",
                        ingestion_id,
                        batch_index,
                        attempt,
                        node.temp_id,
                        prop.property_name,
                        _safe_trace_preview(prop.value),
                        _evidence_cites_any(
                            prop.evidence,
                            rejected_scope.rejected_coverage_chunks,
                        ),
                    )
        logger.debug(
            "[REPAIR_NODE_AFTER] ingestion_id=%s batch=%s attempt=%s node_id=%s "
            "property_count_before=%s property_count_after=%s properties=%s",
            ingestion_id,
            batch_index,
            attempt,
            node.temp_id,
            len(
                next(
                    original.properties
                    for original in base_fragment.nodes
                    if original.temp_id == node.temp_id
                )
            ),
            len(node.properties),
            [prop.property_name for prop in node.properties],
        )
        merged_nodes.append(node)

    for repair_node in repair_nodes.values():
        if (
            repair_node.temp_id in rejected_scope.rejected_nodes
            or _node_cites_rejected_coverage(repair_node, rejected_scope)
        ):
            action_counts["ADD"] += len(repair_node.properties)
            logger.debug(
                "[REPAIR_MERGE] ingestion_id=%s batch=%s attempt=%s node_id=%s "
                "property=%s action=ADD old_value=%s new_value=%s reason=new_node",
                ingestion_id,
                batch_index,
                attempt,
                repair_node.temp_id,
                "*",
                None,
                _trace_node_properties(repair_node),
            )
            merged_nodes.append(repair_node.model_copy(deep=True))
    merged.nodes = merged_nodes

    repair_edges = {_edge_key(edge): edge for edge in repair_fragment.edges}
    merged.edges = [
        (
            repair_edges[_edge_key(edge)].model_copy(deep=True)
            if _edge_key(edge) in rejected_scope.rejected_edges
            and _edge_key(edge) in repair_edges
            else edge
        )
        for edge in merged.edges
        if _edge_key(edge) not in rejected_scope.rejected_edges
        or _edge_key(edge) in repair_edges
    ]
    existing_edge_keys = {_edge_key(edge) for edge in merged.edges}
    for key, edge in repair_edges.items():
        is_new_coverage_edge = (
            key not in existing_edge_keys
            and _evidence_cites_any(
                edge.evidence,
                rejected_scope.rejected_coverage_chunks,
            )
        )
        if (
            key in rejected_scope.rejected_edges or is_new_coverage_edge
        ) and key not in existing_edge_keys:
            logger.debug(
                "[REPAIR_MERGE] ingestion_id=%s batch=%s attempt=%s edge=%s "
                "action=ADD source_node=%s target_node=%s cites_rejected_coverage=%s",
                ingestion_id,
                batch_index,
                attempt,
                edge.edge_name,
                edge.source_temp_id,
                edge.target_temp_id,
                is_new_coverage_edge,
            )
            merged.edges.append(edge.model_copy(deep=True))

    repair_coverage = {
        coverage.chunk_index: coverage for coverage in repair_fragment.coverage
    }
    merged.coverage = [
        (
            repair_coverage[coverage.chunk_index].model_copy(deep=True)
            if coverage.chunk_index in rejected_scope.rejected_coverage_chunks
            and coverage.chunk_index in repair_coverage
            else coverage
        )
        for coverage in merged.coverage
    ]

    warnings = list(merged.warnings)
    for warning in repair_fragment.warnings:
        if warning not in warnings:
            warnings.append(warning)
    merged.warnings = warnings
    after_property_count = sum(len(node.properties) for node in merged.nodes)
    expected_after = (
        before_property_count
        - action_counts["REMOVE"]
        + action_counts["ADD"]
        + action_counts["APPEND"]
    )
    logger.debug(
        "[PROPERTY_COUNT_DIFF] ingestion_id=%s batch=%s attempt=%s before=%s "
        "removed=%s added=%s replaced=%s preserved=%s appended=%s skipped=%s "
        "after=%s",
        ingestion_id,
        batch_index,
        attempt,
        before_property_count,
        action_counts["REMOVE"],
        action_counts["ADD"],
        action_counts["REPLACE"],
        action_counts["PRESERVE"],
        action_counts["APPEND"],
        action_counts["SKIP"],
        after_property_count,
    )
    if after_property_count != expected_after:
        logger.warning(
            "[REPAIR_MERGE_INVARIANT_WARNING] ingestion_id=%s batch=%s attempt=%s "
            "before=%s removed=%s added=%s appended=%s expected_after=%s after=%s",
            ingestion_id,
            batch_index,
            attempt,
            before_property_count,
            action_counts["REMOVE"],
            action_counts["ADD"],
            action_counts["APPEND"],
            expected_after,
            after_property_count,
        )
    _log_duplicate_property_warnings(
        merged,
        ingestion_id=ingestion_id,
        batch_index=batch_index,
        attempt=attempt,
    )
    return merged


def _targeted_repair_error_payload(
    response: dict[str, Any],
    rejected_scope: TargetedRepairScope,
) -> dict[str, Any]:
    return {
        key: response.get(key)
        for key in (
            "stage",
            "batchIndex",
            "errorSummary",
            "repairInstructions",
            "affectedChunkIndexes",
            "conflict",
        )
        if key in response
    } | {
        "rejectedProperties": sorted(
            f"{node}.{prop}" for node, prop in rejected_scope.rejected_properties
        ),
        "rejectedEdges": sorted(
            f"{edge}: {source}->{target}"
            for edge, source, target in rejected_scope.rejected_edges
        ),
        "rejectedCoverage": sorted(rejected_scope.rejected_coverage_chunks),
        "rejectedNodes": sorted(rejected_scope.rejected_nodes),
    }


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


def _issue_chunk_indexes(
    issues: list[ValidationIssue],
    fragment: GraphPatchFragment | None,
) -> list[int]:
    indexes: set[int] = set()
    if fragment is None:
        return []
    node_by_temp_id = {node.temp_id: node for node in fragment.nodes}
    for issue in issues:
        if issue.node_temp_id is not None:
            node = node_by_temp_id.get(issue.node_temp_id)
            if node is not None:
                indexes.update(item.chunk_index for item in node.evidence)
                for prop in node.properties:
                    indexes.update(item.chunk_index for item in prop.evidence)
        if issue.edge_name is not None:
            indexes.update(
                evidence.chunk_index
                for edge in fragment.edges
                if edge.edge_name == issue.edge_name
                for evidence in edge.evidence
            )
    return sorted(indexes)


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
        instructions.append(
            "Resolve the cross-batch conflict using the returned conflict object; do not resubmit the same scalar property/value conflict."
        )
    if "PROPERTY_VALUE_NOT_GROUNDED" in codes:
        instructions.append(
            "Ground property values by ontology policy: literal properties need "
            "the exact source value or must be omitted; normalized properties "
            "must be fully supported by the cited evidence - narrow the value to "
            "what the evidence supports, add verbatim evidence covering every "
            "claim, or mark the chunk AMBIGUOUS and omit the unsupported value; "
            "runtime-managed/edge-derived properties must be omitted and are "
            "supplied by the compiler. Do not resubmit the same unsupported value."
        )
    if summary["evidenceTextNotInSourceLocations"]:
        instructions.append(
            "Use only exact verbatim evidence from the cited chunk. For Markdown tables, "
            "copy the complete source row including pipe delimiters; never synthesize or "
            "join multiple rows into one evidence string."
        )
    if "DERIVED_PROPERTY_REQUIRES_EDGE_EVIDENCE" in codes:
        instructions.append(
            "Add the grounded relationship edge that supports the derived ruleType, or omit the unsupported rule node."
        )
    if summary["coverageNotEvidencedChunkIndexes"]:
        instructions.append(
            "Do not resubmit unchanged coverage. For each coverageNotEvidenced chunk, add at least one grounded property/edge fact when the source has an ontology-representable fact; otherwise mark it NO_RELEVANT_FACT with a source-based reason."
        )
    if summary["schemaErrorLocations"]:
        instructions.append(
            "Fix every schema error before resubmitting the same batch; evidence objects must include source, chunkIndex, section when present, and text."
        )
    return (
        " ".join(instructions)
        or "Correct the batch validation errors and resubmit this same batch."
    )


def _conflict_repair_instruction(conflict: dict[str, Any]) -> str:
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
    summary = _batch_error_summary(
        issues, schema_error_locations=schema_error_locations
    )
    retry = _batch_retry_payload(workspace, batch_index)
    preview = issues[:10]
    affected_chunk_indexes = sorted(
        set(summary["coverageNotEvidencedChunkIndexes"])
        | set(_issue_chunk_indexes(issues, fragment))
    )
    response = {
        "success": False,
        "stage": "batch_validation",
        "batchIndex": batch_index,
        "terminal": not retry_required,
        "retryRequired": retry_required,
        "nextAction": next_action,
        "errorCount": len(issues),
        "errorsTruncated": len(issues) > len(preview),
        "errors": [
            issue.model_dump(by_alias=True, exclude_none=True) for issue in preview
        ],
        "errorSummary": summary,
        "conflict": conflict,
        "fragmentStats": _fragment_stats(fragment),
        "repairInstructions": (
            f"{_repair_instructions(summary)} {conflict['repairInstruction']}"
            if conflict and conflict.get("repairInstruction")
            else _repair_instructions(summary)
        ),
        "affectedChunkIndexes": affected_chunk_indexes,
        "affectedChunks": _affected_chunks_payload(
            workspace,
            affected_chunk_indexes,
        ),
    }
    if retry_required:
        response["nextBatch"] = retry["nextBatch"]
    if conflict is None:
        response.pop("conflict")
    return response


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
    validation_service: GraphPatchValidationService,
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
        service = create_fill_service(validation_service=validation_service)
        fill_kwargs = ({"allow_partial_persistence": True} if allow_partial_persistence else {})
        result = service.fill(graph_patch, artifact_digest, source_chunks, **fill_kwargs)
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
    tool_context: IngestionRuntime,
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
    tool_context: IngestionRuntime,
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
    chunk_by_index = {chunk.index: chunk for chunk in workspace.chunks}
    for batch in workspace.batches:
        logger.info(
            "[INGESTION_BATCH_CREATED] ingestion_id=%s batch=%s chunk_ids=%s input_chars=%s",
            workspace.ingestion_id,
            batch.index,
            batch.chunk_indexes,
            batch.content_chars,
        )
        for chunk_index in batch.chunk_indexes:
            chunk = chunk_by_index[chunk_index]
            logger.debug(
                "[INGESTION_BATCH_CREATED] ingestion_id=%s batch=%s chunk=%s "
                "section=%r line_start=%s line_end=%s char_count=%s",
                workspace.ingestion_id,
                batch.index,
                chunk.index,
                chunk.section,
                chunk.start_line,
                chunk.end_line,
                len(chunk.content),
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
    tool_context: IngestionRuntime,
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
        batch = workspace.batches[batch_index]
        expected_indexes = set(batch.chunk_indexes)
        batch_chunks = [
            chunk for chunk in workspace.chunks if chunk.index in expected_indexes
        ]
        validation_service = _get_validation_service()
        context_fragments = fragments_with_replacement(
            workspace,
            batch_index,
            fragment,
        )
        context_nodes = [
            node
            for context_fragment in context_fragments
            for node in context_fragment.nodes
        ]
        grounding_issues = validation_service.source_grounding.validate(
            fragment,
            batch_chunks,
            context_nodes=context_nodes,
        )
        if not grounding_issues:
            grounding_issues = business_rule_edge_issues(
                fragment,
                validation_service,
                existing_fragments=context_fragments,
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
            _log_validation_rejections(
                grounding_issues,
                ingestion_id=ingestion_id,
                batch_index=batch_index,
                fragment=fragment,
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
        candidate = _get_workspace_service().submit(
            workspace,
            batch_index,
            fragment,
        )
        candidate_patch = _get_workspace_service().merged_patch(candidate)
        candidate_covered_indexes = {
            coverage.chunk_index
            for candidate_batch in candidate.batches
            if candidate_batch.fragment is not None
            for coverage in candidate_batch.fragment.coverage
        }
        candidate_chunks = [
            chunk
            for chunk in candidate.chunks
            if chunk.index in candidate_covered_indexes
        ]
        candidate_assessment = validation_service.assess(
            candidate_patch,
            candidate.artifact_digest,
            candidate_chunks,
        )
        if candidate_assessment.result.errors:
            summary = _batch_error_summary(candidate_assessment.result.errors)
            unchanged_issue = _unchanged_retry_issue(
                workspace,
                batch_index,
                summary,
                fragment,
            )
            batch_issues = candidate_assessment.result.errors
            if unchanged_issue is not None:
                batch_issues = [unchanged_issue, *batch_issues]
            _remember_retry_state(workspace, batch_index, fragment, summary)
            _store_workspace(tool_context, workspace)
            response = _batch_validation_response(
                workspace,
                batch_index,
                batch_issues,
                fragment=fragment,
                retry_required=unchanged_issue is None,
                next_action=(
                    "explicit_extraction_failure"
                    if unchanged_issue is not None
                    else "correct_and_resubmit_same_batch"
                ),
            )
            if unchanged_issue is not None:
                response["stage"] = "explicit_extraction_failure"
                response["terminal"] = True
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
                    conflict.get("nodeTempId") if conflict is not None else None
                ),
                property_name=(
                    conflict.get("propertyName") if conflict is not None else None
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
            summary = _batch_error_summary(
                issues, schema_error_locations=schema_locations
            )
            if retry_fragment is not None:
                unchanged_issue = _unchanged_retry_issue(
                    workspace, batch_index, summary, retry_fragment
                )
                if unchanged_issue is not None:
                    response = _batch_validation_response(
                        workspace,
                        batch_index,
                        [unchanged_issue, *issues],
                        schema_error_locations=schema_locations,
                        conflict=conflict,
                        fragment=retry_fragment,
                        retry_required=False,
                        next_action="explicit_extraction_failure",
                    )
                    _store_workspace(tool_context, workspace)
                    _pace_next_model_turn(tool_context)
                    return response
                _remember_retry_state(workspace, batch_index, retry_fragment, summary)
                _store_workspace(tool_context, workspace)
            response = _batch_validation_response(
                workspace,
                batch_index,
                issues,
                schema_error_locations=schema_locations,
                conflict=conflict,
                fragment=retry_fragment,
            )
            _pace_next_model_turn(tool_context)
            return response
        return {
            "success": False,
            "stage": "batch_validation",
            "batchIndex": batch_index,
            "errors": [
                issue.model_dump(by_alias=True, exclude_none=True) for issue in issues
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
    tool_context: IngestionRuntime,
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
        assessment.result.node_count
        if not assessment.result.valid_for_extraction
        else 0,
        assessment.result.edge_count if assessment.result.valid_for_extraction else 0,
        assessment.result.edge_count
        if not assessment.result.valid_for_extraction
        else 0,
        [item.code.value for item in assessment.result.errors],
        [item.code.value for item in assessment.result.readiness_issues],
    )
    _log_validation_rejections(
        assessment.result.errors,
        ingestion_id=ingestion_id,
        fragment=GraphPatchFragment.model_validate(
            patch.model_dump(by_alias=True, mode="json")
        ),
    )
    if assessment.result.valid_for_extraction and not assessment.result.valid_for_persistence:
        rule_type_issues = [
            issue
            for issue in assessment.result.readiness_issues
            if issue.property_name == RULE_TYPE_PROPERTY
        ]
        if rule_type_issues:
            node_batch: dict[str, int] = {}
            fragment_by_batch: dict[int, GraphPatchFragment] = {}
            for batch in workspace.batches:
                if batch.fragment is None:
                    continue
                fragment_by_batch[batch.index] = batch.fragment
                for node in batch.fragment.nodes:
                    node_batch.setdefault(node.temp_id, batch.index)
            mapped = [
                issue
                for issue in rule_type_issues
                if issue.node_temp_id in node_batch
            ]
            if mapped:
                batch_index = node_batch[mapped[0].node_temp_id]
                batch_issues = [
                    ValidationIssue(
                        code="DERIVED_PROPERTY_REQUIRES_EDGE_EVIDENCE",
                        message=(
                            f"{issue.node_temp_id} requires an incoming "
                            f"relationship edge that derives {RULE_TYPE_PROPERTY}"
                        ),
                        location=issue.location,
                        node_temp_id=issue.node_temp_id,
                        property_name=RULE_TYPE_PROPERTY,
                    )
                    for issue in mapped
                    if node_batch.get(issue.node_temp_id) == batch_index
                ]
                response = _batch_validation_response(
                    workspace,
                    batch_index,
                    batch_issues,
                    fragment=fragment_by_batch.get(batch_index),
                )
                response["stage"] = "batch_validation"
                response["ingestionId"] = ingestion_id
                response["finalizeBlocked"] = True
                _pace_next_model_turn(tool_context)
                return response
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
    tool_context: IngestionRuntime,
    allow_partial_persistence: bool = False,
) -> dict[str, Any]:
    """Persist a finalized graph, with explicit partial persistence when allowed."""

    workspace, error = _workspace_precondition(ingestion_id, tool_context)
    if error is not None or workspace is None:
        return error
    if workspace.finalized_patch is None:
        return {"success": False, "stage": "validation_precondition", "terminal": True, "errors": [ValidationIssue(code="VALIDATION_PRECONDITION", message="finalize_ingestion must run before fill_ingestion", location="ingestionId").model_dump(by_alias=True, exclude_none=True)]}

    validation_service = _get_validation_service()
    assessment = validation_service.assess(workspace.finalized_patch, workspace.artifact_digest, workspace.chunks)
    if not assessment.result.valid_for_extraction or assessment.compiled_patch is None:
        return {"success": False, "stage": "validation", "terminal": True, "validation": _public_assessment(assessment)}
    if not assessment.result.valid_for_persistence and not allow_partial_persistence:
        return {"success": False, "stage": "validation_precondition", "terminal": True, "validation": _public_assessment(assessment), "errors": [ValidationIssue(code="VALIDATION_PRECONDITION", message="Persistence readiness failed; set allow_partial_persistence=true only for an explicit partial persistence commit requested by the user", location="ingestionId").model_dump(by_alias=True, exclude_none=True)]}

    expected_fingerprint = workspace.validated_fingerprint
    if allow_partial_persistence and not assessment.result.valid_for_persistence:
        expected_fingerprint = assessment.fingerprint
    candidate = validation_service.fingerprint_candidate(workspace.finalized_patch, workspace.artifact_digest)
    if expected_fingerprint is None or candidate != expected_fingerprint:
        workspace.validated_fingerprint = None
        _store_workspace(tool_context, workspace)
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


def _reconcile_relationship_gaps(
    ingestion_id: str,
    tool_context: IngestionRuntime,
) -> dict[str, Any]:
    """Run LLM-reasoned relationship reconciliation on edge-rule readiness gaps."""

    workspace, error = _workspace_precondition(ingestion_id, tool_context)
    if error is not None or workspace is None:
        return {"success": False, "terminal": True, **error}
    if workspace.finalized_patch is None:
        return {
            "success": False,
            "terminal": True,
            "reconciliationExhausted": True,
            "reconciliationPasses": 0,
            "reconciliationIssues": [
                ValidationIssue(
                    code="VALIDATION_PRECONDITION",
                    message=(
                        "finalize_ingestion must run before relationship "
                        "reconciliation"
                    ),
                    location="ingestionId",
                ).model_dump(by_alias=True, exclude_none=True)
            ],
        }
    validation_service = _get_validation_service()
    merged_draft = GraphPatchDraft.model_validate(workspace.finalized_patch)
    assessment = validation_service.assess(
        merged_draft,
        workspace.artifact_digest,
        workspace.chunks,
    )
    if not (
        assessment.result.valid_for_extraction
        and reconciliation_triggered(assessment.result.readiness_issues)
    ):
        return {
            "success": False,
            "terminal": True,
            "reconciliationExhausted": False,
            "reconciliationPasses": 0,
            "reconciliationNotNeeded": True,
            "reconciliationIssues": [
                issue.model_dump(by_alias=True, exclude_none=True)
                for issue in assessment.result.readiness_issues
            ],
        }
    outcome = _get_relationship_reconciler().reconcile(
        merged_draft=merged_draft,
        compiled_patch=assessment.compiled_patch,
        readiness_issues=assessment.result.readiness_issues,
        chunks=workspace.chunks,
        validation_service=validation_service,
        artifact_digest=workspace.artifact_digest,
        registry=validation_service.validator.registry,
    )
    if (
        outcome.reconciled
        and outcome.draft is not None
        and outcome.fingerprint is not None
        and outcome.assessment is not None
    ):
        workspace.finalized_patch = outcome.draft.model_dump(
            by_alias=True,
            mode="json",
        )
        workspace.validated_fingerprint = outcome.fingerprint
        _store_workspace(tool_context, workspace)
        return {
            "success": True,
            "stage": "ready_to_fill",
            "terminal": False,
            "ingestionId": ingestion_id,
            "reconciled": True,
            "reconciliationPasses": outcome.passes_used,
            **_public_assessment(outcome.assessment),
            "artifactDigest": workspace.artifact_digest,
            "ontologyDigest": workspace.ontology_digest,
            "skillDigest": workspace.skill_digest,
            "workspaceStats": _workspace_stats(workspace),
        }
    logger.warning(
        "INGESTION_RECONCILIATION_FAILED ingestion_id=%s exhausted=%s passes=%s",
        ingestion_id,
        outcome.exhausted,
        outcome.passes_used,
    )
    return {
        "success": False,
        "terminal": True,
        "reconciliationExhausted": outcome.exhausted,
        "reconciliationPasses": outcome.passes_used,
        "reconciliationIssues": [
            issue.model_dump(by_alias=True, exclude_none=True)
            for issue in outcome.issues
        ],
    }


async def ingest_document_end_to_end(
    artifact_name: str,
    tool_context: IngestionRuntime,
    persist: bool = True,
    allow_partial_persistence: bool = False,
    max_retries_per_batch: int = DEFAULT_MAX_RETRIES_PER_BATCH,
    max_transport_retries: int = DEFAULT_MAX_TRANSPORT_RETRIES,
    max_rate_limit_retries: int = DEFAULT_MAX_RATE_LIMIT_RETRIES,
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
        current_workspace = _load_workspace(tool_context)
        graph_context = (
            _canonical_graph_context(current_workspace, batch_index)
            if current_workspace is not None
            else ""
        )
        previous_error: dict[str, Any] | None = None
        candidate_fragment: GraphPatchFragment | None = None
        rejected_scope: TargetedRepairScope | None = None
        semantic_attempts_used = 0
        transport_retries_used = 0
        rate_limit_retries_used = 0
        while semantic_attempts_used < max_retries_per_batch:
            semantic_attempts_used += 1
            try:
                batch_chunks = [
                    DocumentChunk.model_validate(item)
                    for item in batch_payload.get("chunks", [])
                ]
                if candidate_fragment is not None and rejected_scope is not None:
                    affected_indexes = (
                        previous_error or {}
                    ).get("affectedChunkIndexes") or sorted(
                        rejected_scope.rejected_coverage_chunks
                    )
                    affected_chunks = [
                        chunk
                        for chunk in batch_payload.get("chunks", [])
                        if chunk.get("index") in set(affected_indexes)
                    ]
                    rejected_context = _rejected_candidate_context(
                        candidate_fragment,
                        rejected_scope,
                    )
                    accepted_context = _accepted_repair_context(
                        candidate_fragment,
                        rejected_scope,
                    )
                    logger.info(
                        "[SEMANTIC_REPAIR_START] ingestion_id=%s batch=%s attempt=%s "
                        "affected_chunks=%s rejected_properties=%s rejected_edges=%s "
                        "rejected_coverage=%s preserved_property_count=%s "
                        "preserved_edge_count=%s",
                        ingestion_id,
                        batch_index,
                        semantic_attempts_used - 1,
                        [chunk.get("index") for chunk in affected_chunks],
                        sorted(rejected_scope.rejected_properties),
                        sorted(rejected_scope.rejected_edges),
                        sorted(rejected_scope.rejected_coverage_chunks),
                        sum(
                            1
                            for node in candidate_fragment.nodes
                            for prop in node.properties
                            if (node.temp_id, prop.property_name)
                            not in rejected_scope.rejected_properties
                            and node.temp_id not in rejected_scope.rejected_nodes
                        ),
                        sum(
                            1
                            for edge in candidate_fragment.edges
                            if _edge_key(edge) not in rejected_scope.rejected_edges
                        ),
                    )
                    for node in candidate_fragment.nodes:
                        logger.debug(
                            "[REPAIR_NODE_BEFORE] ingestion_id=%s batch=%s attempt=%s "
                            "node_id=%s properties=%s",
                            ingestion_id,
                            batch_index,
                            semantic_attempts_used - 1,
                            node.temp_id,
                            _trace_node_properties(node),
                        )
                    repair = extractor.repair_fragment(
                        validation_errors=previous_error or {},
                        affected_chunks=affected_chunks,
                        ontology_catalog=ontology_catalog,
                        graph_context=graph_context,
                        rejected_candidate_facts=rejected_context,
                        accepted_candidate_facts=accepted_context,
                    )
                    _trace_fragment_details(
                        repair,
                        tag="SEMANTIC_REPAIR_RAW_RESULT",
                        ingestion_id=ingestion_id,
                        batch_index=batch_index,
                        attempt=semantic_attempts_used - 1,
                    )
                    repair = repair_fragment_grounding(
                        repair,
                        batch_chunks,
                        _get_validation_service().source_grounding,
                    )
                    _trace_fragment_details(
                        repair,
                        tag="SEMANTIC_REPAIR_GROUNDED_RESULT",
                        ingestion_id=ingestion_id,
                        batch_index=batch_index,
                        attempt=semantic_attempts_used - 1,
                    )
                    fragment = _merge_targeted_repair(
                        candidate_fragment,
                        repair,
                        rejected_scope,
                        trace_context={
                            "ingestion_id": ingestion_id,
                            "batch": batch_index,
                            "attempt": semantic_attempts_used - 1,
                        },
                    )
                else:
                    logger.info(
                        "[EXTRACTION_REQUEST] ingestion_id=%s batch=%s chunk_ids=%s "
                        "existing_context_nodes=%s accumulated_entities=%s "
                        "ontology_chars=%s",
                        ingestion_id,
                        batch_index,
                        batch_payload.get("chunkIndexes"),
                        graph_context.count("- ref=") if graph_context else 0,
                        graph_context.count("- ref=") if graph_context else 0,
                        len(ontology_catalog),
                    )
                    fragment = extractor.extract_fragment(
                        batch_payload=batch_payload,
                        ontology_catalog=ontology_catalog,
                        previous_error=previous_error,
                        graph_context=graph_context,
                    )
                    _trace_fragment_details(
                        fragment,
                        tag="RAW_EXTRACTION_RESULT",
                        ingestion_id=ingestion_id,
                        batch_index=batch_index,
                        attempt=0,
                    )
                fragment = repair_fragment_grounding(
                    fragment,
                    batch_chunks,
                    _get_validation_service().source_grounding,
                )
                _trace_fragment_details(
                    fragment,
                    tag="EXTRACTION_GROUNDED_RESULT",
                    ingestion_id=ingestion_id,
                    batch_index=batch_index,
                    attempt=semantic_attempts_used - 1,
                )
                _log_duplicate_property_warnings(
                    fragment,
                    ingestion_id=ingestion_id,
                    batch_index=batch_index,
                    attempt=semantic_attempts_used - 1,
                )
            except Exception as exc:
                logger.exception(
                    "ORCHESTRATION_FAILED ingestion_id=%s batch=%s attempt=%s",
                    ingestion_id,
                    batch_index,
                    semantic_attempts_used,
                )
                if (
                    _is_transient_transport_error(exc)
                    and transport_retries_used < max_transport_retries
                ):
                    transport_retries_used += 1
                    delay = _transport_retry_delay_seconds(transport_retries_used)
                    logger.warning(
                        "INGESTION_TRANSPORT_RETRY ingestion_id=%s batch=%s transport_retry=%s delay_seconds=%.2f error=%s",
                        ingestion_id,
                        batch_index,
                        transport_retries_used,
                        delay,
                        type(exc).__name__,
                    )
                    await asyncio.sleep(delay)
                    semantic_attempts_used -= 1
                    continue
                if (
                    _is_rate_limit_error(exc)
                    and rate_limit_retries_used < max_rate_limit_retries
                ):
                    rate_limit_retries_used += 1
                    delay = _rate_limit_retry_delay_seconds(
                        exc, rate_limit_retries_used
                    )
                    logger.warning(
                        "INGESTION_RATE_LIMIT_RETRY ingestion_id=%s batch=%s rate_limit_retry=%s delay_seconds=%.2f",
                        ingestion_id,
                        batch_index,
                        rate_limit_retries_used,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    semantic_attempts_used -= 1
                    continue
                if (
                    _is_retryable_extraction_error(exc)
                    and semantic_attempts_used < max_retries_per_batch
                ):
                    previous_error = _extractor_retry_error(exc)
                    continue
                failure: dict[str, Any] = {
                    "success": False,
                    "stage": "explicit_extraction_failure",
                    "terminal": True,
                    "ingestionId": ingestion_id,
                    "batchIndex": batch_index,
                    "attempt": semantic_attempts_used,
                    "errorKind": _orchestration_error_kind(exc),
                    "errors": [
                        ValidationIssue(
                            code="ORCHESTRATION_FAILED",
                            message=_orchestration_error_message(exc),
                            location=f"batches.{batch_index}.extract",
                        ).model_dump(by_alias=True, exclude_none=True)
                    ],
                }
                if _is_transient_transport_error(exc):
                    failure["failureReason"] = "TRANSPORT_RETRIES_EXHAUSTED"
                    failure["transportRetries"] = transport_retries_used
                elif _is_rate_limit_error(exc):
                    failure["failureReason"] = "RATE_LIMIT_RETRIES_EXHAUSTED"
                    failure["rateLimitRetries"] = rate_limit_retries_used
                elif _is_extractor_configuration_error(exc):
                    failure["failureReason"] = "EXTRACTOR_CONFIGURATION_FAILED"
                elif _is_retryable_extraction_error(exc):
                    failure["failureReason"] = "SEMANTIC_REPAIR_EXHAUSTED"
                failure["workspaceStats"] = (
                    _workspace_stats(workspace)
                    if (workspace := _load_workspace(tool_context)) is not None
                    else {}
                )
                return failure
            response = submit_ingestion_batch(
                ingestion_id,
                batch_index,
                fragment,
                tool_context,
            )
            if response.get("success"):
                processed_batches = int(response.get("processedBatches", 0))
                break
            candidate_fragment = fragment
            issues = _issues_from_response(response)
            rejected_scope = _rejected_scope_from_issues(candidate_fragment, issues)
            previous_error = _targeted_repair_error_payload(response, rejected_scope)
            logger.info(
                "SEMANTIC_REPAIR_RESULT ingestion_id=%s batch=%s attempt=%s repairedProperties=%s repairedEdges=%s repairedCoverage=%s remainingErrors=%s",
                ingestion_id,
                batch_index,
                semantic_attempts_used - 1,
                sorted(rejected_scope.rejected_properties),
                sorted(rejected_scope.rejected_edges),
                sorted(rejected_scope.rejected_coverage_chunks),
                [error.get("code") for error in response.get("errors", [])],
            )
            if not response.get("retryRequired"):
                logger.warning(
                    "[SEMANTIC_REPAIR_EXHAUSTED] ingestion_id=%s batch=%s attempts=%s "
                    "remaining_errors=%s remaining_properties=%s duplicate_properties=%s",
                    ingestion_id,
                    batch_index,
                    semantic_attempts_used,
                    response.get("errors", []),
                    [
                        {
                            "node": node.temp_id,
                            "property": prop.property_name,
                            "value": _safe_trace_preview(prop.value),
                            "evidence_chunk_ids": [
                                item.chunk_index for item in prop.evidence
                            ],
                        }
                        for node in candidate_fragment.nodes
                        for prop in node.properties
                    ],
                    _log_duplicate_property_warnings(
                        candidate_fragment,
                        ingestion_id=ingestion_id,
                        batch_index=batch_index,
                        attempt=semantic_attempts_used - 1,
                    ),
                )
                return {
                    **response,
                    "stage": "explicit_extraction_failure",
                    "terminal": True,
                    "ingestionId": ingestion_id,
                    "processedBatches": processed_batches,
                    "failureReason": "SEMANTIC_REPAIR_EXHAUSTED",
                }
        else:
            logger.warning(
                "[SEMANTIC_REPAIR_EXHAUSTED] ingestion_id=%s batch=%s attempts=%s "
                "remaining_errors=%s remaining_properties=%s duplicate_properties=%s",
                ingestion_id,
                batch_index,
                max_retries_per_batch,
                response.get("errors", []),
                [
                    {
                        "node": node.temp_id,
                        "property": prop.property_name,
                        "value": _safe_trace_preview(prop.value),
                        "evidence_chunk_ids": [
                            item.chunk_index for item in prop.evidence
                        ],
                    }
                    for node in (candidate_fragment.nodes if candidate_fragment else [])
                    for prop in node.properties
                ],
                _log_duplicate_property_warnings(
                    candidate_fragment,
                    ingestion_id=ingestion_id,
                    batch_index=batch_index,
                    attempt=max_retries_per_batch,
                )
                if candidate_fragment is not None
                else [],
            )
            return {
                **response,
                "success": False,
                "stage": "explicit_extraction_failure",
                "terminal": True,
                "ingestionId": ingestion_id,
                "batchIndex": batch_index,
                "processedBatches": processed_batches,
                "failureReason": "SEMANTIC_REPAIR_EXHAUSTED",
                "errors": response.get("errors", []),
                "message": (
                    f"Batch {batch_index} could not be safely reduced after "
                    f"{max_retries_per_batch} attempts"
                ),
            }

    workspace = _load_workspace(tool_context)
    skipped_chunks = workspace.skipped_chunk_indexes if workspace else []
    warnings = workspace.ingestion_warnings if workspace else []

    finalized = finalize_ingestion(ingestion_id, tool_context)
    partial_override = bool(
        allow_partial_persistence
        and persist
        and finalized.get("stage") == "readiness_gate"
        and finalized.get("validForExtraction") is True
    )
    if finalized.get("stage") != "ready_to_fill" and not partial_override:
        if (
            finalized.get("stage") == "readiness_gate"
            and finalized.get("validForExtraction") is True
        ):
            reconciled = _reconcile_relationship_gaps(ingestion_id, tool_context)
            if reconciled.get("success"):
                finalized = reconciled
            else:
                logger.warning(
                    "INGESTION_RECONCILIATION_FAILED ingestion_id=%s "
                    "exhausted=%s passes=%s",
                    ingestion_id,
                    reconciled.get("reconciliationExhausted"),
                    reconciled.get("reconciliationPasses"),
                )
                return {
                    **finalized,
                    "terminal": True,
                    "reconciliationExhausted": reconciled.get(
                        "reconciliationExhausted",
                        False,
                    ),
                    "reconciliationPasses": reconciled.get(
                        "reconciliationPasses",
                        0,
                    ),
                    "reconciliationIssues": reconciled.get(
                        "reconciliationIssues",
                        [],
                    ),
                    "partial": bool(skipped_chunks),
                    "skippedChunks": skipped_chunks,
                    "ingestionWarnings": warnings,
                }
        else:
            root_validation_codes = Counter(
                error.get("code") for error in finalized.get("errors", [])
            )
            duplicate_properties = (
                _log_duplicate_property_warnings(
                    GraphPatchFragment.model_validate(
                        _get_workspace_service()
                        .merged_patch(workspace)
                        .model_dump(by_alias=True, mode="json")
                    ),
                    ingestion_id=ingestion_id,
                )
                if workspace is not None
                else []
            )
            logger.error(
                "[INGESTION_FAILED] ingestion_id=%s document=%s failed_batch=%s "
                "failed_chunks=%s failure_stage=%s failure_code=%s "
                "processed_batches=%s skipped_chunks=%s root_validation_codes=%s "
                "duplicate_properties_detected=%s errors=%s readiness=%s",
                ingestion_id,
                workspace.artifact_name if workspace is not None else None,
                finalized.get("batchIndex"),
                finalized.get("affectedChunkIndexes"),
                finalized.get("stage"),
                finalized.get("failureReason")
                or (finalized.get("errors") or [{}])[0].get("code"),
                processed_batches,
                skipped_chunks,
                dict(root_validation_codes),
                duplicate_properties,
                finalized.get("errors", []),
                finalized.get("readinessIssues", []),
            )
            return {
                **finalized,
                "terminal": True,
                "partial": bool(skipped_chunks),
                "skippedChunks": skipped_chunks,
                "ingestionWarnings": warnings,
            }
    if partial_override:
        logger.warning("INGESTION_PARTIAL_PERSISTENCE_OVERRIDE ingestion_id=%s readiness=%s", ingestion_id, finalized.get("readinessIssues", []))

    if not persist:
        result = {
            **finalized,
            "stage": "ready_to_fill",
            "terminal": True,
            "persisted": False,
            "partial": bool(skipped_chunks),
            "skippedChunks": skipped_chunks,
            "ingestionWarnings": warnings,
        }
        logger.info(
            "INGESTION_COMPLETED ingestion_id=%s persisted=false "
            "processed_batches=%s total_batches=%s partial=%s skipped_chunks=%s",
            ingestion_id,
            processed_batches,
            len(workspace.batches) if workspace else 0,
            bool(skipped_chunks),
            skipped_chunks,
        )
        return result

    filled = await fill_ingestion(ingestion_id, tool_context, allow_partial_persistence=partial_override)
    workspace = _load_workspace(tool_context)
    skipped_chunks = workspace.skipped_chunk_indexes if workspace else skipped_chunks
    warnings = workspace.ingestion_warnings if workspace else warnings

    result = {
        **filled,
        "terminal": True,
        "ingestionId": ingestion_id,
        "partial": bool(skipped_chunks),
        "skippedChunks": skipped_chunks,
        "ingestionWarnings": warnings,
        "workspaceStats": (
            _workspace_stats(workspace)
            if workspace is not None
            else finalized.get("workspaceStats", {})
        ),
    }
    logger.info(
        "INGESTION_COMPLETED ingestion_id=%s persisted=%s "
        "processed_batches=%s total_batches=%s partial=%s skipped_chunks=%s "
        "nodes=%s edges=%s commit_status=%s verification=%s warnings=%s",
        ingestion_id,
        filled.get("commitStatus") == "committed",
        processed_batches,
        len(workspace.batches) if workspace else 0,
        bool(skipped_chunks),
        skipped_chunks,
        filled.get("nodes", 0),
        filled.get("edges", 0),
        filled.get("commitStatus"),
        filled.get("verificationStatus"),
        warnings,
    )
    return result


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


class IngestionUseCase:
    """Application workflow for document ingestion, independent of ADK tools."""

    async def prepare(
        self,
        artifact_name: str,
        runtime: IngestionRuntime,
    ) -> dict[str, Any]:
        return await prepare_extraction_context(artifact_name, runtime)

    async def begin(
        self,
        artifact_name: str,
        runtime: IngestionRuntime,
    ) -> dict[str, Any]:
        return await begin_ingestion(artifact_name, runtime)

    def submit_batch(
        self,
        ingestion_id: str,
        batch_index: int,
        graph_fragment: GraphPatchFragment,
        runtime: IngestionRuntime,
    ) -> dict[str, Any]:
        return submit_ingestion_batch(
            ingestion_id,
            batch_index,
            graph_fragment,
            runtime,
        )

    def finalize(
        self,
        ingestion_id: str,
        runtime: IngestionRuntime,
    ) -> dict[str, Any]:
        return finalize_ingestion(ingestion_id, runtime)

    async def fill(
        self,
        ingestion_id: str,
        runtime: IngestionRuntime,
        allow_partial_persistence: bool = False,
    ) -> dict[str, Any]:
        return await fill_ingestion(
            ingestion_id,
            runtime,
            allow_partial_persistence=allow_partial_persistence,
        )

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
