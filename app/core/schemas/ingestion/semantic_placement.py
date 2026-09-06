from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.schemas.ingestion.graph_patch import Evidence, GraphPatchFragment


class _SemanticPlacementModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        str_strip_whitespace=True,
    )


class AtomicFact(_SemanticPlacementModel):
    fact_id: str = Field(alias="factId", min_length=1)
    subject: str = Field(min_length=1)
    predicate: str = Field(min_length=1)
    object: str = Field(min_length=1)
    fact_shape: Literal[
        "attribute",
        "relationship",
        "entity",
        "rule",
        "document",
        "script",
        "knowledge",
        "unknown",
    ] = Field(default="unknown", alias="factShape")
    source_chunk_index: int = Field(alias="sourceChunkIndex", ge=0)
    evidence: list[Evidence] = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    context: dict[str, Any] = Field(default_factory=dict)


class AtomicFactBatch(_SemanticPlacementModel):
    facts: list[AtomicFact] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class SourceFactCoverageItem(_SemanticPlacementModel):
    chunk_index: int = Field(alias="chunkIndex", ge=0)
    status: Literal[
        "COVERED",
        "NO_RELEVANT_FACT",
        "AMBIGUOUS",
        "OMISSION_SUSPECTED",
        "PARTIAL_OMISSION_SUSPECTED",
    ]
    reason: str = Field(min_length=1)
    extracted_fact_ids: list[str] = Field(default_factory=list, alias="extractedFactIds")
    suspected_missing_claims: list[str] = Field(
        default_factory=list, alias="suspectedMissingClaims"
    )
    evidence_excerpt: str | None = Field(default=None, alias="evidenceExcerpt")


class SourceFactCoverageAudit(_SemanticPlacementModel):
    passed: bool
    items: list[SourceFactCoverageItem]


class CandidateValidity(_SemanticPlacementModel):
    passed: bool
    reason: str = Field(min_length=1)


class RepresentationCandidate(_SemanticPlacementModel):
    candidate_id: str = Field(alias="candidateId", min_length=1)
    fact_ids: list[str] = Field(alias="factIds", min_length=1)
    kind: Literal[
        "property",
        "node",
        "node_edge",
        "existing_node_property",
        "existing_node_edge",
    ]
    fragment: GraphPatchFragment
    validity: CandidateValidity
    retrieval_score: float = Field(alias="retrievalScore", ge=0.0)
    semantic_fit: float = Field(default=0.0, alias="semanticFit", ge=0.0, le=1.0)
    specificity: float = Field(ge=0.0, le=1.0)
    queryability: float = Field(ge=0.0, le=1.0)
    preserves_information: bool = Field(alias="preservesInformation")
    fallback_role: bool = Field(default=False, alias="fallbackRole")
    rationale: str = Field(min_length=1)


class RepresentationDecision(_SemanticPlacementModel):
    fact_id: str = Field(alias="factId", min_length=1)
    selected_candidate_id: str = Field(alias="selectedCandidateId", min_length=1)
    alternative_candidate_ids: list[str] = Field(
        default_factory=list, alias="alternativeCandidateIds"
    )
    semantic_fit: float = Field(alias="semanticFit", ge=0.0, le=1.0)
    specificity: float = Field(ge=0.0, le=1.0)
    reason: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    fallback_used: bool = Field(default=False, alias="fallbackUsed")
    fallback_justification: str | None = Field(
        default=None, alias="fallbackJustification"
    )


class SemanticPlacementIssue(_SemanticPlacementModel):
    code: Literal[
        "MORE_SPECIFIC_REPRESENTATION_AVAILABLE",
        "GENERIC_FALLBACK_WITHOUT_JUSTIFICATION",
        "SEMANTIC_PLACEMENT_LOW_CONFIDENCE",
        "ENTITY_GRANULARITY_LOSS",
        "SELECTED_CANDIDATE_NOT_GENERATED",
        "SELECTED_CANDIDATE_INVALID",
        "FACT_PROVENANCE_LOST",
        "GRAPH_MAPPING_UNSUPPORTED",
    ]
    fact_id: str = Field(alias="factId", min_length=1)
    candidate_id: str | None = Field(default=None, alias="candidateId")
    message: str = Field(min_length=1)


class SemanticPlacementAssessment(_SemanticPlacementModel):
    passed: bool
    issues: list[SemanticPlacementIssue] = Field(default_factory=list)


class RepresentationCompletenessItem(_SemanticPlacementModel):
    fact_id: str = Field(alias="factId", min_length=1)
    status: Literal[
        "REPRESENTED",
        "DUPLICATE",
        "NOT_RELEVANT",
        "UNSUPPORTED_BY_ONTOLOGY",
        "AMBIGUOUS",
        "REJECTED_WITH_REASON",
    ]
    reason: str = Field(min_length=1)


class RepresentationCompletenessAudit(_SemanticPlacementModel):
    passed: bool
    items: list[RepresentationCompletenessItem]


class SemanticPlacementStats(_SemanticPlacementModel):
    total_atomic_facts: int = Field(alias="totalAtomicFacts", ge=0)
    coverage_support_fact_count: int = Field(
        default=0, alias="coverageSupportFactCount", ge=0
    )
    graph_candidate_fact_count: int = Field(
        default=0, alias="graphCandidateFactCount", ge=0
    )
    represented_facts: int = Field(alias="representedFacts", ge=0)
    represented_graph_fact_count: int = Field(
        default=0, alias="representedGraphFactCount", ge=0
    )
    fallback_representation_count: int = Field(alias="fallbackRepresentationCount", ge=0)
    specific_representation_count: int = Field(alias="specificRepresentationCount", ge=0)
    semantic_placement_repair_count: int = Field(
        default=0, alias="semanticPlacementRepairCount", ge=0
    )
    unrepresented_fact_count: int = Field(alias="unrepresentedFactCount", ge=0)
    unrepresented_graph_fact_count: int = Field(
        default=0, alias="unrepresentedGraphFactCount", ge=0
    )
