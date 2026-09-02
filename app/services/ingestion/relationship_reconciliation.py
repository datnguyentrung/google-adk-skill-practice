"""Ontology-driven, LLM-reasoned readiness reconciliation.

Batch-local extraction may legally produce structurally valid fragments but
miss semantically required properties or relationships.
This module reconciles such gaps: it builds a compact context from the
canonical graph, the unmet ontology constraints, and relevant source chunks,
asks the LLM whether the source actually supports the missing facts,
and returns a candidate GraphPatchFragment that the existing merge + assess
pipeline re-validates. The validator remains the guardrail; the LLM is the
semantic reasoner. Nothing here hard-codes ontology identifiers.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any

from google import genai
from google.genai import types

from app.core.schemas.ingestion.document import DocumentChunk
from app.core.schemas.ingestion.graph_patch import GraphPatchDraft, GraphPatchFragment
from app.core.schemas.ingestion.validation import ValidationCode, ValidationIssue
from app.services.ingestion.graph_patch_compiler import CompiledGraphPatch
from app.services.ingestion.registry import OntologyRegistry
from app.services.ingestion.staged_ingestion import IngestionWorkspaceService
from app.services.ingestion.validate_graph_patch import (
    GraphPatchAssessment,
    GraphPatchValidationService,
)

logger = logging.getLogger(__name__)

DEFAULT_RECONCILIATION_MODEL = os.getenv(
    "INGESTION_RECONCILIATION_MODEL",
    os.getenv(
        "INGESTION_ORCHESTRATOR_MODEL",
        os.getenv("GOOGLE_ADK_MODEL", "gemini-3.1-flash-lite"),
    ),
)
DEFAULT_MAX_PASSES = max(1, int(os.getenv("INGESTION_RECONCILIATION_MAX_PASSES", "2")))
DEFAULT_MAX_CHUNKS = max(1, int(os.getenv("INGESTION_RECONCILIATION_MAX_CHUNKS", "12")))
DEFAULT_CHUNK_CHARS = 1200
DEFAULT_FULL_DOCUMENT_CHARS = max(
    1,
    int(os.getenv("INGESTION_RECONCILIATION_FULL_DOCUMENT_CHARS", "120000")),
)


@dataclass(frozen=True)
class ReconciliationOutcome:
    reconciled: bool
    draft: GraphPatchDraft | None
    fingerprint: str | None
    passes_used: int
    exhausted: bool
    issues: tuple[ValidationIssue, ...] = ()
    assessment: GraphPatchAssessment | None = None


def reconciliation_triggered(readiness_issues) -> bool:
    """Trigger when ontology readiness gaps may be source-repaired."""

    issues = list(readiness_issues)
    if not issues:
        return False
    has_ontology_gap = any(
        issue.code == ValidationCode.ONTOLOGY_RULE_UNSATISFIED
        and (issue.edge_name is not None or issue.property_name is not None)
        for issue in issues
    )
    if not has_ontology_gap:
        return False
    repairable_codes = {
        ValidationCode.ONTOLOGY_RULE_UNSATISFIED,
        ValidationCode.IDENTITY_UNRESOLVED,
    }
    return all(issue.code in repairable_codes for issue in issues)


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"\w+", text.casefold()))


class RelationshipReconciler:
    """Build a candidate patch for unmet ontology-required facts."""

    def __init__(
        self,
        *,
        model: str = DEFAULT_RECONCILIATION_MODEL,
        client: genai.Client | None = None,
        max_passes: int = DEFAULT_MAX_PASSES,
        max_chunks: int = DEFAULT_MAX_CHUNKS,
        chunk_chars: int = DEFAULT_CHUNK_CHARS,
        full_document_chars: int = DEFAULT_FULL_DOCUMENT_CHARS,
    ):
        self.model = model
        self.client = client or genai.Client(api_key=os.getenv("GOOGLE_API_KEY"))
        self.max_passes = max(1, max_passes)
        self.max_chunks = max(1, max_chunks)
        self.chunk_chars = max(1, chunk_chars)
        self.full_document_chars = max(1, full_document_chars)

    def reconcile(
        self,
        *,
        merged_draft: GraphPatchDraft,
        compiled_patch: CompiledGraphPatch,
        readiness_issues: list[ValidationIssue],
        chunks: list[DocumentChunk],
        validation_service: GraphPatchValidationService,
        artifact_digest: str,
        registry: OntologyRegistry,
    ) -> ReconciliationOutcome:
        if not reconciliation_triggered(readiness_issues):
            return ReconciliationOutcome(
                reconciled=False,
                draft=None,
                fingerprint=None,
                passes_used=0,
                exhausted=False,
                issues=tuple(readiness_issues),
            )

        canonical = GraphPatchFragment.model_validate(
            merged_draft.model_dump(by_alias=True, mode="json")
        )
        current_patch = compiled_patch
        current_draft = merged_draft
        passes = 0
        previous_attempt: dict[str, Any] | None = None
        while passes < self.max_passes:
            passes += 1
            context = self._build_context(
                draft=current_draft,
                compiled_patch=current_patch,
                gaps=readiness_issues,
                chunks=chunks,
                registry=registry,
            )
            candidate = self._ask_llm(context, previous_attempt)
            if candidate is None:
                break
            aligned = self._align_candidate_coverage(candidate, canonical)
            merged = IngestionWorkspaceService.merge_fragments(
                [canonical, aligned]
            )
            merged = self._apply_candidate_coverage_signals(merged, candidate)
            draft = GraphPatchDraft.model_validate(
                merged.model_dump(by_alias=True, mode="json")
            )
            assessment = validation_service.assess(
                draft,
                artifact_digest,
                chunks,
            )
            if assessment.result.valid_for_persistence:
                return ReconciliationOutcome(
                    reconciled=True,
                    draft=draft,
                    fingerprint=assessment.fingerprint,
                    passes_used=passes,
                    exhausted=False,
                    issues=(),
                    assessment=assessment,
                )
            gaps = [
                issue
                for issue in assessment.result.readiness_issues
                if issue.code
                in {
                    ValidationCode.ONTOLOGY_RULE_UNSATISFIED,
                    ValidationCode.IDENTITY_UNRESOLVED,
                }
            ]
            if not gaps:
                return ReconciliationOutcome(
                    reconciled=False,
                    draft=draft,
                    fingerprint=None,
                    passes_used=passes,
                    exhausted=True,
                    issues=(
                        *assessment.result.errors,
                        *assessment.result.readiness_issues,
                    ),
                )
            previous_attempt = {
                "pass": passes,
                "errors": [
                    self._issue_summary(issue)
                    for issue in assessment.result.errors
                ],
                "readinessIssues": [
                    self._issue_summary(issue)
                    for issue in assessment.result.readiness_issues
                ],
            }
            canonical = merged
            current_draft = draft
            current_patch = assessment.compiled_patch
            readiness_issues = gaps

        return ReconciliationOutcome(
            reconciled=False,
            draft=None,
            fingerprint=None,
            passes_used=passes,
            exhausted=True,
            issues=tuple(readiness_issues),
        )

    def _ask_llm(
        self,
        context: str,
        previous_attempt: dict[str, Any] | None = None,
    ) -> GraphPatchFragment | None:
        try:
            response = self.client.models.generate_content(
                model=self.model,
                contents=self._prompt(context, previous_attempt),
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_json_schema=GraphPatchFragment.model_json_schema(),
                    temperature=0,
                ),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("RECONCILIATION_LLM_FAILED error=%s", exc)
            return None
        try:
            raw = getattr(response, "parsed", None)
            if raw is None:
                raw = json.loads(getattr(response, "text", "") or "{}")
            return GraphPatchFragment.model_validate(raw)
        except Exception as exc:  # noqa: BLE001
            logger.warning("RECONCILIATION_INVALID_PATCH error=%s", exc)
            return None

    @staticmethod
    def _align_candidate_coverage(
        candidate: GraphPatchFragment,
        canonical: GraphPatchFragment,
    ) -> GraphPatchFragment:
        """Mirror canonical decisions so merge_fragments sees no conflicts."""

        canonical_by_index = {
            item.chunk_index: item for item in canonical.coverage
        }
        aligned = candidate.model_copy(deep=True)
        for index, item in enumerate(aligned.coverage):
            existing = canonical_by_index.get(item.chunk_index)
            if existing is not None and existing.decision != item.decision:
                aligned.coverage[index] = existing.model_copy(deep=True)
        return aligned

    @staticmethod
    def _apply_candidate_coverage_signals(
        merged: GraphPatchFragment,
        candidate: GraphPatchFragment,
    ) -> GraphPatchFragment:
        """Upgrade canonical NO_RELEVANT_FACT to candidate relevance signals."""

        candidate_by_index = {
            item.chunk_index: item.decision for item in candidate.coverage
        }
        coverage = []
        for item in merged.coverage:
            signal = candidate_by_index.get(item.chunk_index)
            if (
                signal in {"MAPPED", "AMBIGUOUS", "FAILED"}
                and item.decision == "NO_RELEVANT_FACT"
            ):
                item = item.model_copy(
                    update={
                        "decision": signal,
                        "reason": item.reason + " (reconciliation)",
                    }
                )
            coverage.append(item)
        return merged.model_copy(update={"coverage": coverage})

    @staticmethod
    def _issue_summary(issue: ValidationIssue) -> dict[str, Any]:
        return {
            key: value
            for key, value in {
                "code": issue.code.value,
                "message": issue.message,
                "location": issue.location,
                "nodeTempId": issue.node_temp_id,
                "propertyName": issue.property_name,
                "edgeName": issue.edge_name,
            }.items()
            if value is not None
        }

    @staticmethod
    def _prompt(
        context: str,
        previous_attempt: dict[str, Any] | None = None,
    ) -> str:
        repair_block = ""
        if previous_attempt:
            repair_block = (
                "A previous reconciliation attempt did not satisfy validation. "
                "Inspect the reported issues and produce a corrected fragment. "
                "Do not repeat the same mistakes.\n"
                + json.dumps(previous_attempt, ensure_ascii=False)
                + "\n\n"
            )
        return (
            "You reconcile a Product Sales Knowledge Graph that failed ontology "
            "readiness because required source facts are missing. Decide whether "
            "the cited source evidence actually supports adding the missing "
            "properties or outgoing edges. If yes, return a GraphPatchFragment "
            "that adds only the required candidate facts, using existing "
            "canonical refs as endpoints when possible and avoiding duplicate "
            "nodes. Literal-sensitive values such as identifiers, codes, dates, "
            "versions, statuses, numeric amounts, and limits must be copied or "
            "normalized only from explicit source evidence; convert dates to ISO "
            "format only when the source meaning is unambiguous. Use verbatim evidence from "
            "the cited chunks and mark every chunk you ground as MAPPED. If the "
            "evidence does not support a missing fact, do not invent it; mark the "
            "relevant chunks AMBIGUOUS in coverage instead. The response schema "
            "(GraphPatchFragment) is enforced: exactly one object with nodes, "
            "edges, coverage, warnings and no extra keys.\n\n"
            f"{repair_block}"
            f"{context}"
        )

    def _build_context(
        self,
        *,
        draft: GraphPatchDraft,
        compiled_patch: CompiledGraphPatch,
        gaps: list[ValidationIssue],
        chunks: list[DocumentChunk],
        registry: OntologyRegistry,
    ) -> str:
        gap_lines: list[str] = []
        for gap in gaps:
            node_class = None
            node_properties: dict[str, Any] = {}
            if gap.node_temp_id is not None:
                node = next(
                    (
                        item
                        for item in compiled_patch.nodes
                        if item.temp_id == gap.node_temp_id
                    ),
                    None,
                )
                node_class = node.class_name if node is not None else None
                node_properties = node.properties if node is not None else {}
            edge = registry.get_edge(gap.edge_name) if gap.edge_name else None
            attribute = (
                registry.get_attribute(gap.property_name)
                if gap.property_name
                else None
            )
            if edge is not None:
                gap_lines.append(
                    "- node="
                    + (gap.node_temp_id or "?")
                    + " class="
                    + (node_class or "?")
                    + " requires outgoing edge "
                    + gap.edge_name
                    + " domain="
                    + json.dumps(edge.domain, ensure_ascii=False)
                    + " range="
                    + json.dumps(edge.range, ensure_ascii=False)
                    + " definition="
                    + edge.definition
                    + " minimum=1"
                )
                continue
            if attribute is not None:
                current = node_properties.get(gap.property_name)
                gap_lines.append(
                    "- node="
                    + (gap.node_temp_id or "?")
                    + " class="
                    + (node_class or "?")
                    + " requires property "
                    + gap.property_name
                    + " range="
                    + json.dumps(attribute.range, ensure_ascii=False)
                    + " definition="
                    + attribute.definition
                    + " current="
                    + json.dumps(current, ensure_ascii=False, default=str)
                )
                continue
            gap_lines.append(
                "- node="
                + (gap.node_temp_id or "?")
                + " class="
                + (node_class or "?")
                + " readiness issue "
                + gap.code.value
                + ": "
                + gap.message
            )

        node_lines: list[str] = []
        for node in compiled_patch.nodes:
            identity = {
                key: value
                for key, value in node.properties.items()
                if not isinstance(value, (dict, list))
            }
            node_lines.append(f"- ref={node.temp_id}")
            node_lines.append(f"  class={node.class_name}")
            node_lines.append(
                "  identity="
                + json.dumps(identity, ensure_ascii=False, sort_keys=True)
            )
        edge_lines = [
            f"- {edge.edge_name}: {edge.source_temp_id} -> {edge.target_temp_id}"
            for edge in compiled_patch.edges
        ]

        full_document = (
            sum(len(chunk.content) for chunk in chunks) <= self.full_document_chars
        )
        selected = (
            list(chunks)
            if full_document
            else self._select_chunks(
                gaps=gaps,
                compiled_patch=compiled_patch,
                draft=draft,
                chunks=chunks,
                registry=registry,
            )
        )
        disposition_by_index = {
            item.chunk_index: item.decision for item in draft.coverage
        }
        chunk_lines: list[str] = []
        for chunk in selected:
            chunk_lines.append(
                f"[CHUNK {chunk.index}] section={chunk.section} "
                f"coverage_disposition={disposition_by_index.get(chunk.index, '?')}"
            )
            content = chunk.content
            if not full_document:
                content = content[: self.chunk_chars]
            chunk_lines.append(content)

        lines: list[str] = ["Unmet ontology requirements:"]
        lines.extend(gap_lines)
        lines.append("")
        lines.append("Existing canonical graph:")
        lines.append("Nodes:")
        lines.extend(node_lines)
        if edge_lines:
            lines.append("Existing edges:")
            lines.extend(edge_lines)
        lines.append("")
        lines.append("Relevant source chunks:")
        lines.extend(chunk_lines)
        return "\n".join(lines)

    def _select_chunks(
        self,
        *,
        gaps: list[ValidationIssue],
        compiled_patch: CompiledGraphPatch,
        draft: GraphPatchDraft,
        chunks: list[DocumentChunk],
        registry: OntologyRegistry,
    ) -> list[DocumentChunk]:
        node_ids = {
            gap.node_temp_id for gap in gaps if gap.node_temp_id is not None
        }
        node_chunk_indexes: set[int] = set()
        for node in compiled_patch.nodes:
            if node.temp_id not in node_ids:
                continue
            node_chunk_indexes.update(item.chunk_index for item in node.evidence)
            for items in node.property_evidence.values():
                node_chunk_indexes.update(item.chunk_index for item in items)

        coverage_indexes = {
            item.chunk_index
            for item in draft.coverage
            if item.decision in {"MAPPED", "AMBIGUOUS", "FAILED"}
        }

        edge_terms: set[str] = set()
        for gap in gaps:
            edge = registry.get_edge(gap.edge_name) if gap.edge_name else None
            attribute = (
                registry.get_attribute(gap.property_name)
                if gap.property_name
                else None
            )
            edge_terms.update(_tokens(gap.message))
            if gap.property_name:
                edge_terms.update(_tokens(gap.property_name))
            if gap.edge_name:
                edge_terms.update(_tokens(gap.edge_name))
            if edge is not None:
                edge_terms.update(_tokens(edge.label))
                edge_terms.update(_tokens(edge.definition))
                for term in [*edge.domain, *edge.range]:
                    edge_terms.update(_tokens(term))
            if attribute is not None:
                edge_terms.update(_tokens(attribute.label))
                edge_terms.update(_tokens(attribute.definition))
                for term in attribute.range:
                    edge_terms.update(_tokens(term))

        lexical_score = {
            chunk.index: len(_tokens(chunk.content) & edge_terms)
            for chunk in chunks
        }
        chunk_by_index = {chunk.index: chunk for chunk in chunks}

        ordered: list[DocumentChunk] = []
        seen: set[int] = set()

        def _add(index: int) -> None:
            if index in seen:
                return
            seen.add(index)
            chunk = chunk_by_index.get(index)
            if chunk is not None:
                ordered.append(chunk)

        for index in sorted(node_chunk_indexes):
            _add(index)
        for index in sorted(coverage_indexes):
            _add(index)
        for chunk in sorted(chunks, key=lambda item: -lexical_score.get(item.index, 0)):
            _add(chunk.index)
        return ordered[: self.max_chunks]


__all__ = [
    "ReconciliationOutcome",
    "RelationshipReconciler",
    "reconciliation_triggered",
]
