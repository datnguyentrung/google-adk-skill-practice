from __future__ import annotations

import json
import logging
import os
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.schemas.ingestion.document import DocumentChunk
from app.core.schemas.ingestion.graph_patch import Evidence, GraphPatchDraft
from app.core.schemas.ingestion.validation import GraphPatchValidationResult, ValidationIssue
from app.services.ingestion.graph_patch_compiler import (
    DEFAULT_ONTOLOGY_PATH, CompiledGraphPatch, GraphPatchCompiler,
)
from app.services.ingestion.identity import (
    IdentityResolutionError, create_product_sales_identity_resolver, source_scope_from_evidence,
)
from app.services.ingestion.loader import OntologyLoader
from app.services.ingestion.model_call_control import AdkStructuredCallExecutor
from app.services.ingestion.ontology_datatypes import XsdDatatype, value_matches_xsd, xsd_datatypes
from app.services.ingestion.registry import OntologyRegistry

logger = logging.getLogger(__name__)


def deduplicate_issues(issues: list[ValidationIssue]) -> list[ValidationIssue]:
    unique = {(str(issue.code), issue.location, issue.message): issue for issue in issues}
    return list(unique.values())


def cardinality_failure(operator: str, raw_expected: Any, actual_count: int) -> tuple[str, int] | None:
    if operator == "some":
        return ("minimum", 1) if actual_count < 1 else None
    try:
        expected = int(raw_expected)
    except (TypeError, ValueError):
        return None
    if operator == "exactlyQualified" and actual_count != expected:
        return ("exactly", expected)
    if operator == "minQualified" and actual_count < expected:
        return ("minimum", expected)
    return None


DEFAULT_SEMANTIC_GROUNDING_MODEL = os.getenv(
    "INGESTION_SEMANTIC_GROUNDING_MODEL",
    os.getenv("INGESTION_ORCHESTRATOR_MODEL", os.getenv("GOOGLE_ADK_MODEL", "gemini-3.1-flash-lite")),
)


@dataclass(frozen=True)
class SemanticGroundingDecision:
    verdict: Literal["supported", "unsupported", "unknown"]
    reason: str = ""
    missing_evidence: tuple[str, ...] = ()


class SemanticGroundingJudge(Protocol):
    def judge_edge(
        self,
        *,
        edge: Any,
        source_node: Any,
        target_node: Any,
        evidence_items: list[Evidence],
        registry: OntologyRegistry,
    ) -> SemanticGroundingDecision:
        """Decide whether evidence semantically supports an ontology edge."""


class SemanticValueJudge(Protocol):
    def judge_value(
        self,
        *,
        property_name: str,
        value: Any,
        evidence_items: list[Evidence],
        attribute: Any,
    ) -> SemanticGroundingDecision:
        """Decide whether evidence semantically supports a property value."""


class PermissiveSemanticGroundingJudge:
    """Local fallback: never blocks semantics when no LLM judge is configured."""

    def judge_edge(
        self,
        *,
        edge: Any,
        source_node: Any,
        target_node: Any,
        evidence_items: list[Evidence],
        registry: OntologyRegistry,
    ) -> SemanticGroundingDecision:
        return SemanticGroundingDecision(
            verdict="supported",
            reason="Semantic grounding judge is not configured; deterministic checks passed.",
        )


class _JudgeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: Literal["supported", "unsupported", "unknown"]
    reason: str = Field(default="")
    missing_evidence: list[str] | None = Field(default=None, alias="missingEvidence")


class GeminiSemanticGroundingJudge:
    """LLM-backed semantic grounding without per-edge keyword functions."""

    def __init__(
        self,
        *,
        model: str = DEFAULT_SEMANTIC_GROUNDING_MODEL,
        structured_executor: AdkStructuredCallExecutor | None = None,
    ):
        self.model = model
        self.structured_executor = structured_executor or AdkStructuredCallExecutor(
            model=self.model
        )
        self._cache: dict[str, SemanticGroundingDecision] = {}

    def judge_edge(
        self,
        *,
        edge: Any,
        source_node: Any,
        target_node: Any,
        evidence_items: list[Evidence],
        registry: OntologyRegistry,
    ) -> SemanticGroundingDecision:
        payload = self._payload(
            edge=edge,
            source_node=source_node,
            target_node=target_node,
            evidence_items=evidence_items,
            registry=registry,
        )
        key = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        if key in self._cache:
            return self._cache[key]
        decision = self._run_judge(
            operation="semantic_grounding_edge",
            prompt=self._prompt(payload),
            log_context=f"edge_name={edge.edge_name}",
        )
        self._cache[key] = decision
        return decision

    @staticmethod
    def _prompt(payload: dict[str, Any]) -> str:
        return (
            "You judge whether cited source evidence semantically supports one "
            "Product Sales Knowledge Graph edge. Use reasoning over the source "
            "language; do not require ontology labels or English keywords to appear. "
            "Return only JSON with keys verdict, reason, missingEvidence. "
            "Use verdict='supported' when the evidence, node facts, and ontology "
            "definition make the relationship directly stated or unambiguously "
            "entailed. Use verdict='unsupported' only when evidence contradicts or "
            "does not identify the relationship. Use verdict='unknown' for model "
            "or evidence ambiguity that should not be converted into a hard-coded "
            "keyword failure.\n\n"
            f"{json.dumps(payload, ensure_ascii=False, default=str)}"
        )

    @staticmethod
    def _payload(
        *,
        edge: Any,
        source_node: Any,
        target_node: Any,
        evidence_items: list[Evidence],
        registry: OntologyRegistry,
    ) -> dict[str, Any]:
        ontology_edge = registry.get_edge(edge.edge_name)
        return {
            "edge": {
                "edgeName": edge.edge_name,
                "sourceTempId": edge.source_temp_id,
                "targetTempId": edge.target_temp_id,
            },
            "ontologyEdge": (
                None
                if ontology_edge is None
                else {
                    "technicalName": ontology_edge.technical_name,
                    "label": ontology_edge.label,
                    "definition": ontology_edge.definition,
                    "domain": ontology_edge.domain,
                    "range": ontology_edge.range,
                }
            ),
            "sourceNode": _node_payload(source_node),
            "targetNode": _node_payload(target_node),
            "evidence": [
                {
                    "source": item.source,
                    "chunkIndex": item.chunk_index,
                    "section": item.section,
                    "text": item.text,
                }
                for item in evidence_items
            ],
        }


    def judge_value(
        self,
        *,
        property_name: str,
        value: Any,
        evidence_items: list[Evidence],
        attribute: Any,
    ) -> SemanticGroundingDecision:
        payload = self._value_payload(
            property_name=property_name,
            value=value,
            evidence_items=evidence_items,
            attribute=attribute,
        )
        key = "value:" + json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        if key in self._cache:
            return self._cache[key]
        decision = self._run_judge(
            operation="semantic_grounding_value",
            prompt=self._value_prompt(payload),
            log_context=f"property={property_name}",
        )
        self._cache[key] = decision
        return decision

    def _run_judge(
        self,
        *,
        operation: str,
        prompt: str,
        log_context: str,
    ) -> SemanticGroundingDecision:
        try:
            payload = self.structured_executor.run(
                operation=operation,
                instruction=prompt,
                output_schema=_JudgeResponse.model_json_schema(by_alias=True),
                message="Judge the supplied evidence and return the structured verdict.",
            )
            model_response = _JudgeResponse.model_validate(payload)
            return SemanticGroundingDecision(
                verdict=model_response.verdict,
                reason=model_response.reason,
                missing_evidence=tuple(model_response.missing_evidence or []),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Semantic grounding judge failed %s error=%s", log_context, exc)
            return SemanticGroundingDecision(
                verdict="unknown",
                reason=f"Semantic grounding judge failed: {exc}",
            )

    @staticmethod
    def _value_prompt(payload: dict[str, Any]) -> str:
        return (
            "You judge whether cited source evidence semantically supports one "
            "Product Sales Knowledge Graph property value. The value may contain "
            "multiple claims; you must evaluate EVERY claim in the value against "
            "the ENTIRE evidence list. Return only JSON with keys verdict, reason, "
            "missingEvidence. Use verdict='supported' only when every claim is "
            "directly stated or unambiguously entailed by the evidence, including "
            "reasonable normalization of number, currency, date, and percent "
            "formats. Use verdict='unsupported' when any claim is missing, "
            "contradicted, or only weakly related; numeric or keyword overlap "
            "alone is NOT sufficient. Use verdict='unknown' for model or evidence "
            "ambiguity that must not be accepted as grounded. In missingEvidence, "
            "list each claim that the evidence does not support.\n\n"
            f"{json.dumps(payload, ensure_ascii=False, default=str)}"
        )

    @staticmethod
    def _value_payload(
        *,
        property_name: str,
        value: Any,
        evidence_items: list[Evidence],
        attribute: Any,
    ) -> dict[str, Any]:
        attribute_payload = None
        if attribute is not None:
            policy = getattr(attribute, "ingestion_policy", None)
            attribute_payload = {
                "technicalName": getattr(attribute, "technical_name", None),
                "label": getattr(attribute, "label", None),
                "definition": getattr(attribute, "definition", None),
                "range": getattr(attribute, "range", None),
                "grounding": getattr(policy, "grounding", None),
            }
        return {
            "property": property_name,
            "value": value,
            "ontologyAttribute": attribute_payload,
            "evidence": [
                {
                    "source": item.source,
                    "chunkIndex": item.chunk_index,
                    "section": item.section,
                    "text": item.text,
                }
                for item in evidence_items
            ],
        }


def create_default_semantic_grounding_judge() -> SemanticGroundingJudge:
    if (
        os.getenv("INGESTION_SEMANTIC_GROUNDING_JUDGE", "").casefold()
        == "gemini"
        and os.getenv("GOOGLE_API_KEY")
    ):
        return GeminiSemanticGroundingJudge()
    return PermissiveSemanticGroundingJudge()


def create_default_semantic_value_judge() -> SemanticValueJudge | None:
    """Return a Gemini value judge by default when a key exists; else fail closed."""

    if os.getenv("GOOGLE_API_KEY"):
        return GeminiSemanticGroundingJudge()
    return None


def _node_payload(node: Any) -> dict[str, Any]:
    properties = getattr(node, "properties", {})
    if isinstance(properties, dict):
        props = properties
    else:
        props = {
            item.property_name: item.value
            for item in properties
            if hasattr(item, "property_name")
        }
    return {
        "tempId": getattr(node, "temp_id", None),
        "className": getattr(node, "class_name", None),
        "properties": props,
    }

_SENSITIVE_PATTERN = re.compile(
    r"(?i)(authorization\s*[:=]\s*\S+|api[_-]?key\s*[:=]\s*\S+|"
    r"token\s*[:=]\s*\S+|password\s*[:=]\s*\S+|pin\s*[:=]\s*\S+|"
    r"otp\s*[:=]\s*\S+)"
)


def _safe_preview(value: Any, *, limit: int = 500) -> str:
    text = value if isinstance(value, str) else repr(value)
    text = _SENSITIVE_PATTERN.sub("<REDACTED>", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    half = max(1, limit // 2)
    return f"{text[:half]} ... {text[-half:]}"


def _evidence_trace(evidence_items: Iterable[Evidence]) -> list[dict[str, Any]]:
    return [
        {
            "chunkIndex": item.chunk_index,
            "source": item.source,
            "section": item.section,
            "text": _safe_preview(item.text),
        }
        for item in evidence_items
    ]


class SourceGroundingValidator:
    """Validate completeness from grounded property and relationship facts."""

    def __init__(
        self,
        registry: OntologyRegistry,
        semantic_judge: SemanticGroundingJudge | None = None,
        semantic_value_judge: SemanticValueJudge | None = None,
    ):
        self.registry = registry
        self.semantic_judge = semantic_judge or PermissiveSemanticGroundingJudge()
        self.semantic_value_judge = semantic_value_judge

    def validate(
        self,
        draft: GraphPatchDraft,
        chunks: list[DocumentChunk],
        *,
        context_nodes: list[Any] | tuple[Any, ...] | None = None,
        evidence_context: list[DocumentChunk] | None = None,
    ) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        expected_chunk_by_index = {chunk.index: chunk for chunk in chunks}
        chunk_by_index = dict(expected_chunk_by_index)
        chunk_by_index.update({chunk.index: chunk for chunk in (evidence_context or [])})
        expected_indexes = set(expected_chunk_by_index)
        coverage_by_index = {}
        fact_chunks_by_claim: dict[int, list[str]] = {}
        for position, item in enumerate(draft.coverage):
            if item.chunk_index in coverage_by_index:
                issues.append(
                    ValidationIssue(
                        code="DUPLICATE_COVERAGE",
                        message=(
                            f"Chunk {item.chunk_index} appears more than once in coverage"
                        ),
                        location=f"coverage.{position}.chunkIndex",
                    )
                )
            coverage_by_index[item.chunk_index] = item

        supplied_indexes = set(coverage_by_index)
        for chunk_index in sorted(expected_indexes - supplied_indexes):
            issues.append(
                ValidationIssue(
                    code="COVERAGE_MISSING",
                    message=f"Prepared source chunk {chunk_index} is missing from coverage",
                    location="coverage",
                )
            )
        for chunk_index in sorted(supplied_indexes - expected_indexes):
            issues.append(
                ValidationIssue(
                    code="COVERAGE_UNKNOWN_CHUNK",
                    message=f"Coverage references unknown source chunk {chunk_index}",
                    location=f"coverage.{chunk_index}",
                )
            )

        fact_chunks: set[int] = set()
        related_edge_chunks: dict[str, set[int]] = {}
        node_by_temp_id = {node.temp_id: node for node in (context_nodes or [])}
        node_by_temp_id.update({node.temp_id: node for node in draft.nodes})
        for edge_index, edge in enumerate(draft.edges):
            edge_issues, valid_chunks = self._validate_evidence(
                edge.evidence,
                chunk_by_index,
                f"edges.{edge_index}.evidence",
                edge_name=edge.edge_name,
            )
            issues.extend(edge_issues)
            valid_evidence = [
                item for item in edge.evidence if item.chunk_index in valid_chunks
            ]
            grounded_chunks: set[int] = set()
            if valid_evidence:
                supported, grounded_chunks, unrelated = self._edge_support_details(
                    edge, valid_evidence, node_by_temp_id
                )
                logger.debug(
                    "[GROUNDING_CHECK] node_id=%s edge=%s source_node=%s "
                    "target_node=%s evidence=%s deterministic_check=%s "
                    "semantic_judge=%s semantic_verdict=%s reason=%s",
                    None,
                    edge.edge_name,
                    edge.source_temp_id,
                    edge.target_temp_id,
                    _evidence_trace(valid_evidence),
                    "PASSED" if supported else "FAILED",
                    type(self.semantic_judge).__name__,
                    "SUPPORTED" if supported else "UNSUPPORTED",
                    "edge semantic grounding",
                )
                if not supported:
                    issues.append(ValidationIssue(
                        code="EDGE_RELATION_NOT_GROUNDED",
                        message=(f"Edge {edge.edge_name} evidence must collectively "
                                 "identify both endpoints and express the predicate"),
                        location=f"edges.{edge_index}.evidence",
                        edge_name=edge.edge_name,
                    ))
                else:
                    for evidence_index in unrelated:
                        issues.append(ValidationIssue(
                            code="EDGE_RELATION_NOT_GROUNDED",
                            message=(f"Edge {edge.edge_name} evidence item does not "
                                     "contribute an endpoint or predicate fact"),
                            location=f"edges.{edge_index}.evidence.{evidence_index}",
                            edge_name=edge.edge_name,
                        ))
            fact_chunks.update(grounded_chunks)
            for chunk_index in grounded_chunks:
                fact_chunks_by_claim.setdefault(chunk_index, []).append(
                    f"edge.{edge.source_temp_id}.{edge.edge_name}.{edge.target_temp_id}"
                )
            related_edge_chunks.setdefault(edge.source_temp_id, set()).update(grounded_chunks)
            related_edge_chunks.setdefault(edge.target_temp_id, set()).update(grounded_chunks)

        for node_index, node in enumerate(draft.nodes):
            node_issues, _ = self._validate_evidence(
                node.evidence,
                chunk_by_index,
                f"nodes.{node_index}.evidence",
                node_temp_id=node.temp_id,
            )
            issues.extend(node_issues)
            for property_index, entry in enumerate(node.properties):
                location = f"nodes.{node_index}.properties.{property_index}"
                evidence_issues, valid_chunks = self._validate_evidence(
                    entry.evidence,
                    chunk_by_index,
                    f"{location}.evidence",
                    node_temp_id=node.temp_id,
                    property_name=entry.property_name,
                )
                issues.extend(evidence_issues)
                attribute = self.registry.get_attribute(entry.property_name)
                if attribute is None:
                    continue
                valid_evidence = [
                    evidence
                    for evidence in entry.evidence
                    if evidence.chunk_index in valid_chunks
                ]
                grounding = self._property_grounding(
                    entry.property_name,
                    attribute,
                )
                if grounding == "derived":
                    logger.debug(
                        "[PROPERTY_ORIGIN] node=%s property=%s origin=%s grounding=%s "
                        "policy_mode=%s",
                        node.temp_id,
                        entry.property_name,
                        "SYSTEM_GENERATED"
                        if attribute.ingestion_policy.mode
                        in {"runtime_managed", "system_default", "edge_derived"}
                        else "LLM_DERIVED",
                        grounding,
                        attribute.ingestion_policy.mode,
                    )
                    if (
                        attribute.ingestion_policy.mode == "edge_derived"
                        and not related_edge_chunks.get(node.temp_id)
                    ):
                        issues.append(
                            ValidationIssue(
                                code="DERIVED_PROPERTY_REQUIRES_EDGE_EVIDENCE",
                                message=(
                                    f"Compiler-derived {entry.property_name} requires "
                                    "a grounded relationship involving this node"
                                ),
                                location=location,
                                node_temp_id=node.temp_id,
                                property_name=entry.property_name,
                            )
                        )
                    continue
                if grounding == "normalized":
                    deterministic_chunks = self._value_supported_chunks(
                        entry.value,
                        valid_evidence,
                        attribute.range,
                    )
                    supported_chunks = self._normalized_value_supported_chunks(
                        entry.value,
                        valid_evidence,
                        attribute,
                    )
                else:
                    deterministic_chunks = self._value_supported_chunks(
                        entry.value,
                        valid_evidence,
                        attribute.range,
                    )
                    supported_chunks = self._value_supported_chunks(
                        entry.value,
                        valid_evidence,
                        attribute.range,
                    )
                reason_code = self._grounding_failure_reason(
                    entry.value,
                    entry.evidence,
                    valid_evidence,
                    valid_chunks,
                    attribute.range,
                )
                logger.debug(
                    "[GROUNDING_CHECK] node_id=%s property=%s property_index=%s "
                    "value=%r evidence=%s deterministic_check=%s semantic_judge=%s "
                    "semantic_verdict=%s reason_code=%s grounding=%s",
                    node.temp_id,
                    entry.property_name,
                    property_index,
                    _safe_preview(entry.value),
                    _evidence_trace(entry.evidence),
                    "PASSED" if deterministic_chunks else "FAILED",
                    (
                        type(self.semantic_value_judge).__name__
                        if self.semantic_value_judge is not None
                        else "none"
                    ),
                    "SUPPORTED" if supported_chunks else "UNSUPPORTED",
                    reason_code,
                    grounding,
                )
                if supported_chunks:
                    fact_chunks.update(supported_chunks)
                    for chunk_index in supported_chunks:
                        fact_chunks_by_claim.setdefault(chunk_index, []).append(
                            f"node.{node.temp_id}.{entry.property_name}"
                        )
                    continue
                if valid_evidence:
                    issues.append(
                        ValidationIssue(
                            code="PROPERTY_VALUE_NOT_GROUNDED",
                            message=self._value_grounding_message(
                                entry.property_name,
                                entry.value,
                                grounding,
                            ),
                            location=f"{location}.evidence",
                            node_temp_id=node.temp_id,
                            property_name=entry.property_name,
                        )
                    )

        for chunk_index, item in coverage_by_index.items():
            if chunk_index not in expected_indexes:
                continue
            if item.decision in {
                "FAILED",
                "AMBIGUOUS",
                "UNSUPPORTED_BY_ONTOLOGY",
            }:
                issues.append(
                    ValidationIssue(
                        code="COVERAGE_NOT_EVIDENCED",
                        message=(
                            f"Chunk {chunk_index} is marked {item.decision}; "
                            "the extraction is not semantically complete"
                        ),
                        location=f"coverage.{chunk_index}",
                    )
                )
            if item.decision == "MAPPED" and chunk_index not in fact_chunks:
                actual_evidence_chunks = sorted(
                    {
                        evidence.chunk_index
                        for node in draft.nodes
                        for prop in node.properties
                        for evidence in prop.evidence
                    }
                    | {
                        evidence.chunk_index
                        for edge in draft.edges
                        for evidence in edge.evidence
                    }
                )
                logger.debug(
                    "[COVERAGE_MISMATCH] chunk_id=%s claimed_by=%s "
                    "actual_evidence_chunks=%s",
                    chunk_index,
                    [f"coverage.{chunk_index}"],
                    actual_evidence_chunks,
                )
                issues.append(
                    ValidationIssue(
                        code="COVERAGE_NOT_EVIDENCED",
                        message=(
                            f"Chunk {chunk_index} is marked MAPPED but no grounded "
                            "property or edge fact references it"
                        ),
                        location=f"coverage.{chunk_index}",
                    )
                )
            if item.decision != "MAPPED" and chunk_index in fact_chunks:
                issues.append(
                    ValidationIssue(
                        code="COVERAGE_CONFLICT",
                        message=(
                            f"Chunk {chunk_index} is marked {item.decision} but is "
                            "used by a grounded property or edge fact"
                        ),
                        location=f"coverage.{chunk_index}",
                    )
                )
            logger.debug(
                "[COVERAGE_RESULT] chunk_id=%s disposition=%s reason=%r "
                "mapped_claims=%s",
                chunk_index,
                item.decision,
                _safe_preview(item.reason),
                fact_chunks_by_claim.get(chunk_index, []),
            )

        return deduplicate_issues(issues)

    def _edge_supported(
        self, edge, evidence_items: list[Evidence], node_by_temp_id: dict
    ) -> bool:
        supported, _, _ = self._edge_support_details(
            edge, evidence_items, node_by_temp_id
        )
        return supported

    def _edge_support_details(
        self, edge, evidence_items: list[Evidence], node_by_temp_id: dict
    ) -> tuple[bool, set[int], set[int]]:
        source = node_by_temp_id.get(edge.source_temp_id)
        target = node_by_temp_id.get(edge.target_temp_id)
        if source is None or target is None:
            return False, set(), set(range(len(evidence_items)))
        decision = self.semantic_judge.judge_edge(
            edge=edge,
            source_node=source,
            target_node=target,
            evidence_items=evidence_items,
            registry=self.registry,
        )
        return decision.verdict == "supported", {
            item.chunk_index for item in evidence_items
        }, set()

    def _property_grounding(
        self,
        property_name: str,
        attribute,
    ) -> str:
        policy = attribute.ingestion_policy
        if (
            policy.mode in {"runtime_managed", "system_default", "edge_derived"}
            or self.registry.is_runtime_managed_attribute(property_name)
            or bool(self.registry.edge_names_deriving_property(property_name))
        ):
            return "derived"
        if policy.grounding == "source_normalized":
            return "normalized"
        return "literal"

    @staticmethod
    def _value_grounding_message(
        property_name: str,
        value: Any,
        grounding: str,
    ) -> str:
        if grounding == "normalized":
            return (
                f"Property {property_name}={value!r} cannot be deterministically "
                "grounded in the valid evidence excerpts; a normalized paraphrase "
                "requires a semantic grounding judge or a value compatible with "
                "the source evidence"
            )
        return (
            f"Property {property_name}={value!r} is not supported by the valid "
            "evidence excerpts for this property"
        )

    def _normalized_value_supported_chunks(
        self,
        value: Any,
        evidence_items: list[Evidence],
        attribute,
    ) -> set[int]:
        """Deterministic compatibility first, then the semantic value judge."""

        if not evidence_items or value is None:
            return set()
        deterministic = self._value_supported_chunks(
            value,
            evidence_items,
            attribute.range,
        )
        if deterministic:
            return deterministic
        if self.semantic_value_judge is None:
            logger.info(
                "INGESTION_VALUE_GROUNDING property=%s deterministic_failed=True "
                "semantic_judge=none verdict=unsupported",
                attribute.technical_name,
            )
            return set()
        try:
            decision = self.semantic_value_judge.judge_value(
                property_name=attribute.technical_name,
                value=value,
                evidence_items=list(evidence_items),
                attribute=attribute,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "INGESTION_VALUE_GROUNDING property=%s semantic_judge=gemini "
                "error=%s verdict=unsupported",
                attribute.technical_name,
                exc,
            )
            return set()
        logger.info(
            "INGESTION_VALUE_GROUNDING property=%s deterministic_failed=True "
            "semantic_judge=gemini verdict=%s reason=%s",
            attribute.technical_name,
            decision.verdict,
            decision.reason,
        )
        if decision.verdict == "supported":
            return {evidence.chunk_index for evidence in evidence_items}
        return set()

    def _validate_evidence(
        self,
        evidence_items: Iterable[Evidence],
        chunk_by_index: dict[int, DocumentChunk],
        location_prefix: str,
        *,
        node_temp_id: str | None = None,
        property_name: str | None = None,
        edge_name: str | None = None,
    ) -> tuple[list[ValidationIssue], set[int]]:
        issues: list[ValidationIssue] = []
        valid_chunks: set[int] = set()
        for index, evidence in enumerate(evidence_items):
            location = f"{location_prefix}.{index}"
            chunk = chunk_by_index.get(evidence.chunk_index)
            evidence_valid = True
            if chunk is None:
                issues.append(
                    ValidationIssue(
                        code="EVIDENCE_UNKNOWN_CHUNK",
                        message=f"Evidence references unknown chunk {evidence.chunk_index}",
                        location=location,
                        node_temp_id=node_temp_id,
                        property_name=property_name,
                        edge_name=edge_name,
                    )
                )
                continue
            if evidence.source != chunk.source:
                evidence_valid = False
                issues.append(
                    ValidationIssue(
                        code="EVIDENCE_SOURCE_MISMATCH",
                        message=(
                            f"Evidence source {evidence.source!r} does not match "
                            f"prepared chunk source {chunk.source!r}"
                        ),
                        location=location,
                        node_temp_id=node_temp_id,
                        property_name=property_name,
                        edge_name=edge_name,
                    )
                )
            if evidence.section is not None and evidence.section != chunk.section:
                evidence_valid = False
                issues.append(
                    ValidationIssue(
                        code="EVIDENCE_SECTION_MISMATCH",
                        message=(
                            f"Evidence section {evidence.section!r} does not match "
                            f"chunk section {chunk.section!r}"
                        ),
                        location=location,
                        node_temp_id=node_temp_id,
                        property_name=property_name,
                        edge_name=edge_name,
                    )
                )
            if not (
                self._contains_quote(chunk.content, evidence.text)
                or self._contains_quote(chunk.section or "", evidence.text)
            ):
                evidence_valid = False
                issues.append(
                    ValidationIssue(
                        code="EVIDENCE_TEXT_NOT_IN_SOURCE",
                        message=(
                            "Evidence text is not a verbatim excerpt of chunk "
                            f"{evidence.chunk_index}"
                        ),
                        location=location,
                        node_temp_id=node_temp_id,
                        property_name=property_name,
                        edge_name=edge_name,
                    )
                )
            if evidence_valid:
                valid_chunks.add(evidence.chunk_index)
        return issues, valid_chunks

    @classmethod
    def _value_supported(
        cls,
        value: Any,
        evidence_text: str,
        ranges: list[str],
    ) -> bool:
        if isinstance(value, list):
            return bool(value) and all(
                cls._value_supported(item, evidence_text, ranges) for item in value
            )
        if value is None:
            return False
        datatypes = set(xsd_datatypes(ranges))
        if datatypes & {XsdDatatype.DATE, XsdDatatype.DATETIME}:
            return cls._date_supported(value, evidence_text)
        if XsdDatatype.BOOLEAN in datatypes and isinstance(value, bool):
            candidates = ("true", "yes", "có") if value else ("false", "no", "không")
            normalized = cls._normalize(evidence_text)
            return any(cls._normalize(item) in normalized for item in candidates)
        if datatypes & {XsdDatatype.INTEGER, XsdDatatype.DECIMAL}:
            return cls._number_supported(value, evidence_text)
        return cls._string_supported(str(value), evidence_text)

    @classmethod
    def _value_supported_chunks(
        cls,
        value: Any,
        evidence_items: list[Evidence],
        ranges: list[str],
    ) -> set[int]:
        if not evidence_items or value is None:
            return set()
        if isinstance(value, list):
            if not value:
                return set()
            supported_chunks: set[int] = set()
            for item in value:
                item_chunks = cls._value_supported_chunks(
                    item,
                    evidence_items,
                    ranges,
                )
                if not item_chunks:
                    return set()
                supported_chunks.update(item_chunks)
            return supported_chunks

        individually_supported = {
            evidence.chunk_index
            for evidence in evidence_items
            if cls._value_supported(
                value,
                cls._evidence_support_text(evidence),
                ranges,
            )
        }
        if individually_supported:
            return individually_supported

        combined_text = "\n".join(
            cls._evidence_support_text(evidence) for evidence in evidence_items
        )
        if cls._value_supported(value, combined_text, ranges):
            return {evidence.chunk_index for evidence in evidence_items}
        return set()

    @classmethod
    def _grounding_failure_reason(
        cls,
        value: Any,
        evidence_items: list[Evidence],
        valid_evidence: list[Evidence],
        valid_chunks: set[int],
        ranges: list[str],
    ) -> str:
        if not evidence_items:
            return "NO_EVIDENCE"
        if not valid_evidence:
            invalid_chunks = {item.chunk_index for item in evidence_items} - valid_chunks
            return "EVIDENCE_OUTSIDE_SOURCE" if invalid_chunks else "EVIDENCE_INVALID"
        if isinstance(value, list):
            supported = [
                bool(cls._value_supported_chunks(item, valid_evidence, ranges))
                for item in value
            ]
            if any(supported) and not all(supported):
                return "MULTI_FACT_VALUE_PARTIALLY_SUPPORTED"
        normalized_value = cls._normalize(str(value)) if value is not None else ""
        normalized_evidence = cls._normalize(
            "\n".join(cls._evidence_support_text(item) for item in valid_evidence)
        )
        if normalized_value and normalized_value in normalized_evidence:
            return "NORMALIZATION_MISMATCH"
        value_tokens = set(re.findall(r"\w+", normalized_value))
        evidence_tokens = set(re.findall(r"\w+", normalized_evidence))
        if value_tokens and value_tokens & evidence_tokens:
            return "PARTIAL_MATCH"
        return "VALUE_NOT_FOUND"

    @staticmethod
    def _evidence_support_text(evidence: Evidence) -> str:
        if evidence.section:
            return f"{evidence.section}\n{evidence.text}"
        return evidence.text

    @classmethod
    def _string_supported(cls, value: str, evidence_text: str) -> bool:
        normalized_value = cls._normalize(value)
        normalized_evidence = cls._normalize(evidence_text)
        return bool(normalized_value) and normalized_value in normalized_evidence

    @classmethod
    def _number_supported(cls, value: Any, evidence_text: str) -> bool:
        if not isinstance(value, (int, float, Decimal)) or isinstance(value, bool):
            return False
        normalized_value = cls._normalize_number_value(value)
        if not normalized_value:
            return False
        for token in re.findall(r"[+-]?\d[\d.,]*", evidence_text):
            if re.sub(r"\D", "", token) == normalized_value:
                return True
        return False

    @staticmethod
    def _normalize_number_value(value: float | Decimal) -> str:
        if isinstance(value, int):
            return str(abs(value))
        decimal_value = Decimal(str(value)).normalize()
        text = format(decimal_value, "f")
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return re.sub(r"\D", "", text)

    @classmethod
    def _contains_quote(cls, content: str, quote: str) -> bool:
        normalized_content = cls._normalize_line_endings(content)
        normalized_quote = cls._normalize_line_endings(quote)
        if normalized_quote not in normalized_content:
            return False
        for line in normalized_content.split("\n"):
            if normalized_quote not in line:
                continue
            stripped_line = line.strip()
            stripped_quote = normalized_quote.strip()
            if (
                stripped_line.startswith("|")
                and stripped_line.endswith("|")
                and "|" in stripped_quote
            ):
                return stripped_quote == stripped_line
            return True
        return True

    @staticmethod
    def _normalize_line_endings(value: str) -> str:
        return value.replace("\r\n", "\n").replace("\r", "\n")

    @staticmethod
    def _normalize(value: str) -> str:
        value = unicodedata.normalize("NFKC", value).casefold()
        return re.sub(r"\s+", " ", value).strip()

    @classmethod
    def _date_supported(cls, value: Any, source_text: str) -> bool:
        parsed: date
        if isinstance(value, datetime):
            parsed = value.date()
        elif isinstance(value, date):
            parsed = value
        elif isinstance(value, str):
            try:
                parsed = date.fromisoformat(value[:10])
            except ValueError:
                return False
        else:
            return False
        candidates = {
            parsed.isoformat(),
            parsed.strftime("%d/%m/%Y"),
            parsed.strftime("%d-%m-%Y"),
            parsed.strftime("%d.%m.%Y"),
        }
        normalized_source = cls._normalize(source_text)
        return any(
            cls._normalize(candidate) in normalized_source
            for candidate in candidates
        )

class OntologyValidator:
    """Validate emitted facts separately from persistence completeness."""

    def __init__(self, registry: OntologyRegistry):
        self.registry = registry

    def validate_extraction(
        self,
        patch: CompiledGraphPatch,
    ) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        node_by_temp_id = {node.temp_id: node for node in patch.nodes}

        for node_index, node in enumerate(patch.nodes):
            ontology_class = self.registry.get_class(node.class_name)
            if ontology_class is None:
                issues.append(
                    ValidationIssue(
                        code="UNKNOWN_CLASS",
                        message=f"Unknown ontology class: {node.class_name}",
                        location=f"nodes.{node_index}.className",
                        node_temp_id=node.temp_id,
                    )
                )
                continue

            for property_name, value in node.properties.items():
                attribute = self.registry.get_attribute(property_name)
                location = f"nodes.{node_index}.properties.{property_name}"
                if attribute is None:
                    issues.append(
                        ValidationIssue(
                            code="UNKNOWN_PROPERTY",
                            message=f"Unknown ontology property: {property_name}",
                            location=location,
                            node_temp_id=node.temp_id,
                            property_name=property_name,
                        )
                    )
                    continue
                if ontology_class.name not in attribute.domain:
                    issues.append(
                        ValidationIssue(
                            code="PROPERTY_DOMAIN_MISMATCH",
                            message=(
                                f"Property {property_name} does not belong to "
                                f"class {node.class_name}"
                            ),
                            location=location,
                            node_temp_id=node.temp_id,
                            property_name=property_name,
                        )
                    )
                    continue
                if not self._is_valid_property_value(value, attribute.range):
                    issues.append(
                        ValidationIssue(
                            code="PROPERTY_DATATYPE_MISMATCH",
                            message=(
                                f"Invalid datatype for {property_name}; expected "
                                f"{attribute.range}, got {type(value).__name__}"
                            ),
                            location=location,
                            node_temp_id=node.temp_id,
                            property_name=property_name,
                        )
                    )

        for edge_index, edge in enumerate(patch.edges):
            source = node_by_temp_id.get(edge.source_temp_id)
            target = node_by_temp_id.get(edge.target_temp_id)
            if source is None or target is None:
                issues.append(
                    ValidationIssue(
                        code="DANGLING_REFERENCE",
                        message=f"Edge {edge.edge_name} references an unknown tempId",
                        location=f"edges.{edge_index}",
                        edge_name=edge.edge_name,
                    )
                )
                continue

            ontology_edge = self.registry.get_edge(edge.edge_name)
            if ontology_edge is None:
                issues.append(
                    ValidationIssue(
                        code="UNKNOWN_EDGE",
                        message=f"Unknown ontology edge: {edge.edge_name}",
                        location=f"edges.{edge_index}.edgeName",
                        edge_name=edge.edge_name,
                    )
                )
                continue

            source_class = self.registry.get_class(source.class_name)
            target_class = self.registry.get_class(target.class_name)
            if source_class is not None and source_class.name not in ontology_edge.domain:
                issues.append(
                    ValidationIssue(
                        code="EDGE_DOMAIN_MISMATCH",
                        message=(
                            f"Invalid edge domain for {edge.edge_name}: "
                            f"{source.class_name} is not allowed"
                        ),
                        location=f"edges.{edge_index}.sourceTempId",
                        node_temp_id=source.temp_id,
                        edge_name=edge.edge_name,
                    )
                )
            if target_class is not None and target_class.name not in ontology_edge.range:
                issues.append(
                    ValidationIssue(
                        code="EDGE_RANGE_MISMATCH",
                        message=(
                            f"Invalid edge range for {edge.edge_name}: "
                            f"{target.class_name} is not allowed"
                        ),
                        location=f"edges.{edge_index}.targetTempId",
                        node_temp_id=target.temp_id,
                        edge_name=edge.edge_name,
                    )
                )

            for attribute, expected_value in self.registry.derived_target_properties_for_edge(
                edge.edge_name
            ):
                if target.properties.get(attribute.technical_name) != expected_value:
                    issues.append(
                        ValidationIssue(
                            code="SEMANTIC_CONFLICT",
                            message=(
                                f"Edge {edge.edge_name} requires target "
                                f"{attribute.technical_name}={expected_value}"
                            ),
                            location=f"edges.{edge_index}",
                            node_temp_id=target.temp_id,
                            property_name=attribute.technical_name,
                            edge_name=edge.edge_name,
                        )
                    )

        return deduplicate_issues(issues)

    def validate_persistence(
        self,
        patch: CompiledGraphPatch,
    ) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []

        for node_index, node in enumerate(patch.nodes):
            ontology_class = self.registry.get_class(node.class_name)
            if ontology_class is None:
                continue

            for rule in ontology_class.rules:
                attribute = self.registry.get_attribute(rule.property)
                if attribute is None:
                    continue
                if self.registry.is_runtime_managed_attribute(rule.property):
                    continue
                value = node.properties.get(rule.property)
                message = self._attribute_rule_failure(rule, value)
                if message is not None:
                    issues.append(
                        self._missing_rule_issue(
                            rule,
                            message=message,
                            location=f"nodes.{node_index}.properties.{rule.property}",
                            node_temp_id=node.temp_id,
                            property_name=rule.property,
                        )
                    )

        for node_index, node in enumerate(patch.nodes):
            ontology_class = self.registry.get_class(node.class_name)
            if ontology_class is None:
                continue
            outgoing = [
                edge for edge in patch.edges if edge.source_temp_id == node.temp_id
            ]
            for rule in ontology_class.rules:
                if self.registry.get_edge(rule.property) is None:
                    continue
                count = sum(edge.edge_name == rule.property for edge in outgoing)
                message = self._edge_rule_failure(rule, count)
                if message is not None:
                    issues.append(
                        self._missing_rule_issue(
                            rule,
                            message=message,
                            location=f"nodes.{node_index}.edges.{rule.property}",
                            node_temp_id=node.temp_id,
                            edge_name=rule.property,
                        )
                    )

        return deduplicate_issues(issues)

    def _missing_rule_issue(
        self, rule, *, message: str, location: str, node_temp_id: str | None = None,
        property_name: str | None = None, edge_name: str | None = None,
    ) -> ValidationIssue:
        mode = _missing_rule_mode(self.registry, rule)
        code, suffix = {
            "source": ("MISSING_REQUIRED_SOURCE_FACT", "required source-backed ontology fact is missing"),
            "system": ("DEFAULT_APPLIED", "value is owned by ingestion runtime/default policy"),
            "derived": ("DERIVATION_PENDING", "value may be derived from related facts or edges"),
            "optional": ("OPTIONAL_OMISSION", "ontology requirement is advisory for ingestion"),
        }[mode]
        return ValidationIssue(
            code=code, message=f"{message}; {suffix}", location=location,
            node_temp_id=node_temp_id, property_name=property_name, edge_name=edge_name,
        )

    def _attribute_rule_failure(self, rule, value: Any) -> str | None:
        count = self._value_count(value)
        failure = cardinality_failure(rule.operator, rule.value, count)
        if failure is not None:
            kind, expected_count = failure
            if kind == "exactly":
                return (
                    f"Property {rule.property} must occur exactly {expected_count} "
                    f"time(s); got {count}"
                )
            return (
                f"Property {rule.property} must occur at least {expected_count} "
                f"time(s); got {count}"
            )
        if rule.operator == "some":
            expected = rule.value
            if isinstance(expected, str) and expected.startswith("xsd:"):
                if not self._is_valid_property_value(value, [expected]):
                    return f"Property {rule.property} must satisfy {expected}"
            elif isinstance(expected, str):
                values = value if isinstance(value, list) else [value]
                if expected not in values:
                    return f"Property {rule.property} must contain value {expected}"
        return None

    def _edge_rule_failure(self, rule, count: int) -> str | None:
        failure = cardinality_failure(rule.operator, rule.value, count)
        if failure is None:
            return None
        kind, expected_count = failure
        if kind == "exactly":
            return (
                f"Edge {rule.property} must occur exactly {expected_count} time(s); "
                f"got {count}"
            )
        if rule.operator == "some":
            return f"Edge {rule.property} is required"
        return (
            f"Edge {rule.property} must occur at least {expected_count} time(s); "
            f"got {count}"
        )

    def _is_valid_property_value(self, value: Any, ranges: list[str]) -> bool:
        if isinstance(value, list):
            return bool(value) and all(
                self._is_valid_single_value(item, ranges) for item in value
            )
        return self._is_valid_single_value(value, ranges)

    def _is_valid_single_value(self, value: Any, ranges: list[str]) -> bool:
        if value is None:
            return False
        return any(
            value_matches_xsd(value, datatype)
            for datatype in xsd_datatypes(ranges)
        )

    @staticmethod
    def _value_count(value: Any) -> int:
        if value is None:
            return 0
        return len(value) if isinstance(value, list) else 1


def _missing_rule_mode(registry: OntologyRegistry, rule) -> str:
    failure = cardinality_failure(rule.operator, rule.value, 0)
    if failure is None and rule.operator != "some":
        return "optional"
    attribute = registry.get_attribute(rule.property)
    if attribute is not None:
        policy = attribute.ingestion_policy
        if registry.is_runtime_managed_attribute(rule.property) or policy.mode in {"runtime_managed", "system_default"}:
            return "system"
        if policy.mode == "edge_derived" or registry.edge_names_deriving_property(rule.property):
            return "derived"
        return "source"
    if registry.get_edge(rule.property) is not None:
        return "derived"
    return "optional"


def is_source_required_rule(registry: OntologyRegistry, rule) -> bool:
    return _missing_rule_mode(registry, rule) == "source"

class InvalidGraphPatchFragmentError(ValueError):
    error_kind = "invalid_graph_patch_fragment"
    retryable = True

    def __init__(self, message: str, *, summary: dict[str, Any] | None = None):
        super().__init__(message)
        self.summary = summary or {}


@dataclass(frozen=True)
class GraphPatchAssessment:
    result: GraphPatchValidationResult
    compiled_patch: CompiledGraphPatch | None
    fingerprint: str | None


class GraphValidation:
    def __init__(
        self,
        ontology_path: str | Path = DEFAULT_ONTOLOGY_PATH,
        *,
        compiler_schema_version: str | None = None,
        semantic_grounding_judge: SemanticGroundingJudge | None = None,
    ):
        ontology_path = Path(ontology_path)
        ontology = OntologyLoader.load(ontology_path)
        registry = OntologyRegistry(ontology)
        self.registry = registry
        compiler_kwargs: dict[str, Any] = {"ontology_path": ontology_path}
        if compiler_schema_version is not None:
            compiler_kwargs["schema_version"] = compiler_schema_version
        self.compiler = GraphPatchCompiler(**compiler_kwargs)
        self.validator = OntologyValidator(registry)
        self.source_grounding = SourceGroundingValidator(
            registry,
            semantic_judge=semantic_grounding_judge,
            semantic_value_judge=create_default_semantic_value_judge(),
        )
        self.identity_resolver = create_product_sales_identity_resolver(registry)

    def assess(
        self,
        graph_patch: GraphPatchDraft | dict[str, Any],
        artifact_content_digest: str | None,
        source_chunks: list[DocumentChunk] | list[dict[str, Any]] | None = None,
    ) -> GraphPatchAssessment:
        try:
            draft = GraphPatchDraft.model_validate(graph_patch)
        except ValidationError as exc:
            issues = self._schema_issues(exc)
            return GraphPatchAssessment(
                result=GraphPatchValidationResult(
                    valid_for_extraction=False,
                    valid_for_persistence=False,
                    errors=issues,
                    readiness_issues=[],
                    warnings=[],
                    node_count=self._collection_size(graph_patch, "nodes"),
                    edge_count=self._collection_size(graph_patch, "edges"),
                ),
                compiled_patch=None,
                fingerprint=None,
            )

        chunks = self._source_chunks(source_chunks)
        compiler_result = self.compiler.compile(draft)
        warning_issues = [
            ValidationIssue(
                code="SOURCE_WARNING",
                message=warning,
                location=f"warnings.{index}",
            )
            for index, warning in enumerate(draft.warnings)
        ]
        if compiler_result.compiled_patch is None:
            return GraphPatchAssessment(
                result=GraphPatchValidationResult(
                    valid_for_extraction=False,
                    valid_for_persistence=False,
                    errors=list(compiler_result.errors),
                    readiness_issues=[],
                    warnings=warning_issues,
                    node_count=len(draft.nodes),
                    edge_count=len(draft.edges),
                ),
                compiled_patch=None,
                fingerprint=None,
            )

        patch = compiler_result.compiled_patch
        extraction_issues = self._derived_edge_issues(draft)
        if chunks is not None:
            extraction_issues.extend(self.source_grounding.validate(draft, chunks))
        extraction_issues.extend(self.validator.validate_extraction(patch))

        readiness_issues: list[ValidationIssue] = []
        if not extraction_issues:
            readiness_issues.extend(self.validator.validate_persistence(patch))
            readiness_issues.extend(self._identity_preflight(patch))
            if chunks is None:
                readiness_issues.append(
                    ValidationIssue(
                        code="SOURCE_CONTEXT_REQUIRED",
                        message=(
                            "Prepared source chunks are required before a graph "
                            "patch can be authorized for persistence"
                        ),
                        location="graphPatch",
                    )
                )

        valid_for_extraction = not extraction_issues
        valid_for_persistence = valid_for_extraction and not readiness_issues
        return GraphPatchAssessment(
            result=GraphPatchValidationResult(
                valid_for_extraction=valid_for_extraction,
                valid_for_persistence=valid_for_persistence,
                errors=extraction_issues,
                readiness_issues=readiness_issues,
                warnings=warning_issues,
                node_count=len(patch.nodes),
                edge_count=len(patch.edges),
            ),
            compiled_patch=patch,
            fingerprint=self.compiler.fingerprint(patch, artifact_content_digest),
        )

    @staticmethod
    def _source_chunks(
        source_chunks: list[DocumentChunk] | list[dict[str, Any]] | None,
    ) -> list[DocumentChunk] | None:
        if source_chunks is None:
            return None
        return [
            item if isinstance(item, DocumentChunk) else DocumentChunk.model_validate(item)
            for item in source_chunks
        ]

    def _derived_edge_issues(self, draft: GraphPatchDraft) -> list[ValidationIssue]:
        rule_type_property = "pskg:ruleType"
        deriving_edges = self.registry.edge_names_deriving_property(rule_type_property)
        if not deriving_edges:
            return []
        derived_targets = {
            edge.target_temp_id
            for edge in draft.edges
            if edge.edge_name in deriving_edges
        }
        return [
            ValidationIssue(
                code="DERIVED_PROPERTY_REQUIRES_EDGE_EVIDENCE",
                message=(
                    f"BusinessRule node {node.temp_id} requires an incoming "
                    f"relationship that derives {rule_type_property}"
                ),
                location=f"nodes.{index}.properties.{rule_type_property}",
                node_temp_id=node.temp_id,
                property_name=rule_type_property,
            )
            for index, node in enumerate(draft.nodes)
            if node.class_name == "pskg:BusinessRule"
            and node.temp_id not in derived_targets
        ]

    def _identity_preflight(
        self,
        patch: CompiledGraphPatch,
    ) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        for node_index, node in enumerate(patch.nodes):
            try:
                identity = self.identity_resolver.resolve(
                    class_name=node.class_name,
                    properties=node.properties,
                    source_scope=source_scope_from_evidence(node.evidence),
                )
            except IdentityResolutionError as exc:
                issues.append(
                    ValidationIssue(
                        code="IDENTITY_UNRESOLVED",
                        message=str(exc),
                        location=f"nodes.{node_index}",
                        node_temp_id=node.temp_id,
                    )
                )
                continue
            if identity.strategy == "unresolved":
                issues.append(
                    ValidationIssue(
                        code="IDENTITY_UNRESOLVED",
                        message=identity.reason or "Identity could not be resolved",
                        location=f"nodes.{node_index}",
                        node_temp_id=node.temp_id,
                    )
                )
        return issues

    @staticmethod
    def _schema_issues(exc: ValidationError) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        for error in exc.errors(include_url=False):
            location = ".".join(str(part) for part in error["loc"])
            if error["type"] == "string_pattern_mismatch":
                code = "TECHNICAL_NAME_INVALID"
            elif "evidence" in error["loc"]:
                code = "EVIDENCE_INVALID"
            else:
                code = "SCHEMA_INVALID"
            issues.append(
                ValidationIssue(
                    code=code,
                    message=error["msg"],
                    location=location or "graphPatch",
                )
            )
        return issues

    @staticmethod
    def _collection_size(value: Any, key: str) -> int:
        if isinstance(value, dict) and isinstance(value.get(key), list):
            return len(value[key])
        return 0
