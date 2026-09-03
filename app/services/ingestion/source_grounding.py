from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Iterable
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from app.core.schemas.ingestion.document import DocumentChunk
from app.core.schemas.ingestion.graph_patch import Evidence, GraphPatchDraft
from app.core.schemas.ingestion.validation import ValidationIssue
from app.services.ingestion.ontology_datatypes import XsdDatatype, xsd_datatypes
from app.services.ingestion.registry import OntologyRegistry
from app.services.ingestion.semantic_grounding import (
    PermissiveSemanticGroundingJudge,
    SemanticGroundingJudge,
    SemanticValueJudge,
)
from app.services.ingestion.validation_utils import deduplicate_issues

logger = logging.getLogger(__name__)

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
        return decision.verdict != "unsupported", {
            item.chunk_index for item in evidence_items
        }, set()

    def _predicate_supported(self, edge_name: str, evidence_text: str) -> bool:
        return True

    def _endpoint_supported(self, node, evidence_text: str) -> bool:
        for entry in node.properties:
            attribute = self.registry.get_attribute(entry.property_name)
            if attribute is None or attribute.ingestion_policy.mode != "source":
                continue
            if self._value_supported(entry.value, evidence_text, attribute.range):
                return True
        return False

    def _property_grounding(
        self,
        property_name: str,
        attribute,
    ) -> str:
        """Classify property grounding semantics from ontology policy."""

        policy = attribute.ingestion_policy
        if (
            policy.mode in {"runtime_managed", "system_default", "edge_derived"}
            or self.registry.is_runtime_managed_attribute(property_name)
            or bool(self.registry.edge_names_deriving_property(property_name))
        ):
            return "derived"
        if policy.grounding == "source_normalized":
            return "normalized"
        if self._is_literal_by_contract(property_name, attribute.range):
            return "literal"
        return "normalized"

    @classmethod
    def _is_literal_by_contract(
        cls,
        property_name: str,
        ranges: list[str],
    ) -> bool:
        datatypes = set(xsd_datatypes(ranges))
        if datatypes & {
            XsdDatatype.BOOLEAN,
            XsdDatatype.DATE,
            XsdDatatype.DATETIME,
            XsdDatatype.DECIMAL,
            XsdDatatype.INTEGER,
        }:
            return True
        lowered = property_name.casefold()
        literal_tokens = (
            "code",
            "date",
            "effective",
            "name",
            "status",
            "version",
        )
        return any(token in lowered for token in literal_tokens)

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
            if not self._contains_quote(chunk.content, evidence.text):
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
        if normalized_value in normalized_evidence:
            return True
        value_tokens = [
            token for token in re.findall(r"\w+", normalized_value)
            if token not in {"là"}
        ]
        evidence_tokens = re.findall(r"\w+", normalized_evidence)
        if not value_tokens:
            return False
        cursor = 0
        for token in value_tokens:
            while cursor < len(evidence_tokens) and evidence_tokens[cursor] != token:
                cursor += 1
            if cursor >= len(evidence_tokens):
                return False
            cursor += 1
        return True

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


__all__ = ["SourceGroundingValidator"]
