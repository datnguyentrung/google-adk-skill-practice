from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Protocol

from google import genai
from google.genai import types
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.schemas.ingestion.document import DocumentChunk
from app.core.schemas.ingestion.graph_patch import (
    ChunkCoverage,
    Evidence,
    ExtractedEdge,
    ExtractedNode,
    ExtractedProperty,
    GraphPatchDraft,
    GraphPatchFragment,
)
from app.core.schemas.ingestion.semantic_placement import (
    AtomicFact,
    AtomicFactBatch,
    CandidateValidity,
    RepresentationCandidate,
    RepresentationCompletenessAudit,
    RepresentationCompletenessItem,
    RepresentationDecision,
    SemanticPlacementAssessment,
    SemanticPlacementIssue,
    SemanticPlacementStats,
    SourceFactCoverageAudit,
    SourceFactCoverageItem,
)
from app.core.schemas.ingestion.validation import ValidationIssue
from app.services.ingestion.fragment_grounding_repair import (
    align_coverage_with_grounded_facts,
)
from app.services.ingestion.graph_patch_compiler import GraphPatchCompiler
from app.services.ingestion.ontology_datatypes import value_matches_xsd, xsd_datatypes
from app.services.ingestion.registry import OntologyRegistry
from app.services.ingestion.source_grounding import SourceGroundingValidator
from app.services.ingestion.validator import OntologyValidator

logger = logging.getLogger(__name__)

DEFAULT_ATOMIC_FACT_MODEL = os.getenv(
    "INGESTION_ATOMIC_FACT_MODEL",
    os.getenv("INGESTION_MODEL", "gemini-3.1-flash-lite"),
)


@dataclass(frozen=True)
class PlacementConfig:
    top_k_classes: int = int(os.getenv("INGESTION_TOP_K_CLASSES", "8"))
    top_k_properties: int = int(os.getenv("INGESTION_TOP_K_PROPERTIES", "12"))
    top_k_edges: int = int(os.getenv("INGESTION_TOP_K_EDGES", "12"))
    max_representation_candidates: int = int(
        os.getenv("INGESTION_MAX_REPRESENTATION_CANDIDATES", "24")
    )
    fallback_margin: float = float(os.getenv("INGESTION_FALLBACK_MARGIN", "0.12"))
    minimum_selection_confidence: float = float(
        os.getenv("INGESTION_MIN_SELECTION_CONFIDENCE", "0.35")
    )
    selector_mode: str = os.getenv("INGESTION_SELECTOR_MODE", "llm").strip().lower()


class AtomicFactExtractor(Protocol):
    def extract_facts(
        self,
        *,
        batch_payload: dict[str, Any],
        ontology_scope: str,
        previous_error: dict[str, Any] | None = None,
    ) -> AtomicFactBatch: ...


class RepresentationSelector(Protocol):
    def select(
        self,
        *,
        fact: AtomicFact,
        candidates: list[RepresentationCandidate],
    ) -> RepresentationDecision | None: ...


class SourceFactCoverageJudge(Protocol):
    def find_missing_claims(
        self,
        *,
        chunk: DocumentChunk,
        claims: list[str],
        facts: list[AtomicFact],
        ontology_scope: str,
    ) -> list[str]: ...


@dataclass(frozen=True)
class RetrievedOntology:
    classes: list[tuple[str, float]]
    properties: list[tuple[str, float]]
    edges: list[tuple[str, float]]


@dataclass(frozen=True)
class BatchPlacementResult:
    fragment: GraphPatchFragment
    facts: AtomicFactBatch
    source_audit: SourceFactCoverageAudit
    decisions: list[RepresentationDecision]
    placement: SemanticPlacementAssessment
    completeness: RepresentationCompletenessAudit
    stats: SemanticPlacementStats


class InvalidAtomicFactBatchError(ValueError):
    def __init__(self, message: str, *, summary: dict[str, Any] | None = None):
        super().__init__(message)
        self.summary = summary or {}


class _SelectorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    selected_candidate_id: str | None = Field(default=None, alias="selectedCandidateId")
    alternative_candidate_ids: list[str] = Field(
        default_factory=list, alias="alternativeCandidateIds"
    )
    semantic_fit: float = Field(default=0.0, alias="semanticFit", ge=0.0, le=1.0)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    reason: str = ""
    fallback_justification: str | None = Field(
        default=None, alias="fallbackJustification"
    )


class _CoverageJudgeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    missing_claims: list[str] = Field(default_factory=list, alias="missingClaims")


class GeminiAtomicFactExtractor:
    """Ontology-aware, representation-blind source fact extractor."""

    def __init__(
        self,
        *,
        model: str = DEFAULT_ATOMIC_FACT_MODEL,
        client: genai.Client | None = None,
    ):
        self.model = model
        self.client = client or genai.Client(api_key=os.getenv("GOOGLE_API_KEY"))

    def extract_facts(
        self,
        *,
        batch_payload: dict[str, Any],
        ontology_scope: str,
        previous_error: dict[str, Any] | None = None,
    ) -> AtomicFactBatch:
        prompt = self._prompt(
            batch_payload=batch_payload,
            ontology_scope=ontology_scope,
            previous_error=previous_error,
        )
        logger.info(
            "[ATOMIC_FACT_REQUEST] batch=%s chunk_ids=%s ontology_scope_chars=%s prompt_chars=%s",
            batch_payload.get("batchIndex"),
            batch_payload.get("chunkIndexes"),
            len(ontology_scope),
            len(prompt),
        )
        response = self.client.models.generate_content(
            model=self.model,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_json_schema=AtomicFactBatch.model_json_schema(),
                temperature=0,
            ),
        )
        payload = getattr(response, "parsed", None)
        if payload is None:
            text = getattr(response, "text", None)
            if not text:
                raise InvalidAtomicFactBatchError("Gemini returned no atomic facts")
            payload = self._parse_json_response(text)
        try:
            return AtomicFactBatch.model_validate(payload)
        except ValidationError as exc:
            raise InvalidAtomicFactBatchError(
                "LLM returned invalid AtomicFactBatch",
                summary={"validationErrors": exc.errors(include_url=False)[:5]},
            ) from exc

    @staticmethod
    def _parse_json_response(text: str) -> Any:
        payload = text.strip()
        if payload.startswith("```"):
            lines = payload.splitlines()
            if lines and lines[0].strip().startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            payload = "\n".join(lines).strip()
        try:
            return json.loads(payload)
        except json.JSONDecodeError as exc:
            raise InvalidAtomicFactBatchError(
                f"LLM returned invalid AtomicFactBatch JSON: {exc.msg}",
                summary={"line": exc.lineno, "column": exc.colno},
            ) from exc

    @staticmethod
    def _prompt(
        *,
        batch_payload: dict[str, Any],
        ontology_scope: str,
        previous_error: dict[str, Any] | None = None,
    ) -> str:
        repair = ""
        if previous_error is not None:
            repair = (
                "\nPrevious atomic fact extraction failed. Correct only source "
                "fact coverage and evidence defects.\n"
                f"{json.dumps(previous_error, ensure_ascii=False)}\n"
            )
        return (
            "Extract atomic business facts from source chunks.\n"
            "You are ontology-aware for relevance: use the ontology scope only "
            "to decide whether source content belongs to representable business "
            "knowledge. You are representation-blind for placement: never emit "
            "ontology technical names, class names, property names, edge names, "
            "nodes, edges, GraphPatch, or Neo4j details.\n"
            "Each fact must be independent, source-grounded, and expressed as "
            "subject, predicate, object. Keep factShape coarse and ontology-neutral.\n"
            "Evidence text must be a verbatim excerpt from the cited chunk content, "
            "not merely the section or filename.\n"
            "Coverage must include every chunk in the batch. Mark a chunk "
            "NO_RELEVANT_FACT only when it has no meaningful business fact within "
            "the ontology scope; use AMBIGUOUS or EXTRACTION_FAILED otherwise.\n"
            f"{repair}\n"
            "Ontology scope, definitions only:\n"
            f"{ontology_scope}\n\n"
            "Batch payload:\n"
            f"{json.dumps(batch_payload, ensure_ascii=False)}"
        )


class GeminiSourceFactCoverageJudge:
    """Find ontology-relevant source claims not covered by extracted facts."""

    def __init__(
        self,
        *,
        model: str = DEFAULT_ATOMIC_FACT_MODEL,
        client: genai.Client | None = None,
    ):
        self.model = model
        self.client = client or genai.Client(api_key=os.getenv("GOOGLE_API_KEY"))

    def find_missing_claims(
        self,
        *,
        chunk: DocumentChunk,
        claims: list[str],
        facts: list[AtomicFact],
        ontology_scope: str,
    ) -> list[str]:
        response = self.client.models.generate_content(
            model=self.model,
            contents=self._prompt(
                chunk=chunk,
                claims=claims,
                facts=facts,
                ontology_scope=ontology_scope,
            ),
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_json_schema=_CoverageJudgeResponse.model_json_schema(),
                temperature=0,
            ),
        )
        payload = getattr(response, "parsed", None)
        if payload is None:
            text = getattr(response, "text", None) or "{}"
            payload = GeminiAtomicFactExtractor._parse_json_response(text)
        parsed = _CoverageJudgeResponse.model_validate(payload)
        chunk_text = _normalize_text(chunk.content)
        return _dedupe_text(
            [
                claim
                for claim in parsed.missing_claims
                if _normalize_text(claim) in chunk_text
            ]
        )

    @staticmethod
    def _prompt(
        *,
        chunk: DocumentChunk,
        claims: list[str],
        facts: list[AtomicFact],
        ontology_scope: str,
    ) -> str:
        fact_payload = [
            {
                "factId": fact.fact_id,
                "subject": fact.subject,
                "predicate": fact.predicate,
                "object": fact.object,
                "evidence": [evidence.text for evidence in fact.evidence],
            }
            for fact in facts
        ]
        return (
            "Audit whether extracted atomic facts cover independent business "
            "claims in one source chunk. Use ontology scope only for relevance; "
            "do not choose ontology classes, properties, edges, nodes, GraphPatch, "
            "or Neo4j details. Return short verbatim excerpts from the chunk for "
            "every ontology-relevant independent claim not covered by extractedFacts. "
            "candidateClaims are deterministic hints only; you may identify a missing "
            "claim elsewhere in the chunk. Return [] when coverage is complete.\n\n"
            f"Ontology scope:\n{ontology_scope}\n\n"
            "Chunk:\n"
            f"{json.dumps(chunk.model_dump(mode='json'), ensure_ascii=False)}\n\n"
            "Candidate claims:\n"
            f"{json.dumps(claims, ensure_ascii=False)}\n\n"
            "Extracted facts:\n"
            f"{json.dumps(fact_payload, ensure_ascii=False)}"
        )


class GeminiRepresentationSelector:
    """Constrained semantic selector over deterministic candidate IDs."""

    def __init__(
        self,
        *,
        model: str = DEFAULT_ATOMIC_FACT_MODEL,
        client: genai.Client | None = None,
    ):
        self.model = model
        self.client = client or genai.Client(api_key=os.getenv("GOOGLE_API_KEY"))

    def select(
        self,
        *,
        fact: AtomicFact,
        candidates: list[RepresentationCandidate],
    ) -> RepresentationDecision | None:
        valid = [candidate for candidate in candidates if candidate.validity.passed]
        if not valid:
            return None
        response = self.client.models.generate_content(
            model=self.model,
            contents=self._prompt(fact=fact, candidates=valid),
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_json_schema=_SelectorResponse.model_json_schema(),
                temperature=0,
            ),
        )
        payload = getattr(response, "parsed", None)
        if payload is None:
            text = getattr(response, "text", None) or "{}"
            payload = GeminiAtomicFactExtractor._parse_json_response(text)
        selected = _SelectorResponse.model_validate(payload)
        if selected.selected_candidate_id is None:
            logger.info(
                "[SEMANTIC_SELECTOR_REJECTED] fact_id=%s reason=%s confidence=%s",
                fact.fact_id,
                selected.reason,
                selected.confidence,
            )
            return None
        candidate_by_id = {candidate.candidate_id: candidate for candidate in valid}
        selected_candidate = candidate_by_id.get(selected.selected_candidate_id)
        return RepresentationDecision(
            factId=fact.fact_id,
            selectedCandidateId=selected.selected_candidate_id,
            alternativeCandidateIds=selected.alternative_candidate_ids,
            semanticFit=selected.semantic_fit,
            specificity=selected_candidate.specificity if selected_candidate else 0.0,
            reason=selected.reason or "LLM selected representation candidate",
            confidence=selected.confidence,
            fallbackUsed=selected_candidate.fallback_role
            if selected_candidate
            else False,
            fallbackJustification=selected.fallback_justification,
        )

    @staticmethod
    def _prompt(
        *,
        fact: AtomicFact,
        candidates: list[RepresentationCandidate],
    ) -> str:
        return (
            "Select the best ontology representation candidate for one atomic "
            "fact. You must choose only a candidateId from candidates. Do not "
            "invent ontology classes, properties, edges, nodes, GraphPatch, or "
            "Neo4j details. Prefer specific, queryable representations that "
            "preserve the independent business meaning. Use fallback only if no "
            "non-fallback candidate fits the fact.\n\n"
            "Fact:\n"
            f"{json.dumps(fact.model_dump(by_alias=True, mode='json'), ensure_ascii=False)}\n\n"
            "Candidates:\n"
            f"{json.dumps([_candidate_summary(c) for c in candidates], ensure_ascii=False)}"
        )


class OntologyPlacementPolicy:
    """Runtime placement policy derived from ontology metadata without mutation."""

    _FALLBACK_HINTS = (
        "fallback",
        "generic",
        "general",
        "other",
        "misc",
        "free text",
        "not yet normalized",
        "not normalized",
        "additional",
        "catch-all",
    )

    def __init__(self, registry: OntologyRegistry):
        self.registry = registry

    def is_fallback_property(self, property_name: str) -> bool:
        attribute = self.registry.get_attribute(property_name)
        if attribute is None:
            return False
        definition = _normalize_text(attribute.definition)
        label = _normalize_text(attribute.label)
        name = _normalize_text(attribute.name)
        broad_string = attribute.range == ["xsd:string"] and len(attribute.domain) <= 2
        hinted = any(
            hint in definition or hint in label or hint in name
            for hint in self._FALLBACK_HINTS
        )
        return hinted and broad_string

    def specificity_for_property(self, property_name: str) -> float:
        attribute = self.registry.get_attribute(property_name)
        if attribute is None:
            return 0.0
        if self.is_fallback_property(property_name):
            return 0.2
        domain_penalty = min(0.25, max(0, len(attribute.domain) - 1) * 0.05)
        return max(0.3, 0.85 - domain_penalty)

    def specificity_for_class(self, class_name: str) -> float:
        ontology_class = self.registry.get_class(class_name)
        if ontology_class is None:
            return 0.0
        parents_penalty = min(0.2, len(ontology_class.parents) * 0.03)
        return max(0.35, 0.8 - parents_penalty)

    def specificity_for_edge(self, edge_name: str) -> float:
        edge = self.registry.get_edge(edge_name)
        if edge is None:
            return 0.0
        breadth = len(edge.domain) + len(edge.range)
        return max(0.35, 0.9 - max(0, breadth - 2) * 0.04)


class OntologySemanticRetriever:
    def __init__(self, registry: OntologyRegistry, config: PlacementConfig):
        self.registry = registry
        self.config = config

    def retrieve(self, fact: AtomicFact) -> RetrievedOntology:
        query = _fact_text(fact)
        class_scores = [
            (name, _semantic_score(query, _class_text(cls)))
            for name in self.registry.list_classes()
            if (cls := self.registry.get_class(name)) is not None
        ]
        property_scores = [
            (name, _semantic_score(query, _attribute_text(attr)))
            for name in self.registry.list_attributes()
            if (attr := self.registry.get_attribute(name)) is not None
            and attr.ingestion_policy.mode == "source"
        ]
        edge_scores = [
            (name, _semantic_score(query, _edge_text(edge)))
            for name in self.registry.list_edges()
            if (edge := self.registry.get_edge(name)) is not None
        ]
        return RetrievedOntology(
            classes=_top_k(class_scores, self.config.top_k_classes),
            properties=_top_k(property_scores, self.config.top_k_properties),
            edges=_top_k(edge_scores, self.config.top_k_edges),
        )


class OntologyCandidateGenerator:
    def __init__(
        self,
        *,
        registry: OntologyRegistry,
        placement_policy: OntologyPlacementPolicy,
        ontology_validator: OntologyValidator,
        compiler: GraphPatchCompiler,
        config: PlacementConfig,
    ):
        self.registry = registry
        self.placement_policy = placement_policy
        self.ontology_validator = ontology_validator
        self.compiler = compiler
        self.config = config

    def generate(
        self,
        *,
        fact: AtomicFact,
        retrieval: RetrievedOntology,
        graph_context: str | None = None,
    ) -> list[RepresentationCandidate]:
        candidates: list[RepresentationCandidate] = []
        for property_name, score in retrieval.properties:
            attr = self.registry.get_attribute(property_name)
            if attr is None or not self._value_feasible(fact.object, attr.range):
                continue
            for class_name in self._domain_classes(attr.domain, retrieval.classes):
                candidates.append(
                    self._property_candidate(fact, class_name, property_name, score)
                )
                if len(candidates) >= self.config.max_representation_candidates:
                    return candidates

        for edge_name, score in retrieval.edges:
            edge = self.registry.get_edge(edge_name)
            if edge is None:
                continue
            source_classes = self._named_classes(edge.domain, retrieval.classes)
            target_classes = self._named_classes(edge.range, retrieval.classes)
            if not source_classes or not target_classes:
                continue
            candidates.append(
                self._node_edge_candidate(
                    fact,
                    source_classes[0],
                    target_classes[0],
                    edge_name,
                    score,
                )
            )
            if len(candidates) >= self.config.max_representation_candidates:
                return candidates

        for class_name, score in retrieval.classes:
            candidates.append(self._node_candidate(fact, class_name, score))
            if len(candidates) >= self.config.max_representation_candidates:
                return candidates

        return candidates[: self.config.max_representation_candidates]

    def _property_candidate(
        self,
        fact: AtomicFact,
        class_name: str,
        property_name: str,
        retrieval_score: float,
    ) -> RepresentationCandidate:
        fallback = self.placement_policy.is_fallback_property(property_name)
        node = ExtractedNode(
            tempId=_entity_temp_id("node", fact.subject, class_name),
            className=class_name,
            properties=[
                ExtractedProperty(
                    propertyName=property_name,
                    value=fact.object,
                    evidence=[item.model_copy(deep=True) for item in fact.evidence],
                )
            ],
            evidence=[item.model_copy(deep=True) for item in fact.evidence],
            confidence=fact.confidence,
        )
        fragment = GraphPatchFragment(
            nodes=[node],
            edges=[],
            coverage=[_mapped_coverage(fact)],
            warnings=[],
        )
        validity = self._validate_candidate(fragment, class_name=class_name)
        specificity = self.placement_policy.specificity_for_property(property_name)
        return RepresentationCandidate(
            candidateId=_candidate_id(
                fact.fact_id, "property", class_name, property_name
            ),
            factIds=[fact.fact_id],
            kind="property",
            fragment=fragment,
            validity=validity,
            retrievalScore=retrieval_score,
            semanticFit=min(1.0, retrieval_score),
            specificity=specificity,
            queryability=0.35 if fallback else 0.7,
            preservesInformation=True,
            fallbackRole=fallback,
            rationale=f"Property candidate {class_name}.{property_name}",
        )

    def _node_candidate(
        self,
        fact: AtomicFact,
        class_name: str,
        retrieval_score: float,
    ) -> RepresentationCandidate:
        node = ExtractedNode(
            tempId=_entity_temp_id("node", fact.object or fact.subject, class_name),
            className=class_name,
            properties=[],
            evidence=[item.model_copy(deep=True) for item in fact.evidence],
            confidence=fact.confidence,
        )
        fragment = GraphPatchFragment(
            nodes=[node],
            edges=[],
            coverage=[_mapped_coverage(fact)],
            warnings=[],
        )
        validity = self._validate_candidate(fragment, class_name=class_name)
        if self._missing_required_source_properties(class_name, fragment):
            validity = CandidateValidity(
                passed=False,
                reason="Source lacks required property evidence for this new node",
            )
        specificity = self.placement_policy.specificity_for_class(class_name)
        entity_fact = fact.fact_shape == "entity"
        return RepresentationCandidate(
            candidateId=_candidate_id(fact.fact_id, "node", class_name),
            factIds=[fact.fact_id],
            kind="node",
            fragment=fragment,
            validity=validity,
            retrievalScore=retrieval_score,
            semanticFit=min(
                1.0, retrieval_score if entity_fact else retrieval_score * 0.65
            ),
            specificity=specificity,
            queryability=0.65,
            preservesInformation=entity_fact,
            fallbackRole=False,
            rationale=f"Entity candidate {class_name}",
        )

    def _node_edge_candidate(
        self,
        fact: AtomicFact,
        source_class: str,
        target_class: str,
        edge_name: str,
        retrieval_score: float,
    ) -> RepresentationCandidate:
        source = ExtractedNode(
            tempId=_entity_temp_id("node", fact.subject, source_class),
            className=source_class,
            properties=[],
            evidence=[item.model_copy(deep=True) for item in fact.evidence],
            confidence=fact.confidence,
        )
        target = ExtractedNode(
            tempId=_entity_temp_id("node", fact.object or fact.subject, target_class),
            className=target_class,
            properties=[],
            evidence=[item.model_copy(deep=True) for item in fact.evidence],
            confidence=fact.confidence,
        )
        edge = ExtractedEdge(
            edgeName=edge_name,
            sourceTempId=source.temp_id,
            targetTempId=target.temp_id,
            evidence=[item.model_copy(deep=True) for item in fact.evidence],
            confidence=fact.confidence,
        )
        fragment = GraphPatchFragment(
            nodes=[source, target],
            edges=[edge],
            coverage=[_mapped_coverage(fact)],
            warnings=[],
        )
        validity = self._validate_candidate(fragment, class_name=source_class)
        specificity = max(
            self.placement_policy.specificity_for_edge(edge_name),
            self.placement_policy.specificity_for_class(target_class),
        )
        return RepresentationCandidate(
            candidateId=_candidate_id(
                fact.fact_id, "node_edge", source_class, edge_name, target_class
            ),
            factIds=[fact.fact_id],
            kind="node_edge",
            fragment=fragment,
            validity=validity,
            retrievalScore=retrieval_score,
            semanticFit=min(1.0, retrieval_score),
            specificity=specificity,
            queryability=0.9,
            preservesInformation=True,
            fallbackRole=False,
            rationale=f"Relationship candidate {source_class}-{edge_name}->{target_class}",
        )

    def _validate_candidate(
        self,
        fragment: GraphPatchFragment,
        *,
        class_name: str,
    ) -> CandidateValidity:
        try:
            draft = GraphPatchDraft.model_validate(
                fragment.model_dump(by_alias=True, mode="json")
            )
        except ValidationError as exc:
            return CandidateValidity(passed=False, reason=str(exc))
        compiled = self.compiler.compile(draft)
        if compiled.compiled_patch is None:
            return CandidateValidity(
                passed=False,
                reason="; ".join(issue.message for issue in compiled.errors),
            )
        issues = self.ontology_validator.validate_extraction(compiled.compiled_patch)
        if issues:
            return CandidateValidity(
                passed=False,
                reason="; ".join(issue.message for issue in issues),
            )
        if not self.registry.get_class(class_name):
            return CandidateValidity(passed=False, reason="Unknown class")
        return CandidateValidity(passed=True, reason="Ontology constraints passed")

    def _missing_required_source_properties(
        self,
        class_name: str,
        fragment: GraphPatchFragment,
    ) -> bool:
        ontology_class = self.registry.get_class(class_name)
        if ontology_class is None:
            return True
        present = {
            prop.property_name
            for node in fragment.nodes
            if node.class_name == class_name
            for prop in node.properties
        }
        for rule in ontology_class.rules:
            if self.registry.is_runtime_managed_attribute(rule.property):
                continue
            if self.registry.get_edge(rule.property) is not None:
                continue
            if (
                rule.operator in {"some", "minQualified", "exactlyQualified"}
                and str(rule.value) not in {"0", "0.0"}
                and rule.property not in present
            ):
                return True
        return False

    def _domain_classes(
        self,
        domains: list[str],
        retrieved_classes: list[tuple[str, float]],
    ) -> list[str]:
        retrieved = self._named_classes(domains, retrieved_classes)
        if retrieved:
            return retrieved
        return [
            class_name
            for class_name in self.registry.list_classes()
            if (cls := self.registry.get_class(class_name)) is not None
            and cls.name in domains
        ][: self.config.top_k_classes]

    def _named_classes(
        self,
        names: list[str],
        retrieved_classes: list[tuple[str, float]],
    ) -> list[str]:
        accepted = []
        for class_name, _ in retrieved_classes:
            ontology_class = self.registry.get_class(class_name)
            if ontology_class is not None and ontology_class.name in names:
                accepted.append(class_name)
        return accepted

    @staticmethod
    def _value_feasible(value: str, ranges: list[str]) -> bool:
        datatypes = xsd_datatypes(ranges)
        if not datatypes:
            return False
        if any(value_matches_xsd(value, datatype) for datatype in datatypes):
            return True
        return "xsd:string" in ranges


class DeterministicRepresentationSelector:
    def select(
        self,
        *,
        fact: AtomicFact,
        candidates: list[RepresentationCandidate],
    ) -> RepresentationDecision | None:
        valid = [candidate for candidate in candidates if candidate.validity.passed]
        if not valid:
            return None
        ranked = sorted(
            valid,
            key=lambda item: (
                item.semantic_fit,
                item.specificity,
                item.queryability,
                not item.fallback_role,
            ),
            reverse=True,
        )
        selected = ranked[0]
        alternatives = [item.candidate_id for item in ranked[1:6]]
        return RepresentationDecision(
            factId=fact.fact_id,
            selectedCandidateId=selected.candidate_id,
            alternativeCandidateIds=alternatives,
            semanticFit=selected.semantic_fit,
            specificity=selected.specificity,
            reason=selected.rationale,
            confidence=min(fact.confidence, max(selected.semantic_fit, 0.1)),
            fallbackUsed=selected.fallback_role,
            fallbackJustification=(
                "No higher-ranked non-fallback candidate passed constraints"
                if selected.fallback_role
                else None
            ),
        )


class SemanticPlacementValidator:
    def __init__(self, config: PlacementConfig):
        self.config = config

    def validate(
        self,
        *,
        facts: list[AtomicFact],
        candidates_by_fact: dict[str, list[RepresentationCandidate]],
        decisions: list[RepresentationDecision],
    ) -> SemanticPlacementAssessment:
        issues: list[SemanticPlacementIssue] = []
        decision_by_fact = {decision.fact_id: decision for decision in decisions}
        for fact in facts:
            candidates = candidates_by_fact.get(fact.fact_id, [])
            by_id = {candidate.candidate_id: candidate for candidate in candidates}
            decision = decision_by_fact.get(fact.fact_id)
            if decision is None:
                issues.append(
                    SemanticPlacementIssue(
                        code="FACT_PROVENANCE_LOST",
                        factId=fact.fact_id,
                        message="No representation decision exists for fact",
                    )
                )
                continue
            selected = by_id.get(decision.selected_candidate_id)
            if selected is None:
                issues.append(
                    SemanticPlacementIssue(
                        code="SELECTED_CANDIDATE_NOT_GENERATED",
                        factId=fact.fact_id,
                        candidateId=decision.selected_candidate_id,
                        message="Selected candidate was not generated",
                    )
                )
                continue
            if not selected.validity.passed:
                issues.append(
                    SemanticPlacementIssue(
                        code="SELECTED_CANDIDATE_INVALID",
                        factId=fact.fact_id,
                        candidateId=selected.candidate_id,
                        message=selected.validity.reason,
                    )
                )
            if decision.confidence < self.config.minimum_selection_confidence:
                issues.append(
                    SemanticPlacementIssue(
                        code="SEMANTIC_PLACEMENT_LOW_CONFIDENCE",
                        factId=fact.fact_id,
                        candidateId=selected.candidate_id,
                        message="Selection confidence is below configured threshold",
                    )
                )
            if selected.fallback_role:
                if not decision.fallback_justification:
                    issues.append(
                        SemanticPlacementIssue(
                            code="GENERIC_FALLBACK_WITHOUT_JUSTIFICATION",
                            factId=fact.fact_id,
                            candidateId=selected.candidate_id,
                            message="Fallback candidate selected without justification",
                        )
                    )
                better = [
                    candidate
                    for candidate in candidates
                    if candidate.validity.passed
                    and not candidate.fallback_role
                    and candidate.semantic_fit
                    >= selected.semantic_fit + self.config.fallback_margin
                ]
                if better:
                    issues.append(
                        SemanticPlacementIssue(
                            code="MORE_SPECIFIC_REPRESENTATION_AVAILABLE",
                            factId=fact.fact_id,
                            candidateId=selected.candidate_id,
                            message=(
                                "Fallback selected while a non-fallback candidate "
                                "has stronger semantic fit"
                            ),
                        )
                    )
            evidence_chunks = (
                {
                    item.chunk_index
                    for node in selected.fragment.nodes
                    for item in node.evidence
                }
                | {
                    item.chunk_index
                    for node in selected.fragment.nodes
                    for prop in node.properties
                    for item in prop.evidence
                }
                | {
                    item.chunk_index
                    for edge in selected.fragment.edges
                    for item in edge.evidence
                }
            )
            if fact.source_chunk_index not in evidence_chunks:
                issues.append(
                    SemanticPlacementIssue(
                        code="FACT_PROVENANCE_LOST",
                        factId=fact.fact_id,
                        candidateId=selected.candidate_id,
                        message="Selected representation does not preserve fact evidence",
                    )
                )
        return SemanticPlacementAssessment(passed=not issues, issues=issues)


class SourceFactCoverageAuditor:
    def __init__(
        self,
        *,
        judge: SourceFactCoverageJudge | None = None,
        ontology_scope: str = "",
    ):
        self.judge = judge
        self.ontology_scope = ontology_scope

    def audit(
        self,
        *,
        chunks: list[DocumentChunk],
        fact_batch: AtomicFactBatch,
        ontology_scope: str | None = None,
    ) -> SourceFactCoverageAudit:
        facts_by_chunk: dict[int, list[AtomicFact]] = {}
        for fact in fact_batch.facts:
            facts_by_chunk.setdefault(fact.source_chunk_index, []).append(fact)
        items: list[SourceFactCoverageItem] = []
        scope = ontology_scope if ontology_scope is not None else self.ontology_scope
        for chunk in chunks:
            facts = facts_by_chunk.get(chunk.index, [])
            extracted_fact_ids = [fact.fact_id for fact in facts]
            claims = _source_claims(chunk.content)
            missing_claims = _uncovered_claims(claims, facts)
            if self.judge is not None and chunk.content.strip():
                missing_claims = self.judge.find_missing_claims(
                    chunk=chunk,
                    claims=claims,
                    facts=facts,
                    ontology_scope=scope,
                )
            if facts:
                if missing_claims:
                    items.append(
                        SourceFactCoverageItem(
                            chunkIndex=chunk.index,
                            status="PARTIAL_OMISSION_SUSPECTED",
                            reason=(
                                "Atomic facts cover only part of the independent "
                                "ontology-relevant source claims"
                            ),
                            extractedFactIds=extracted_fact_ids,
                            suspectedMissingClaims=missing_claims,
                            evidenceExcerpt=missing_claims[0],
                        )
                    )
                    continue
                items.append(
                    SourceFactCoverageItem(
                        chunkIndex=chunk.index,
                        status="COVERED",
                        reason="Atomic facts extracted for chunk",
                        extractedFactIds=extracted_fact_ids,
                    )
                )
                continue
            if missing_claims or _looks_business_relevant(chunk.content):
                items.append(
                    SourceFactCoverageItem(
                        chunkIndex=chunk.index,
                        status="OMISSION_SUSPECTED",
                        reason=(
                            "No atomic facts cover independent ontology-relevant "
                            "source claims"
                        ),
                        suspectedMissingClaims=missing_claims or claims[:5],
                        evidenceExcerpt=(missing_claims or claims or [None])[0],
                    )
                )
                continue
            items.append(
                SourceFactCoverageItem(
                    chunkIndex=chunk.index,
                    status="NO_RELEVANT_FACT",
                    reason="No independent source claims requiring atomic facts",
                )
            )
        return SourceFactCoverageAudit(
            passed=all(
                item.status not in {"OMISSION_SUSPECTED", "PARTIAL_OMISSION_SUSPECTED"}
                for item in items
            ),
            items=items,
        )


class RepresentationCompletenessAuditor:
    def audit(
        self,
        *,
        facts: list[AtomicFact],
        decisions: list[RepresentationDecision],
        candidates_by_fact: dict[str, list[RepresentationCandidate]],
    ) -> RepresentationCompletenessAudit:
        decisions_by_fact = {decision.fact_id: decision for decision in decisions}
        items: list[RepresentationCompletenessItem] = []
        for fact in facts:
            decision = decisions_by_fact.get(fact.fact_id)
            if decision is None:
                candidates = candidates_by_fact.get(fact.fact_id, [])
                status = "UNSUPPORTED_BY_ONTOLOGY" if candidates else "AMBIGUOUS"
                items.append(
                    RepresentationCompletenessItem(
                        factId=fact.fact_id,
                        status=status,
                        reason="No valid selected representation",
                    )
                )
                continue
            items.append(
                RepresentationCompletenessItem(
                    factId=fact.fact_id,
                    status="REPRESENTED",
                    reason="Selected representation materialized",
                )
            )
        return RepresentationCompletenessAudit(
            passed=all(item.status == "REPRESENTED" for item in items),
            items=items,
        )


class GraphPatchMaterializer:
    def materialize(
        self,
        *,
        decisions: list[RepresentationDecision],
        candidates_by_fact: dict[str, list[RepresentationCandidate]],
        chunks: list[DocumentChunk],
    ) -> GraphPatchFragment:
        fragments = []
        for decision in decisions:
            candidates = {
                candidate.candidate_id: candidate
                for candidate in candidates_by_fact.get(decision.fact_id, [])
            }
            selected = candidates.get(decision.selected_candidate_id)
            if selected is not None:
                fragments.append(selected.fragment)
        if not fragments:
            return GraphPatchFragment(
                nodes=[],
                edges=[],
                coverage=[
                    ChunkCoverage(
                        chunkIndex=chunk.index,
                        decision="NO_RELEVANT_FACT",
                        reason="No ontology representation selected",
                    )
                    for chunk in chunks
                ],
                warnings=[],
            )
        merged = _merge_fragments_losslessly(fragments, chunks)
        return merged


class SemanticPlacementPlanner:
    def __init__(
        self,
        *,
        registry: OntologyRegistry,
        compiler: GraphPatchCompiler,
        ontology_validator: OntologyValidator,
        source_grounding: SourceGroundingValidator,
        fact_extractor: AtomicFactExtractor | None = None,
        selector: RepresentationSelector | None = None,
        source_coverage_judge: SourceFactCoverageJudge | None = None,
        config: PlacementConfig | None = None,
    ):
        self.registry = registry
        self.compiler = compiler
        self.ontology_validator = ontology_validator
        self.source_grounding = source_grounding
        self.config = config or PlacementConfig()
        self.fact_extractor = fact_extractor or GeminiAtomicFactExtractor()
        self.policy = OntologyPlacementPolicy(registry)
        self.retriever = OntologySemanticRetriever(registry, self.config)
        self.generator = OntologyCandidateGenerator(
            registry=registry,
            placement_policy=self.policy,
            ontology_validator=ontology_validator,
            compiler=compiler,
            config=self.config,
        )
        self.selector = selector or _default_selector(self.config)
        self.source_auditor = SourceFactCoverageAuditor(
            judge=source_coverage_judge or _default_source_coverage_judge(self.config)
        )
        self.placement_validator = SemanticPlacementValidator(self.config)
        self.materializer = GraphPatchMaterializer()
        self.completeness_auditor = RepresentationCompletenessAuditor()

    def plan_batch(
        self,
        *,
        batch_payload: dict[str, Any],
        ontology_scope: str,
        chunks: list[DocumentChunk],
        graph_context: str | None = None,
        previous_error: dict[str, Any] | None = None,
    ) -> BatchPlacementResult:
        facts = self.fact_extractor.extract_facts(
            batch_payload=batch_payload,
            ontology_scope=ontology_scope,
            previous_error=previous_error,
        )
        source_audit = self.source_auditor.audit(
            chunks=chunks,
            fact_batch=facts,
            ontology_scope=ontology_scope,
        )
        candidates_by_fact: dict[str, list[RepresentationCandidate]] = {}
        decisions: list[RepresentationDecision] = []
        for fact in facts.facts:
            retrieval = self.retriever.retrieve(fact)
            candidates = self.generator.generate(
                fact=fact,
                retrieval=retrieval,
                graph_context=graph_context,
            )
            candidates_by_fact[fact.fact_id] = candidates
            decision = self.selector.select(fact=fact, candidates=candidates)
            if decision is not None:
                decisions.append(decision)
            logger.info(
                "[SEMANTIC_PLACEMENT_FACT] fact_id=%s chunk=%s candidates=%s selected=%s",
                fact.fact_id,
                fact.source_chunk_index,
                len(candidates),
                decision.selected_candidate_id if decision else None,
            )
        placement = self.placement_validator.validate(
            facts=facts.facts,
            candidates_by_fact=candidates_by_fact,
            decisions=decisions,
        )
        fragment = self.materializer.materialize(
            decisions=decisions,
            candidates_by_fact=candidates_by_fact,
            chunks=chunks,
        )
        fragment = align_coverage_with_grounded_facts(
            fragment,
            chunks,
            self.source_grounding,
        )
        completeness = self.completeness_auditor.audit(
            facts=facts.facts,
            decisions=decisions,
            candidates_by_fact=candidates_by_fact,
        )
        fallback_count = sum(decision.fallback_used for decision in decisions)
        stats = SemanticPlacementStats(
            totalAtomicFacts=len(facts.facts),
            representedFacts=sum(
                item.status == "REPRESENTED" for item in completeness.items
            ),
            fallbackRepresentationCount=fallback_count,
            specificRepresentationCount=len(decisions) - fallback_count,
            semanticPlacementRepairCount=0,
            unrepresentedFactCount=sum(
                item.status != "REPRESENTED" for item in completeness.items
            ),
        )
        logger.info(
            "[SEMANTIC_PLACEMENT_RESULT] totalAtomicFacts=%s representedFacts=%s "
            "fallbackRepresentationCount=%s specificRepresentationCount=%s "
            "unrepresentedFactCount=%s sourceCoveragePassed=%s placementPassed=%s",
            stats.total_atomic_facts,
            stats.represented_facts,
            stats.fallback_representation_count,
            stats.specific_representation_count,
            stats.unrepresented_fact_count,
            source_audit.passed,
            placement.passed,
        )
        return BatchPlacementResult(
            fragment=fragment,
            facts=facts,
            source_audit=source_audit,
            decisions=decisions,
            placement=placement,
            completeness=completeness,
            stats=stats,
        )


def placement_issues_to_validation(
    issues: list[SemanticPlacementIssue],
) -> list[ValidationIssue]:
    return [
        ValidationIssue(
            code="ORCHESTRATION_FAILED",
            message=f"{issue.code}: {issue.message}",
            location=f"semanticPlacement.{issue.fact_id}",
        )
        for issue in issues
    ]


def _merge_fragments_losslessly(
    fragments: list[GraphPatchFragment],
    chunks: list[DocumentChunk],
) -> GraphPatchFragment:
    nodes_by_id: dict[str, ExtractedNode] = {}
    edges_by_key: dict[tuple[str, str, str], ExtractedEdge] = {}
    warnings: list[str] = []
    mapped_chunks: set[int] = set()
    for fragment in fragments:
        warnings.extend(fragment.warnings)
        for coverage in fragment.coverage:
            if coverage.decision == "MAPPED":
                mapped_chunks.add(coverage.chunk_index)
        for node in fragment.nodes:
            existing = nodes_by_id.get(node.temp_id)
            if existing is None:
                nodes_by_id[node.temp_id] = node.model_copy(deep=True)
                continue
            existing.evidence = _dedupe_evidence([*existing.evidence, *node.evidence])
            existing.confidence = max(existing.confidence, node.confidence)
            props = {prop.property_name: prop for prop in existing.properties}
            for prop in node.properties:
                current = props.get(prop.property_name)
                if current is None:
                    copied = prop.model_copy(deep=True)
                    existing.properties.append(copied)
                    props[prop.property_name] = copied
                    continue
                if _stable_value(current.value) != _stable_value(prop.value):
                    current.value = _merge_values(current.value, prop.value)
                current.evidence = _dedupe_evidence([*current.evidence, *prop.evidence])
        for edge in fragment.edges:
            key = (edge.edge_name, edge.source_temp_id, edge.target_temp_id)
            existing = edges_by_key.get(key)
            if existing is None:
                edges_by_key[key] = edge.model_copy(deep=True)
                continue
            existing.evidence = _dedupe_evidence([*existing.evidence, *edge.evidence])
            existing.confidence = max(existing.confidence, edge.confidence)
    return GraphPatchFragment(
        nodes=list(nodes_by_id.values()),
        edges=list(edges_by_key.values()),
        coverage=[
            ChunkCoverage(
                chunkIndex=chunk.index,
                decision="MAPPED"
                if chunk.index in mapped_chunks
                else "NO_RELEVANT_FACT",
                reason=(
                    "Selected representation cites this chunk"
                    if chunk.index in mapped_chunks
                    else "No selected representation cites this chunk"
                ),
            )
            for chunk in chunks
        ],
        warnings=list(dict.fromkeys(warnings)),
    )


def _mapped_coverage(fact: AtomicFact) -> ChunkCoverage:
    return ChunkCoverage(
        chunkIndex=fact.source_chunk_index,
        decision="MAPPED",
        reason=f"Atomic fact {fact.fact_id} represented",
    )


def _merge_values(left: Any, right: Any) -> list[Any]:
    values = left if isinstance(left, list) else [left]
    incoming = right if isinstance(right, list) else [right]
    seen = {_stable_value(value) for value in values}
    for value in incoming:
        key = _stable_value(value)
        if key not in seen:
            values.append(value)
            seen.add(key)
    return values


def _dedupe_evidence(items: list[Evidence]) -> list[Evidence]:
    unique = {}
    for item in items:
        unique[_stable_value(item.model_dump(by_alias=True, mode="json"))] = item
    return list(unique.values())


def _candidate_id(fact_id: str, *parts: str) -> str:
    source = "\0".join([fact_id, *parts])
    return "cand_" + hashlib.sha256(source.encode("utf-8")).hexdigest()[:16]


def _entity_temp_id(prefix: str, identity_hint: str, class_name: str) -> str:
    digest = hashlib.sha256(
        f"{prefix}\0{_normalize_text(identity_hint)}\0{class_name}".encode()
    ).hexdigest()
    safe = (
        re.sub(r"[^A-Za-z0-9_]+", "_", class_name.split(":")[-1]).strip("_") or prefix
    )
    return f"{prefix}_{safe}_{digest[:10]}"


def _top_k(scores: list[tuple[str, float]], limit: int) -> list[tuple[str, float]]:
    ranked = sorted(scores, key=lambda item: item[1], reverse=True)
    positive = [item for item in ranked if item[1] > 0]
    return (positive or ranked)[: max(1, limit)]


def _semantic_score(query: str, candidate: str) -> float:
    query_tokens = _tokens(query)
    candidate_tokens = _tokens(candidate)
    if not query_tokens or not candidate_tokens:
        return 0.0
    overlap = query_tokens & candidate_tokens
    containment = len(overlap) / len(query_tokens)
    specificity = len(overlap) / len(candidate_tokens)
    return min(1.0, containment * 0.7 + specificity * 0.3)


def _fact_text(fact: AtomicFact) -> str:
    return " ".join(
        [
            fact.subject,
            fact.predicate,
            fact.object,
            fact.fact_shape,
            *(str(value) for value in fact.context.values()),
        ]
    )


def _class_text(item) -> str:
    return " ".join([item.name, item.label, item.definition, " ".join(item.parents)])


def _attribute_text(item) -> str:
    return " ".join(
        [
            item.name,
            item.label,
            item.definition,
            " ".join(item.domain),
            " ".join(item.range),
        ]
    )


def _edge_text(item) -> str:
    return " ".join(
        [
            item.name,
            item.label,
            item.definition,
            " ".join(item.domain),
            " ".join(item.range),
            " ".join(item.grounding_cues),
        ]
    )


def _tokens(value: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[\w]+", _normalize_text(value))
        if len(token) > 1 and not token.isdigit()
    }


def _normalize_text(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value)).casefold()
    return re.sub(r"\s+", " ", text).strip()


def _stable_value(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _default_selector(config: PlacementConfig) -> RepresentationSelector:
    if config.selector_mode == "deterministic" or not os.getenv("GOOGLE_API_KEY"):
        return DeterministicRepresentationSelector()
    return GeminiRepresentationSelector()


def _default_source_coverage_judge(
    config: PlacementConfig,
) -> SourceFactCoverageJudge | None:
    if config.selector_mode == "deterministic" or not os.getenv("GOOGLE_API_KEY"):
        return None
    return GeminiSourceFactCoverageJudge()


def _candidate_summary(candidate: RepresentationCandidate) -> dict[str, Any]:
    return {
        "candidateId": candidate.candidate_id,
        "kind": candidate.kind,
        "fallback": candidate.fallback_role,
        "semanticFit": candidate.semantic_fit,
        "specificity": candidate.specificity,
        "queryability": candidate.queryability,
        "rationale": candidate.rationale,
        "nodes": [
            {
                "tempId": node.temp_id,
                "className": node.class_name,
                "properties": [
                    {
                        "propertyName": prop.property_name,
                        "value": prop.value,
                    }
                    for prop in node.properties
                ],
            }
            for node in candidate.fragment.nodes
        ],
        "edges": [
            {
                "edgeName": edge.edge_name,
                "sourceTempId": edge.source_temp_id,
                "targetTempId": edge.target_temp_id,
            }
            for edge in candidate.fragment.edges
        ],
    }


def _source_claims(text: str) -> list[str]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    table_rows = []
    for line in lines:
        if "|" not in line:
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if not any(cells) or all(set(cell) <= {"-"} for cell in cells if cell):
            continue
        if [cell.casefold() for cell in cells[:2]] in (
            ["fact", "value"],
            ["nội dung", "thông tin"],
        ):
            continue
        table_rows.append(" | ".join(cell for cell in cells if cell))
    if table_rows:
        return _dedupe_text(table_rows)

    bullets = [
        re.sub(r"^\s*(?:[-*]|\d+[\).])\s+", "", line).strip()
        for line in lines
        if re.match(r"^\s*(?:[-*]|\d+[\).])\s+", line)
    ]
    if len(bullets) >= 2:
        return _dedupe_text([item for item in bullets if len(item) >= 4])

    sentences = [
        sentence.strip()
        for sentence in re.split(r"(?<=[.!?。])\s+", " ".join(lines))
        if 20 <= len(sentence.strip()) <= 260
    ]
    if 2 <= len(sentences) <= 8:
        return _dedupe_text(sentences)
    return []


def _uncovered_claims(claims: list[str], facts: list[AtomicFact]) -> list[str]:
    if not claims:
        return []
    fact_texts = [_fact_coverage_text(fact) for fact in facts]
    uncovered = [
        claim
        for claim in claims
        if not any(_claim_covered_by_fact(claim, fact_text) for fact_text in fact_texts)
    ]
    return uncovered[:5]


def _fact_coverage_text(fact: AtomicFact) -> str:
    return " ".join(
        [
            fact.subject,
            fact.predicate,
            fact.object,
            *(evidence.text for evidence in fact.evidence),
        ]
    )


def _claim_covered_by_fact(claim: str, fact_text: str) -> bool:
    claim_norm = _normalize_text(claim)
    fact_norm = _normalize_text(fact_text)
    if not claim_norm or not fact_norm:
        return False
    if claim_norm in fact_norm or fact_norm in claim_norm:
        return True
    claim_tokens = _tokens(claim_norm)
    fact_tokens = _tokens(fact_norm)
    if not claim_tokens or not fact_tokens:
        return False
    overlap = claim_tokens & fact_tokens
    return len(overlap) / len(claim_tokens) >= 0.55


def _dedupe_text(values: list[str]) -> list[str]:
    seen = set()
    deduped = []
    for value in values:
        key = _normalize_text(value)
        if key and key not in seen:
            seen.add(key)
            deduped.append(value)
    return deduped


def _looks_business_relevant(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    lines = [line for line in stripped.splitlines() if line.strip()]
    table_like = sum(1 for line in lines if "|" in line) >= 2
    enumerated = (
        sum(1 for line in lines if re.match(r"^\s*(\d+[\).]|[-*])\s+", line)) >= 2
    )
    key_value = any(":" in line for line in lines)
    numeric = bool(re.search(r"\d", stripped))
    return table_like or enumerated or (key_value and numeric)


__all__ = [
    "AtomicFactExtractor",
    "BatchPlacementResult",
    "DeterministicRepresentationSelector",
    "GeminiAtomicFactExtractor",
    "GeminiRepresentationSelector",
    "GeminiSourceFactCoverageJudge",
    "GraphPatchMaterializer",
    "InvalidAtomicFactBatchError",
    "OntologyCandidateGenerator",
    "OntologyPlacementPolicy",
    "OntologySemanticRetriever",
    "PlacementConfig",
    "RepresentationCompletenessAuditor",
    "RepresentationSelector",
    "SemanticPlacementPlanner",
    "SemanticPlacementValidator",
    "SourceFactCoverageAuditor",
    "SourceFactCoverageJudge",
    "placement_issues_to_validation",
]
