from app.core.schemas.ingestion.document import DocumentChunk
from app.core.schemas.ingestion.graph_patch import GraphPatchFragment
from app.services.ingestion import graph_validation
from app.services.ingestion import use_case as ingestion_use_case
from app.services.ingestion.loader import OntologyLoader
from app.services.ingestion.registry import OntologyRegistry
from app.services.ingestion.graph_validation import SemanticGroundingDecision
from app.services.ingestion.graph_validation import SourceGroundingValidator


SOURCE = "test.md"
SECTION = "3. Äáº·c Ä‘iá»ƒm chÃ­nh cá»§a tháº»"

ROW_LOAI = "| Loáº¡i tháº» | Tháº» tÃ­n dá»¥ng cÃ¡ nhÃ¢n |"
ROW_HANG = "| Háº¡ng tháº» | Gold |"
ROW_LIMIT = "| Háº¡n má»©c tÃ­n dá»¥ng | Tá»« 20 triá»‡u Ä‘áº¿n 500 triá»‡u VND |"
ROW_PAY = "| Thanh toÃ¡n tá»‘i thiá»ƒu | 5% tá»•ng dÆ° ná»£ sao kÃª, tá»‘i thiá»ƒu 200.000 VND |"

TABLE = "\n".join(
    [
        "| Ná»™i dung | ChÃ­nh sÃ¡ch |",
        "|---|---|",
        ROW_LOAI,
        ROW_HANG,
        ROW_LIMIT,
        ROW_PAY,
    ]
)

# One claim intentionally fails the deterministic token match (normalized
# numbers "20.000.000" vs source "20 triá»‡u") so the judge path is exercised.
CLAIMS = [
    "Loáº¡i tháº»: Tháº» tÃ­n dá»¥ng cÃ¡ nhÃ¢n",
    "Háº¡ng tháº»: Gold",
    "Háº¡n má»©c tÃ­n dá»¥ng: Tá»« 20.000.000 Ä‘áº¿n 500.000.000 VND",
    "Thanh toÃ¡n tá»‘i thiá»ƒu: 5% tá»•ng dÆ° ná»£ sao kÃª, tá»‘i thiá»ƒu 200.000 VND",
]


def _validator(semantic_value_judge=None) -> SourceGroundingValidator:
    registry = OntologyRegistry(
        OntologyLoader.load(
            "app/data/ontology/product_sales_knowledge_graph_base_v3_1.ontology.json"
        )
    )
    return SourceGroundingValidator(
        registry,
        semantic_value_judge=semantic_value_judge,
    )


def _chunk() -> DocumentChunk:
    return DocumentChunk(index=6, source=SOURCE, section=SECTION, content=TABLE)


def _ev(text: str) -> dict:
    return {
        "source": SOURCE,
        "chunkIndex": 6,
        "section": SECTION,
        "text": text,
    }


def _fragment(value, evidence_texts) -> GraphPatchFragment:
    return GraphPatchFragment(
        nodes=[
            {
                "tempId": "product-1",
                "className": "pskg:BankingProduct",
                "properties": [
                    {
                        "propertyName": "pskg:productAttributes",
                        "value": value,
                        "evidence": [_ev(text) for text in evidence_texts],
                    }
                ],
                "evidence": [_ev(evidence_texts[0])],
                "confidence": 0.9,
            }
        ],
        edges=[],
        coverage=[{"chunkIndex": 6, "decision": "MAPPED", "reason": "Attributes"}],
        warnings=[],
    )


def _codes(issues) -> set[str]:
    return {issue.code for issue in issues}


class _FixedValueJudge:
    def __init__(self, verdict: str, *, error: bool = False):
        self.verdict = verdict
        self.error = error
        self.calls = []

    def judge_value(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise RuntimeError("judge unavailable")
        return SemanticGroundingDecision(verdict=self.verdict, reason="fake judge")


def test_four_claims_four_evidence_pass_when_judge_confirms():
    judge = _FixedValueJudge("supported")
    issues = _validator(judge).validate(
        _fragment(CLAIMS, [ROW_LOAI, ROW_HANG, ROW_LIMIT, ROW_PAY]),
        [_chunk()],
    )

    assert "PROPERTY_VALUE_NOT_GROUNDED" not in _codes(issues)
    assert len(judge.calls) == 1
    assert len(judge.calls[0]["evidence_items"]) == 4
    assert len(judge.calls[0]["value"]) == 4


def test_four_claims_two_evidence_fail_when_judge_unsupported():
    judge = _FixedValueJudge("unsupported")
    issues = _validator(judge).validate(
        _fragment(CLAIMS, [ROW_LOAI, ROW_HANG]),
        [_chunk()],
    )

    assert "PROPERTY_VALUE_NOT_GROUNDED" in _codes(issues)
    assert len(judge.calls) == 1
    assert len(judge.calls[0]["evidence_items"]) == 2


def test_four_claims_two_evidence_fail_when_judge_absent():
    issues = _validator().validate(
        _fragment(CLAIMS, [ROW_LOAI, ROW_HANG]),
        [_chunk()],
    )

    assert "PROPERTY_VALUE_NOT_GROUNDED" in _codes(issues)


def test_normalized_number_wording_different_from_source_passes_with_judge():
    judge = _FixedValueJudge("supported")
    issues = _validator(judge).validate(
        _fragment(
            ["Háº¡n má»©c tÃ­n dá»¥ng: Tá»« 20.000.000 Ä‘áº¿n 500.000.000 VND"],
            [ROW_LIMIT],
        ),
        [_chunk()],
    )

    assert "PROPERTY_VALUE_NOT_GROUNDED" not in _codes(issues)
    assert len(judge.calls) == 1


def test_same_number_different_predicate_fails_with_judge():
    judge = _FixedValueJudge("unsupported")
    evidence = "| Háº¡n má»©c rÃºt tiá»n máº·t | Tá»‘i Ä‘a 100.000 VND |"
    chunk = DocumentChunk(index=6, source=SOURCE, section=SECTION, content=evidence)
    issues = _validator(judge).validate(
        _fragment(["PhÃ­ thÆ°á»ng niÃªn 100.000 VND"], [evidence]),
        [chunk],
    )

    assert "PROPERTY_VALUE_NOT_GROUNDED" in _codes(issues)


def test_judge_error_fails_closed():
    judge = _FixedValueJudge("supported", error=True)
    issues = _validator(judge).validate(
        _fragment(CLAIMS, [ROW_LOAI, ROW_HANG, ROW_LIMIT, ROW_PAY]),
        [_chunk()],
    )

    assert "PROPERTY_VALUE_NOT_GROUNDED" in _codes(issues)


def test_judge_unknown_fails_closed():
    issues = _validator(_FixedValueJudge("unknown")).validate(
        _fragment(CLAIMS, [ROW_LOAI, ROW_HANG, ROW_LIMIT, ROW_PAY]),
        [_chunk()],
    )

    assert "PROPERTY_VALUE_NOT_GROUNDED" in _codes(issues)


def test_evidence_verbatim_still_enforced_with_judge():
    judge = _FixedValueJudge("supported")
    issues = _validator(judge).validate(
        _fragment(CLAIMS, ["Loáº¡i tháº»: Tháº» tÃ­n dá»¥ng cÃ¡ nhÃ¢n"]),
        [_chunk()],
    )

    assert "EVIDENCE_TEXT_NOT_IN_SOURCE" in _codes(issues)
    assert "PROPERTY_VALUE_NOT_GROUNDED" not in _codes(issues)
    assert judge.calls == []


def test_deterministic_pass_does_not_need_judge():
    judge = _FixedValueJudge("supported")
    issues = _validator(judge).validate(
        _fragment(CLAIMS[:1], [ROW_LOAI]),
        [_chunk()],
    )

    assert "PROPERTY_VALUE_NOT_GROUNDED" not in _codes(issues)
    assert judge.calls == []


def test_default_value_judge_wiring(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    assert graph_validation.create_default_semantic_value_judge() is not None
    monkeypatch.delenv("GOOGLE_API_KEY")
    assert graph_validation.create_default_semantic_value_judge() is None

