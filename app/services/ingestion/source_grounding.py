from __future__ import annotations

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
from app.services.ingestion.validation_utils import deduplicate_issues

class SourceGroundingValidator:
    """Validate completeness from grounded property and relationship facts."""

    def __init__(self, registry: OntologyRegistry):
        self.registry = registry

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
                derived_attribute = self.registry.get_attribute(entry.property_name)
                if (
                    derived_attribute is not None
                    and derived_attribute.ingestion_policy.mode == "edge_derived"
                ):
                    if not related_edge_chunks.get(node.temp_id):
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

                attribute = self.registry.get_attribute(entry.property_name)
                if attribute is None:
                    continue
                valid_evidence = [
                    evidence
                    for evidence in entry.evidence
                    if evidence.chunk_index in valid_chunks
                ]
                supported_chunks = self._value_supported_chunks(
                    entry.value,
                    valid_evidence,
                    attribute.range,
                )
                if supported_chunks:
                    fact_chunks.update(supported_chunks)
                    continue
                if valid_evidence:
                    issues.append(
                        ValidationIssue(
                            code="PROPERTY_VALUE_NOT_GROUNDED",
                            message=(
                                f"Property {entry.property_name}={entry.value!r} is "
                                "not supported by the valid evidence excerpts for "
                                "this property"
                            ),
                            location=f"{location}.evidence",
                            node_temp_id=node.temp_id,
                            property_name=entry.property_name,
                        )
                    )

        for chunk_index, item in coverage_by_index.items():
            if chunk_index not in expected_indexes:
                continue
            if item.decision == "MAPPED" and chunk_index not in fact_chunks:
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
            if item.decision == "NOT_RELEVANT" and chunk_index in fact_chunks:
                issues.append(
                    ValidationIssue(
                        code="COVERAGE_CONFLICT",
                        message=(
                            f"Chunk {chunk_index} is marked NOT_RELEVANT but is "
                            "used by a grounded property or edge fact"
                        ),
                        location=f"coverage.{chunk_index}",
                    )
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
        source_supported = target_supported = predicate_bound = False
        grounded_chunks: set[int] = set()
        unrelated: set[int] = set()
        for index, item in enumerate(evidence_items):
            source_hit = self._endpoint_supported(source, item.text)
            target_hit = self._endpoint_supported(target, item.text)
            predicate_hit = self._predicate_supported(edge.edge_name, item.text)
            source_supported = source_supported or source_hit
            target_supported = target_supported or target_hit
            predicate_bound = predicate_bound or (predicate_hit and (source_hit or target_hit))
            if source_hit or target_hit or predicate_hit:
                grounded_chunks.add(item.chunk_index)
            else:
                unrelated.add(index)
        return (
            source_supported and target_supported and predicate_bound,
            grounded_chunks,
            unrelated,
        )

    def _predicate_supported(self, edge_name: str, evidence_text: str) -> bool:
        ontology_edge = self.registry.get_edge(edge_name)
        if ontology_edge is None:
            return False
        cues = list(ontology_edge.grounding_cues)
        cues.extend((ontology_edge.name, ontology_edge.label))
        cues.append(re.sub(r"(?<=[a-z])(?=[A-Z])", " ", ontology_edge.local_name))
        normalized_text = self._normalize(evidence_text)
        return any(
            normalized_cue and normalized_cue in normalized_text
            for normalized_cue in (self._normalize(cue) for cue in cues)
        )

    def _endpoint_supported(self, node, evidence_text: str) -> bool:
        for entry in node.properties:
            attribute = self.registry.get_attribute(entry.property_name)
            if attribute is None or attribute.ingestion_policy.mode != "source":
                continue
            if self._value_supported(entry.value, evidence_text, attribute.range):
                return True
        return False

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
            if cls._value_supported(value, evidence.text, ranges)
        }
        if individually_supported:
            return individually_supported

        combined_text = "\n".join(evidence.text for evidence in evidence_items)
        if cls._value_supported(value, combined_text, ranges):
            return {evidence.chunk_index for evidence in evidence_items}
        return set()

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
    def _normalize_number_value(value: int | float | Decimal) -> str:
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
