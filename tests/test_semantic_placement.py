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
    SourceFactCoverageAudit,
)
from app.services.ingestion.document_reader import DocumentReader
from app.services.ingestion.graph_patch_compiler import GraphPatchCompiler
from app.services.ingestion.loader import OntologyLoader
from app.services.ingestion.registry import OntologyRegistry
from app.services.ingestion.semantic_placement import (
    DeterministicRepresentationSelector,
    FactRolePolicy,
    GeminiRepresentationSelector,
    OntologyCandidateGenerator,
    OntologyPlacementPolicy,
    OntologySemanticRetriever,
    PlacementConfig,
    SemanticGraphMapper,
    SemanticPlacementValidator,
    SourceFactCoverageAuditor,
    _all_uncovered_claims,
)
from app.services.ingestion.graph_validation import OntologyValidator


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


def test_coverage_support_facts_do_not_require_representation_decisions():
    fact = _fact_from_claim("auto-0000-001", "Online shopping.")
    fact = fact.model_copy(update={"context": {"role": "coverage_support"}})

    assessment = SemanticPlacementValidator(PlacementConfig()).validate(
        facts=[fact],
        candidates_by_fact={},
        decisions=[],
    )

    assert assessment.passed is True
    assert assessment.issues == []


def test_graph_candidate_without_decision_still_fails_semantic_placement():
    fact = _fact()

    assessment = SemanticPlacementValidator(PlacementConfig()).validate(
        facts=[fact],
        candidates_by_fact={fact.fact_id: []},
        decisions=[],
    )

    assert assessment.passed is False
    assert assessment.issues[0].code == "GRAPH_MAPPING_UNSUPPORTED"


def test_coverage_support_batch_does_not_enter_semantic_selection(tmp_path):
    chunk = _chunk("Explanatory source context.")
    fact = _fact_from_claim("support-0", "Explanatory source context.").model_copy(
        update={"context": {"role": "coverage_support"}}
    )

    class RecordingSelector:
        def __init__(self): self.fact_ids = []
        def select_batch(self, *, facts, candidates_by_fact, previous_error=None):
            self.fact_ids.extend(item.fact_id for item in facts)
            return []

    path, registry = _ontology(tmp_path)
    selector = RecordingSelector()
    mapper = SemanticGraphMapper(
        registry=registry,
        compiler=GraphPatchCompiler(ontology_path=path),
        ontology_validator=OntologyValidator(registry),
        fact_extractor=_StaticAtomicFactExtractor([fact]),
        selector=selector,
    )
    result = mapper.map_batch(
        batch_payload={
            "batchIndex": 0,
            "chunkIndexes": [0],
            "chunks": [chunk.model_dump(by_alias=True, mode="json")],
        },
        ontology_scope="business product eligibility rule",
        chunks=[chunk],
    )

    assert result.source_audit.passed is True
    assert result.placement.passed is True
    assert result.stats.graph_candidate_fact_count == 0
    assert result.stats.coverage_support_fact_count == 1
    assert selector.fact_ids == []


def test_uncovered_claims_are_not_capped_for_targeted_repair():
    claims = [f"claim {index}" for index in range(8)]

    assert len(_all_uncovered_claims(claims, [])) == 8


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
    fact_batch = AtomicFactBatch(facts=[])

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
        self.call_count = 0

    def generate_content(self, **_kwargs):
        self.call_count += 1
        return SimpleNamespace(parsed=self.payloads.pop(0))


class _FakeGeminiClient:
    def __init__(self, payloads):
        self.models = _FakeModels(payloads)


class _StaticAtomicFactExtractor:
    def __init__(self, facts):
        self.facts = facts

    def extract_facts(self, **_kwargs):
        return AtomicFactBatch(
            facts=self.facts,
        )


def test_source_fact_coverage_detects_partial_omission():
    content = "- Minimum age is 20.\n- Monthly income is 10 million.\n- Valid ID is required."
    chunk = _chunk(content)
    facts = [
        _fact_from_claim("fact-age", "Minimum age is 20."),
        _fact_from_claim("fact-income", "Monthly income is 10 million."),
    ]
    batch = AtomicFactBatch(facts=facts)

    audit = SourceFactCoverageAuditor().audit(chunks=[chunk], fact_batch=batch)

    assert audit.passed is False
    assert audit.items[0].status == "PARTIAL_OMISSION_SUSPECTED"
    assert len(audit.items[0].suspected_missing_claims) == 1
    assert "Valid ID is required." in audit.items[0].suspected_missing_claims[0]


def test_source_fact_coverage_passes_when_all_independent_claims_are_covered():
    claims = ["Minimum age is 20.", "Monthly income is 10 million.", "Valid ID is required."]
    chunk = _chunk("\n".join(f"- {claim}" for claim in claims))
    facts = [_fact_from_claim(f"fact-{index}", claim) for index, claim in enumerate(claims)]
    batch = AtomicFactBatch(facts=facts)

    audit = SourceFactCoverageAuditor().audit(chunks=[chunk], fact_batch=batch)

    assert audit.passed is True
    assert audit.items[0].status == "COVERED"


def test_source_fact_coverage_ignores_extractor_self_claim_when_fact_is_missing():
    content = "- Minimum age is 20.\n- Monthly income is 10 million.\n- Valid ID is required."
    chunk = _chunk(content)
    batch = AtomicFactBatch(
        facts=[_fact_from_claim("fact-age", "Minimum age is 20.")],
    )

    audit = SourceFactCoverageAuditor().audit(chunks=[chunk], fact_batch=batch)

    assert audit.passed is False
    assert audit.items[0].status == "PARTIAL_OMISSION_SUSPECTED"
    assert "Valid ID is required." in audit.items[0].suspected_missing_claims[0]


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


def test_gemini_representation_selector_is_batched_and_constrained_for_paraphrases(tmp_path):
    _, generator, retriever = _generator(tmp_path)
    facts = [
        _fact("Applicant qualifies when monthly income exceeds 10 million"),
        _fact("Monthly income above 10 million is required for eligibility"),
    ]
    facts[1] = facts[1].model_copy(update={"fact_id": "fact-2"})
    candidate_sets = {
        fact.fact_id: generator.generate(
            fact=fact, retrieval=retriever.retrieve(fact)
        )
        for fact in facts
    }
    targets = {
        fact.fact_id: _property_candidate(
            candidate_sets[fact.fact_id], "test:eligibilityCondition"
        )
        for fact in facts
    }
    payload = {
        "decisions": [
            {
                "factId": fact.fact_id,
                "selectedCandidateId": targets[fact.fact_id].candidate_id,
                "alternativeCandidateIds": [],
                "semanticFit": 0.95,
                "confidence": 0.94,
                "reason": "Best semantic match",
                "fallbackJustification": None,
            }
            for fact in facts
        ]
    }
    client = _FakeGeminiClient([payload])
    selector = GeminiRepresentationSelector(client=client)
    decisions = selector.select_batch(
        facts=facts, candidates_by_fact=candidate_sets
    )

    assert len(decisions) == 2
    assert client.models.call_count == 1
    selected_properties = []
    for decision in decisions:
        selected = {
            candidate.candidate_id: candidate
            for candidate in candidate_sets[decision.fact_id]
        }[decision.selected_candidate_id]
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
                "decisions": [
                    {
                        "factId": fact.fact_id,
                        "selectedCandidateId": "invented-candidate",
                        "alternativeCandidateIds": [],
                        "semanticFit": 0.99,
                        "confidence": 0.99,
                        "reason": "invalid test response",
                        "fallbackJustification": None,
                    }
                ]
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
    planner = SemanticGraphMapper(
        registry=registry,
        compiler=compiler,
        ontology_validator=validator,
        fact_extractor=_StaticAtomicFactExtractor(facts),
        selector=GeminiRepresentationSelector(
            client=_ExpectedClassSelectorClient(expected_classes)
        ),
        config=PlacementConfig(selector_mode="llm"),
    )
    selected_chunks = []
    for chunk_index in sorted({fact.source_chunk_index for fact in facts}):
        source_chunk = next(chunk for chunk in chunks if chunk.index == chunk_index)
        evidence_texts = [
            fact.evidence[0].text for fact in facts if fact.source_chunk_index == chunk_index
        ]
        selected_chunks.append(
            source_chunk.model_copy(update={"content": "\n".join(evidence_texts)})
        )
    batch_payload = {
        "batchIndex": 0,
        "chunkIndexes": [chunk.index for chunk in selected_chunks],
        "chunks": [chunk.model_dump(by_alias=True, mode="json") for chunk in selected_chunks],
    }

    result = planner.map_batch(
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
        self.call_count = 0

    def generate_content(self, **kwargs):
        self.call_count += 1
        prompt = kwargs["contents"]
        items = json.loads(prompt.split("Items:\n", 1)[1])
        decisions = []
        for item in items:
            fact_id = item["fact"]["factId"]
            expected = self.expected_by_fact[fact_id]
            matching = [
                candidate
                for candidate in item["candidates"]
                if not candidate["fallback"]
                and any(
                    node["className"] == expected for node in candidate["nodes"]
                )
            ]
            target = max(matching, key=lambda candidate: candidate["semanticFit"])
            decisions.append(
                {
                    "factId": fact_id,
                    "selectedCandidateId": target["candidateId"],
                    "alternativeCandidateIds": [],
                    "semanticFit": 0.95,
                    "confidence": 0.95,
                    "reason": "Best constrained semantic match",
                    "fallbackJustification": None,
                }
            )
        return SimpleNamespace(parsed={"decisions": decisions})


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
                "decisions": [
                    {
                        "factId": fact.fact_id,
                        "selectedCandidateId": None,
                        "alternativeCandidateIds": [],
                        "semanticFit": 0.0,
                        "confidence": 0.8,
                        "reason": "No candidate preserves the fact semantics",
                        "fallbackJustification": None,
                    }
                ]
            }
        ])
    )

    decision = selector.select(fact=fact, candidates=candidates)

    assert decision is None


class _CountingAtomicExtractor(_StaticAtomicFactExtractor):
    def __init__(self, facts):
        super().__init__(facts)
        self.calls = 0

    def extract_facts(self, **kwargs):
        self.calls += 1
        return super().extract_facts(**kwargs)


class _CountingDeterministicSelector:
    def __init__(self):
        self.calls = 0
        self.delegate = DeterministicRepresentationSelector()

    def select_batch(self, *, facts, candidates_by_fact, previous_error=None):
        self.calls += 1
        return self.delegate.select_batch(
            facts=facts,
            candidates_by_fact=candidates_by_fact,
            previous_error=previous_error,
        )


def test_semantic_retry_reuses_facts_coverage_and_candidates(tmp_path):
    path, registry = _ontology(tmp_path)
    fact = _fact()
    extractor = _CountingAtomicExtractor([fact])
    selector = _CountingDeterministicSelector()
    planner = SemanticGraphMapper(
        registry=registry,
        compiler=GraphPatchCompiler(ontology_path=path),
        ontology_validator=OntologyValidator(registry),
        fact_extractor=extractor,
        selector=selector,
        config=PlacementConfig(selector_mode="deterministic"),
    )
    chunk = _chunk()
    payload = {
        "batchIndex": 0,
        "chunkIndexes": [0],
        "chunks": [chunk.model_dump(by_alias=True, mode="json")],
    }

    planner.map_batch(
        batch_payload=payload,
        ontology_scope="business product eligibility rule",
        chunks=[chunk],
    )
    retried = planner.map_batch(
        batch_payload=payload,
        ontology_scope="business product eligibility rule",
        chunks=[chunk],
        previous_error={
            "stage": "semantic_placement",
            "errors": [{"location": "semanticPlacement.fact-1"}],
        },
    )

    assert extractor.calls == 1
    assert selector.calls == 2
    assert retried.stats.semantic_placement_repair_count == 1


def test_source_coverage_failure_skips_representation_selection(tmp_path):
    path, registry = _ontology(tmp_path)
    chunk = _chunk("- Minimum age is 20.\n- Valid ID is required.")
    fact = _fact_from_claim("fact-age", "Minimum age is 20.")
    extractor = _CountingAtomicExtractor([fact])
    selector = _CountingDeterministicSelector()
    planner = SemanticGraphMapper(
        registry=registry,
        compiler=GraphPatchCompiler(ontology_path=path),
        ontology_validator=OntologyValidator(registry),
        fact_extractor=extractor,
        selector=selector,
        config=PlacementConfig(selector_mode="deterministic"),
    )
    result = planner.map_batch(
        batch_payload={
            "batchIndex": 0,
            "chunkIndexes": [0],
            "chunks": [chunk.model_dump(by_alias=True, mode="json")],
        },
        ontology_scope="business product eligibility rule",
        chunks=[chunk],
    )

    assert result.source_audit.passed is False
    assert selector.calls == 0


class _SequenceAtomicFactExtractor:
    def __init__(self, batches):
        self.batches = list(batches)
        self.calls = []

    def extract_facts(self, **kwargs):
        self.calls.append(kwargs["batch_payload"])
        return self.batches.pop(0)


def _claim_fact_at(fact_id, claim, chunk_index):
    return AtomicFact(
        factId=fact_id,
        subject="Flexi card",
        predicate="eligibility condition",
        object=claim,
        factShape="rule",
        sourceChunkIndex=chunk_index,
        evidence=[Evidence(source="source.md", chunkIndex=chunk_index, section="Eligibility", text=claim)],
        confidence=0.91,
    )


def test_source_coverage_retry_reextracts_only_failed_chunks(tmp_path):
    chunk0 = _chunk("- Minimum age is 20.")
    chunk1 = DocumentChunk(
        index=1,
        source="source.md",
        section="Eligibility",
        content="- Monthly income is 10 million.\n- Valid ID is required.",
        chunkId="chunk_0001",
        startLine=2,
        endLine=3,
    )
    age = _claim_fact_at("fact-age", "Minimum age is 20.", 0)
    income = _claim_fact_at("fact-income", "Monthly income is 10 million.", 1)
    identity = _claim_fact_at("fact-id", "Valid ID is required.", 1)
    extractor = _SequenceAtomicFactExtractor(
        [
            AtomicFactBatch(facts=[age, income]),
            AtomicFactBatch(facts=[income, identity]),
        ]
    )
    path, registry = _ontology(tmp_path)
    planner = SemanticGraphMapper(
        registry=registry,
        compiler=GraphPatchCompiler(ontology_path=path),
        ontology_validator=OntologyValidator(registry),
        fact_extractor=extractor,
        selector=DeterministicRepresentationSelector(),
        config=PlacementConfig(selector_mode="deterministic"),
    )
    payload = {
        "batchIndex": 0,
        "chunkIndexes": [0, 1],
        "chunks": [
            chunk0.model_dump(by_alias=True, mode="json"),
            chunk1.model_dump(by_alias=True, mode="json"),
        ],
    }
    first = planner.map_batch(
        batch_payload=payload,
        ontology_scope="eligibility business facts",
        chunks=[chunk0, chunk1],
    )
    assert first.source_audit.passed is False
    retry = planner.map_batch(
        batch_payload=payload,
        ontology_scope="eligibility business facts",
        chunks=[chunk0, chunk1],
        previous_error={
            "stage": "source_fact_coverage",
            "coverageFailures": [
                {"chunkIndex": 1, "missingClaims": ["Valid ID is required."]}
            ],
        },
    )

    assert retry.source_audit.passed is True
    assert len(retry.facts.facts) == 3
    assert extractor.calls[0]["chunkIndexes"] == [0, 1]
    assert extractor.calls[1]["chunkIndexes"] == [1]
    assert {fact.source_chunk_index for fact in retry.facts.facts} == {0, 1}
    assert len({fact.fact_id for fact in retry.facts.facts}) == 3
