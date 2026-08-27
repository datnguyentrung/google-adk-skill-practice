from app.core.schemas.ingestion.document import DocumentChunk
from app.core.schemas.ingestion.graph_patch import GraphPatchFragment
from app.services.ingestion import semantic_grounding
from app.services.ingestion import use_case as ingestion_use_case
from app.services.ingestion.loader import OntologyLoader
from app.services.ingestion.registry import OntologyRegistry
from app.services.ingestion.semantic_grounding import SemanticGroundingDecision
from app.services.ingestion.source_grounding import SourceGroundingValidator


SOURCE = "test.md"
SECTION = "3. Đặc điểm chính của thẻ"

ROW_LOAI = "| Loại thẻ | Thẻ tín dụng cá nhân |"
ROW_HANG = "| Hạng thẻ | Gold |"
ROW_LIMIT = "| Hạn mức tín dụng | Từ 20 triệu đến 500 triệu VND |"
ROW_PAY = "| Thanh toán tối thiểu | 5% tổng dư nợ sao kê, tối thiểu 200.000 VND |"

TABLE = "\n".join(
    [
        "| Nội dung | Chính sách |",
        "|---|---|",
        ROW_LOAI,
        ROW_HANG,
        ROW_LIMIT,
        ROW_PAY,
    ]
)

# One claim intentionally fails the deterministic token match (normalized
# numbers "20.000.000" vs source "20 triệu") so the judge path is exercised.
CLAIMS = [
    "Loại thẻ: Thẻ tín dụng cá nhân",
    "Hạng thẻ: Gold",
    "Hạn mức tín dụng: Từ 20.000.000 đến 500.000.000 VND",
    "Thanh toán tối thiểu: 5% tổng dư nợ sao kê, tối thiểu 200.000 VND",
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
            ["Hạn mức tín dụng: Từ 20.000.000 đến 500.000.000 VND"],
            [ROW_LIMIT],
        ),
        [_chunk()],
    )

    assert "PROPERTY_VALUE_NOT_GROUNDED" not in _codes(issues)
    assert len(judge.calls) == 1


def test_same_number_different_predicate_fails_with_judge():
    judge = _FixedValueJudge("unsupported")
    evidence = "| Hạn mức rút tiền mặt | Tối đa 100.000 VND |"
    chunk = DocumentChunk(index=6, source=SOURCE, section=SECTION, content=evidence)
    issues = _validator(judge).validate(
        _fragment(["Phí thường niên 100.000 VND"], [evidence]),
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
        _fragment(CLAIMS, ["Loại thẻ: Thẻ tín dụng cá nhân"]),
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


def test_repair_instruction_offers_narrow_evidence_or_ambiguous():
    text = ingestion_use_case._repair_instructions(
        {
            "codes": ["PROPERTY_VALUE_NOT_GROUNDED"],
            "coverageNotEvidencedChunkIndexes": [],
            "evidenceTextNotInSourceLocations": [],
            "schemaErrorLocations": [],
        }
    )
    assert "narrow the value" in text
    assert "AMBIGUOUS" in text
    assert "Do not resubmit the same unsupported value" in text


def test_default_value_judge_wiring(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    assert semantic_grounding.create_default_semantic_value_judge() is not None
    monkeypatch.delenv("GOOGLE_API_KEY")
    assert semantic_grounding.create_default_semantic_value_judge() is None
