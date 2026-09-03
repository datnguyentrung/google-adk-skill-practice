import json
from pathlib import Path
from types import SimpleNamespace

from app.core.schemas.ingestion.document import DocumentChunk
from app.core.schemas.ingestion.graph_patch import (
    ChunkCoverage,
    Evidence,
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
    RepresentationDecision,
)
from app.services.ingestion.document_reader import DocumentReader
from app.services.ingestion.graph_patch_compiler import GraphPatchCompiler
from app.services.ingestion.loader import OntologyLoader
from app.services.ingestion.registry import OntologyRegistry
from app.services.ingestion.semantic_placement import (
    DeterministicRepresentationSelector,
    GeminiRepresentationSelector,
    GeminiSourceFactCoverageJudge,
    OntologyCandidateGenerator,
    OntologyPlacementPolicy,
    OntologySemanticRetriever,
    PlacementConfig,
    SemanticPlacementPlanner,
    SemanticPlacementValidator,
    SourceFactCoverageAuditor,
)
from app.services.ingestion.source_grounding import SourceGroundingValidator
from app.services.ingestion.validator import OntologyValidator


def _ontology(tmp_path, *, extra_classes=0):
    classes = [
        {
            "name": "business product",
            "technicalName": "test:Product",
            "localName": "Product",
            "iri": "urn:test#Product",
            "label": "business product",
            "definition": "Commercial product that has business facts",
            "parents": [],
            "rules": [],
        },
        {
            "name": "eligibility rule",
            "technicalName": "test:EligibilityRule",
            "localName": "EligibilityRule",
            "iri": "urn:test#EligibilityRule",
            "label": "eligibility rule",
            "definition": "A rule describing who qualifies for a product",
            "parents": [],
            "rules": [],
        },
        {
            "name": "identified artifact",
            "technicalName": "test:IdentifiedArtifact",
            "localName": "IdentifiedArtifact",
            "iri": "urn:test#IdentifiedArtifact",
            "label": "identified artifact",
            "definition": "Artifact with mandatory identity code",
            "parents": [],
            "rules": [
                {
                    "property": "test:artifactCode",
                    "operator": "exactlyQualified",
                    "value": "1",
                    "qualifier": "xsd:string",
                }
            ],
        },
    ]
    for index in range(extra_classes):
        classes.append(
            {
                "name": f"noise class {index}",
                "technicalName": f"test:NoiseClass{index}",
                "localName": f"NoiseClass{index}",
                "iri": f"urn:test#NoiseClass{index}",
                "label": f"noise class {index}",
                "definition": "Unrelated ontology element",
                "parents": [],
                "rules": [],
            }
        )
    attributes = [
        {
            "kind": "property",
            "name": "eligibility condition",
            "technicalName": "test:eligibilityCondition",
            "localName": "eligibilityCondition",
            "iri": "urn:test#eligibilityCondition",
            "label": "eligibility condition",
            "definition": "Specific qualification condition for product eligibility",
            "domain": ["eligibility rule"],
            "range": ["xsd:string"],
        },
        {
            "kind": "property",
            "name": "general attributes",
            "technicalName": "test:generalAttributes",
            "localName": "generalAttributes",
            "iri": "urn:test#generalAttributes",
            "label": "general attributes",
            "definition": "Generic fallback free text for other facts not yet normalized",
            "domain": ["business product"],
            "range": ["xsd:string"],
        },
        {
            "kind": "property",
            "name": "artifact code",
            "technicalName": "test:artifactCode",
            "localName": "artifactCode",
            "iri": "urn:test#artifactCode",
            "label": "artifact code",
            "definition": "Stable artifact identity code",
            "domain": ["identified artifact"],
            "range": ["xsd:string"],
        },
    ]
    edges = [
        {
            "kind": "edge",
            "name": "has eligibility rule",
            "technicalName": "test:hasEligibilityRule",
            "localName": "hasEligibilityRule",
            "iri": "urn:test#hasEligibilityRule",
            "label": "has eligibility rule",
            "definition": "Product is governed by an eligibility rule",
            "domain": ["business product"],
            "range": ["eligibility rule"],
            "groundingCues": ["qualifies", "eligible", "condition"],
        }
    ]
    data = {
        "sourceDir": "test",
        "sourceFiles": ["test"],
        "summary": {
            "sourceFiles": 1,
            "classes": len(classes),
            "edges": len(edges),
            "attributes": len(attributes),
        },
        "classes": classes,
        "edges": edges,
        "attributes": attributes,
    }
    path = tmp_path / "ontology.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    registry = OntologyRegistry(OntologyLoader.load(path))
    return path, registry


def _fact(text="Applicant must have monthly income over 10 million"):
    return AtomicFact(
        factId="fact-1",
        subject="Flexi card",
        predicate="eligibility condition",
        object=text,
        factShape="rule",
        sourceChunkIndex=0,
        evidence=[
            Evidence(
                source="source.md",
                chunkIndex=0,
                section="Eligibility",
                text=text,
            )
        ],
        confidence=0.91,
    )


def _fact_from_claim(fact_id: str, claim: str) -> AtomicFact:
    return AtomicFact(
        factId=fact_id,
        subject="Flexi card",
        predicate="source claim",
        object=claim,
        factShape="attribute",
        sourceChunkIndex=0,
        evidence=[
            Evidence(
                source="source.md",
                chunkIndex=0,
                section="Eligibility",
                text=claim,
            )
        ],
        confidence=0.91,
    )


def _chunk(text="Applicant must have monthly income over 10 million"):
    return DocumentChunk(
        index=0,
        source="source.md",
        section="Eligibility",
        content=text,
        chunkId="chunk_0000",
        startLine=1,
        endLine=1,
    )


def _generator(tmp_path, *, extra_classes=0, config=None):
    path, registry = _ontology(tmp_path, extra_classes=extra_classes)
    compiler = GraphPatchCompiler(ontology_path=path)
    return (
        registry,
        OntologyCandidateGenerator(
            registry=registry,
            placement_policy=OntologyPlacementPolicy(registry),
            ontology_validator=OntologyValidator(registry),
            compiler=compiler,
            config=config or PlacementConfig(),
        ),
        OntologySemanticRetriever(registry, config or PlacementConfig()),
    )


def test_specific_representation_wins_over_fallback(tmp_path):
    _, generator, retriever = _generator(tmp_path)
    fact = _fact()
    candidates = generator.generate(fact=fact, retrieval=retriever.retrieve(fact))
    decision = DeterministicRepresentationSelector().select(
        fact=fact,
        candidates=candidates,
    )

    assert decision is not None
    selected = {
        candidate.candidate_id: candidate for candidate in candidates
    }[decision.selected_candidate_id]
    assert selected.fallback_role is False


def test_fallback_is_legitimate_when_no_specific_candidate_passes(tmp_path):
    _, generator, retriever = _generator(tmp_path)
    fact = _fact("Loose launch note not normalized anywhere")
    candidates = [
        candidate
        for candidate in generator.generate(fact=fact, retrieval=retriever.retrieve(fact))
        if candidate.fallback_role
    ]
    decision = DeterministicRepresentationSelector().select(
        fact=fact,
        candidates=candidates,
    )

    assert decision is not None
    assert decision.fallback_used is True
    assert decision.fallback_justification


def test_semantic_wording_variants_select_same_representation(tmp_path):
    _, generator, retriever = _generator(tmp_path)
    facts = [
        _fact("Applicant qualifies when monthly income exceeds 10 million"),
        _fact("Monthly income must exceed 10 million for applicant eligibility"),
    ]
    signatures = []
    for fact in facts:
        candidates = generator.generate(fact=fact, retrieval=retriever.retrieve(fact))
        decision = DeterministicRepresentationSelector().select(
            fact=fact,
            candidates=candidates,
        )
        assert decision is not None
        selected = {
            candidate.candidate_id: candidate for candidate in candidates
        }[decision.selected_candidate_id]
        signatures.append(
            (
                selected.kind,
                tuple(node.class_name for node in selected.fragment.nodes),
                tuple(edge.edge_name for edge in selected.fragment.edges),
                tuple(
                    prop.property_name
                    for node in selected.fragment.nodes
                    for prop in node.properties
                ),
            )
        )

    assert signatures[0] == signatures[1]


def test_architecture_runs_on_non_flexi_test_ontology(tmp_path):
    _, generator, retriever = _generator(tmp_path)
    fact = _fact("Applicant must satisfy a qualification condition")
    candidates = generator.generate(fact=fact, retrieval=retriever.retrieve(fact))

    assert candidates
    assert all(
        node.class_name.startswith("test:")
        for candidate in candidates
        for node in candidate.fragment.nodes
    )


def test_source_fact_coverage_audit_fails_when_extractor_omits_business_fact():
    chunks = [
        _chunk("| Fact | Value |\n| Product code | CARD-1 |\n| Income | 10 million |")
    ]
    fact_batch = AtomicFactBatch(facts=[], coverage={0: "NO_RELEVANT_FACT"})

    audit = SourceFactCoverageAuditor().audit(chunks=chunks, fact_batch=fact_batch)

    assert audit.passed is False
    assert audit.items[0].status == "OMISSION_SUSPECTED"


def test_candidate_generation_is_bounded_for_large_ontology(tmp_path):
    config = PlacementConfig(
        top_k_classes=3,
        top_k_properties=2,
        top_k_edges=2,
        max_representation_candidates=4,
    )
    _, generator, retriever = _generator(tmp_path, extra_classes=80, config=config)
    fact = _fact()
    retrieval = retriever.retrieve(fact)
    candidates = generator.generate(fact=fact, retrieval=retrieval)

    assert len(retrieval.classes) <= 3
    assert len(retrieval.properties) <= 2
    assert len(retrieval.edges) <= 2
    assert len(candidates) <= 4
    assert any(
        node.class_name == "test:EligibilityRule"
        for candidate in candidates
        for node in candidate.fragment.nodes
    )


def test_required_property_candidate_is_not_valid_without_source_identity(tmp_path):
    _, generator, _ = _generator(tmp_path)
    fact = _fact("Artifact exists")
    candidate = generator._node_candidate(fact, "test:IdentifiedArtifact", 0.9)

    assert candidate.validity.passed is False
    assert "required property" in candidate.validity.reason


def test_selector_and_validator_are_independent_for_fallback_rejection():
    fact = _fact()
    evidence = fact.evidence
    fallback = RepresentationCandidate(
        candidateId="fallback",
        factIds=[fact.fact_id],
        kind="property",
        fragment=GraphPatchFragment(
            nodes=[
                ExtractedNode(
                    tempId="product",
                    className="test:Product",
                    properties=[
                        ExtractedProperty(
                            propertyName="test:generalAttributes",
                            value=fact.object,
                            evidence=evidence,
                        )
                    ],
                    evidence=evidence,
                    confidence=0.9,
                )
            ],
            edges=[],
            coverage=[ChunkCoverage(chunkIndex=0, decision="MAPPED", reason="mapped")],
            warnings=[],
        ),
        validity=CandidateValidity(passed=True, reason="valid"),
        retrievalScore=0.5,
        semanticFit=0.5,
        specificity=0.2,
        queryability=0.2,
        preservesInformation=True,
        fallbackRole=True,
        rationale="fallback",
    )
    specific = fallback.model_copy(
        update={
            "candidate_id": "specific",
            "fallback_role": False,
            "semantic_fit": 0.8,
            "specificity": 0.8,
            "queryability": 0.8,
        }
    )
    decision = RepresentationDecision(
        factId=fact.fact_id,
        selectedCandidateId="fallback",
        alternativeCandidateIds=["specific"],
        semanticFit=0.5,
        specificity=0.2,
        reason="selector chose fallback",
        confidence=0.8,
        fallbackUsed=True,
        fallbackJustification="selector says ok",
    )

    assessment = SemanticPlacementValidator(PlacementConfig()).validate(
        facts=[fact],
        candidates_by_fact={fact.fact_id: [fallback, specific]},
        decisions=[decision],
    )

    assert assessment.passed is False
    assert assessment.issues[0].code == "MORE_SPECIFIC_REPRESENTATION_AVAILABLE"


class _FakeModels:
    def __init__(self, payloads):
        self.payloads = list(payloads)

    def generate_content(self, **_kwargs):
        return SimpleNamespace(parsed=self.payloads.pop(0))


class _FakeGeminiClient:
    def __init__(self, payloads):
        self.models = _FakeModels(payloads)


class _NoMissingClaimsJudge:
    def find_missing_claims(self, **_kwargs):
        return []


class _StaticAtomicFactExtractor:
    def __init__(self, facts):
        self.facts = facts

    def extract_facts(self, **_kwargs):
        return AtomicFactBatch(
            facts=self.facts,
            coverage={fact.source_chunk_index: "FACTS_EXTRACTED" for fact in self.facts},
        )


def test_source_fact_coverage_detects_partial_omission():
    content = "- Minimum age is 20.\n- Monthly income is 10 million.\n- Valid ID is required."
    chunk = _chunk(content)
    facts = [
        _fact_from_claim("fact-age", "Minimum age is 20."),
        _fact_from_claim("fact-income", "Monthly income is 10 million."),
    ]
    batch = AtomicFactBatch(facts=facts, coverage={0: "FACTS_EXTRACTED"})

    audit = SourceFactCoverageAuditor().audit(chunks=[chunk], fact_batch=batch)

    assert audit.passed is False
    assert audit.items[0].status == "PARTIAL_OMISSION_SUSPECTED"
    assert audit.items[0].suspected_missing_claims == ["Valid ID is required."]


def test_source_fact_coverage_passes_when_all_independent_claims_are_covered():
    claims = ["Minimum age is 20.", "Monthly income is 10 million.", "Valid ID is required."]
    chunk = _chunk("\n".join(f"- {claim}" for claim in claims))
    facts = [_fact_from_claim(f"fact-{index}", claim) for index, claim in enumerate(claims)]
    batch = AtomicFactBatch(facts=facts, coverage={0: "FACTS_EXTRACTED"})

    audit = SourceFactCoverageAuditor().audit(chunks=[chunk], fact_batch=batch)

    assert audit.passed is True
    assert audit.items[0].status == "COVERED"


def test_source_fact_coverage_ignores_extractor_self_claim_when_fact_is_missing():
    content = "- Minimum age is 20.\n- Monthly income is 10 million.\n- Valid ID is required."
    chunk = _chunk(content)
    batch = AtomicFactBatch(
        facts=[_fact_from_claim("fact-age", "Minimum age is 20.")],
        coverage={0: "FACTS_EXTRACTED"},
    )

    audit = SourceFactCoverageAuditor().audit(chunks=[chunk], fact_batch=batch)

    assert audit.passed is False
    assert audit.items[0].status == "PARTIAL_OMISSION_SUSPECTED"
    assert "Valid ID is required." in audit.items[0].suspected_missing_claims


def test_production_coverage_judge_can_find_claim_outside_deterministic_hints():
    chunk = _chunk("First independent fact. Second independent fact.")
    client = _FakeGeminiClient([{"missingClaims": ["Second independent fact."]}])
    judge = GeminiSourceFactCoverageJudge(client=client)

    missing = judge.find_missing_claims(
        chunk=chunk,
        claims=[],
        facts=[_fact_from_claim("fact-first", "First independent fact.")],
        ontology_scope="business facts",
    )

    assert missing == ["Second independent fact."]


def _property_candidate(candidates, property_name):
    return next(
        candidate
        for candidate in candidates
        if any(
            prop.property_name == property_name
            for node in candidate.fragment.nodes
            for prop in node.properties
        )
    )


def test_gemini_representation_selector_is_constrained_for_paraphrases(tmp_path):
    _, generator, retriever = _generator(tmp_path)
    facts = [
        _fact("Applicant qualifies when monthly income exceeds 10 million"),
        _fact("Monthly income above 10 million is required for eligibility"),
    ]
    candidate_sets = [
        generator.generate(fact=fact, retrieval=retriever.retrieve(fact)) for fact in facts
    ]
    targets = [
        _property_candidate(candidates, "test:eligibilityCondition")
        for candidates in candidate_sets
    ]
    payloads = [
        {
            "selectedCandidateId": target.candidate_id,
            "alternativeCandidateIds": [],
            "semanticFit": 0.95,
            "confidence": 0.94,
            "reason": "Best semantic match",
            "fallbackJustification": None,
        }
        for target in targets
    ]
    selector = GeminiRepresentationSelector(client=_FakeGeminiClient(payloads))
    decisions = [
        selector.select(fact=fact, candidates=candidates)
        for fact, candidates in zip(facts, candidate_sets, strict=True)
    ]

    assert all(decision is not None for decision in decisions)
    selected_properties = []
    for decision, candidates in zip(decisions, candidate_sets, strict=True):
        assert decision is not None
        selected = {candidate.candidate_id: candidate for candidate in candidates}[
            decision.selected_candidate_id
        ]
        selected_properties.append(
            tuple(
                prop.property_name
                for node in selected.fragment.nodes
                for prop in node.properties
            )
        )
    assert selected_properties == [
        ("test:eligibilityCondition",),
        ("test:eligibilityCondition",),
    ]


def test_gemini_selector_unknown_candidate_is_rejected_by_placement_validator(tmp_path):
    _, generator, retriever = _generator(tmp_path)
    fact = _fact()
    candidates = generator.generate(fact=fact, retrieval=retriever.retrieve(fact))
    selector = GeminiRepresentationSelector(
        client=_FakeGeminiClient([
            {
                "selectedCandidateId": "invented-candidate",
                "alternativeCandidateIds": [],
                "semanticFit": 0.99,
                "confidence": 0.99,
                "reason": "invalid test response",
                "fallbackJustification": None,
            }
        ])
    )
    decision = selector.select(fact=fact, candidates=candidates)
    assert decision is not None

    assessment = SemanticPlacementValidator(PlacementConfig()).validate(
        facts=[fact],
        candidates_by_fact={fact.fact_id: candidates},
        decisions=[decision],
    )

    assert assessment.passed is False
    assert assessment.issues[0].code == "SELECTED_CANDIDATE_NOT_GENERATED"


def _flexi_fact(fact_id, chunk, *, predicate, object_text, fact_shape, evidence_text):
    return AtomicFact(
        factId=fact_id,
        subject="Flexi Rewards",
        predicate=predicate,
        object=object_text,
        factShape=fact_shape,
        sourceChunkIndex=chunk.index,
        evidence=[
            Evidence(
                source=chunk.source,
                chunkIndex=chunk.index,
                section=chunk.section,
                text=evidence_text,
            )
        ],
        confidence=0.95,
    )


def test_flexi_semantic_regression_runs_source_to_graph_patch_draft():
    source_path = next(Path("docs").glob("*FLEXI REWARDS.md"))
    ontology_path = Path(
        "app/data/ontology/product_sales_knowledge_graph_base_v3_1.ontology.json"
    )
    chunks = DocumentReader().read(source_path)

    def chunk_for(excerpt):
        return next(chunk for chunk in chunks if excerpt in chunk.content)

    samples = [
        (
            "segment",
            "Mua hàng trên nền tảng thương mại điện tử.",
            "targets customer segment",
            "relationship",
        ),
        (
            "need",
            "Nhu cầu mua sắm online.",
            "satisfies customer need",
            "relationship",
        ),
        (
            "rule",
            "Từ 20 tuổi tại thời điểm đăng ký.",
            "has eligibility rule",
            "rule",
        ),
        (
            "document",
            "Căn cước công dân hoặc giấy tờ nhận dạng hợp lệ.",
            "requires document",
            "document",
        ),
        (
            "script",
            "Chào chị Lan. Em được biết chị đang tìm hiểu một phương thức thanh toán thuận tiện hơn khi mua hàng online.",
            "has sales script",
            "script",
        ),
        (
            "knowledge",
            "Ngân hàng cấp cho khách hàng một hạn mức tín dụng.",
            "has sales knowledge",
            "knowledge",
        ),
    ]
    facts = [
        _flexi_fact(
            fact_id,
            chunk_for(excerpt),
            predicate=predicate,
            object_text=excerpt,
            fact_shape=fact_shape,
            evidence_text=excerpt,
        )
        for fact_id, excerpt, predicate, fact_shape in samples
    ]
    expected_classes = {
        "segment": "pskg:CustomerSegment",
        "need": "pskg:CustomerNeed",
        "rule": "pskg:BusinessRule",
        "document": "pskg:RequiredDocument",
        "script": "pskg:SalesScript",
        "knowledge": "pskg:SalesKnowledge",
    }
    registry = OntologyRegistry(OntologyLoader.load(ontology_path))
    compiler = GraphPatchCompiler(ontology_path=ontology_path)
    validator = OntologyValidator(registry)
    planner = SemanticPlacementPlanner(
        registry=registry,
        compiler=compiler,
        ontology_validator=validator,
        source_grounding=SourceGroundingValidator(registry),
        fact_extractor=_StaticAtomicFactExtractor(facts),
        selector=GeminiRepresentationSelector(
            client=_ExpectedClassSelectorClient(expected_classes)
        ),
        source_coverage_judge=_NoMissingClaimsJudge(),
        config=PlacementConfig(selector_mode="llm"),
    )
    selected_chunks = list({fact.source_chunk_index: chunk_for(fact.evidence[0].text) for fact in facts}.values())
    batch_payload = {
        "batchIndex": 0,
        "chunkIndexes": [chunk.index for chunk in selected_chunks],
        "chunks": [chunk.model_dump(by_alias=True, mode="json") for chunk in selected_chunks],
    }

    result = planner.plan_batch(
        batch_payload=batch_payload,
        ontology_scope="product customer segment customer need business rule required document sales script sales knowledge",
        chunks=selected_chunks,
    )
    draft = GraphPatchDraft.model_validate(result.fragment.model_dump(by_alias=True, mode="json"))

    assert draft.nodes
    assert result.source_audit.passed is True
    assert result.placement.passed is True
    assert result.completeness.passed is True
    assert result.stats.total_atomic_facts == len(facts)
    assert result.stats.represented_facts == len(facts)
    assert result.stats.unrepresented_fact_count == 0
    assert result.stats.fallback_representation_count == 0
    assert result.stats.specific_representation_count == len(facts)
    assert result.stats.semantic_placement_repair_count == 0
    assert result.placement.issues == []

    decisions = {decision.fact_id: decision for decision in result.decisions}
    for fact in facts:
        candidates = planner.generator.generate(
            fact=fact,
            retrieval=planner.retriever.retrieve(fact),
        )
        selected = {candidate.candidate_id: candidate for candidate in candidates}[
            decisions[fact.fact_id].selected_candidate_id
        ]
        assert selected.fallback_role is False
        assert any(
            node.class_name == expected_classes[fact.fact_id]
            for node in selected.fragment.nodes
        )


class _ExpectedClassSelectorModels:
    def __init__(self, expected_by_fact):
        self.expected_by_fact = expected_by_fact

    def generate_content(self, **kwargs):
        prompt = kwargs["contents"]
        fact_id = next(
            fact_id
            for fact_id in self.expected_by_fact
            if f'"factId": "{fact_id}"' in prompt
        )
        candidates = json.loads(prompt.split("Candidates:\n", 1)[1])
        expected = self.expected_by_fact[fact_id]
        matching = [
            candidate
            for candidate in candidates
            if not candidate["fallback"]
            and any(node["className"] == expected for node in candidate["nodes"])
        ]
        target = max(matching, key=lambda candidate: candidate["semanticFit"])
        return SimpleNamespace(
            parsed={
                "selectedCandidateId": target["candidateId"],
                "alternativeCandidateIds": [],
                "semanticFit": 0.95,
                "confidence": 0.95,
                "reason": "Best constrained semantic match",
                "fallbackJustification": None,
            }
        )


class _ExpectedClassSelectorClient:
    def __init__(self, expected_by_fact):
        self.models = _ExpectedClassSelectorModels(expected_by_fact)


def test_gemini_selector_can_reject_without_inventing_candidate(tmp_path):
    _, generator, retriever = _generator(tmp_path)
    fact = _fact()
    candidates = generator.generate(fact=fact, retrieval=retriever.retrieve(fact))
    selector = GeminiRepresentationSelector(
        client=_FakeGeminiClient([
            {
                "selectedCandidateId": None,
                "alternativeCandidateIds": [],
                "semanticFit": 0.0,
                "confidence": 0.8,
                "reason": "No candidate preserves the fact semantics",
                "fallbackJustification": None,
            }
        ])
    )

    decision = selector.select(fact=fact, candidates=candidates)

    assert decision is None
