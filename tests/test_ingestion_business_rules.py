from app.core.schemas.ingestion.document import DocumentChunk
from app.core.schemas.ingestion.graph_patch import GraphPatchDraft, GraphPatchFragment
from app.services.ingestion import use_case as ingestion_use_case
from app.services.ingestion.graph_patch_compiler import GraphPatchCompiler
from app.services.ingestion.graph_validation import SemanticGroundingDecision
from app.services.ingestion.staged_ingestion import IngestionWorkspaceService
from app.services.ingestion.use_case import IngestionUseCase
from app.services.ingestion.graph_validation import GraphValidation


class FakeToolContext:
    def __init__(self):
        self.state = {}


SOURCE = "test.md"
PRODUCT_ID = "product-cc-flexi-001"
RULE_ID = "rule-income-001"
PRODUCT_CODE = "CC-FLEXI-001"
RULE_CONDITION = "Thu nhập ròng từ 10 triệu VND/tháng trở lên"
EDGE_EVIDENCE = (
    "eligibility rule CC-FLEXI-001 Thu nhập ròng từ 10 triệu VND/tháng trở lên"
)


def _evidence(text: str) -> dict:
    return {
        "source": SOURCE,
        "chunkIndex": 0,
        "section": "Eligibility",
        "text": text,
    }


def _product_node() -> dict:
    return {
        "tempId": PRODUCT_ID,
        "className": "pskg:BankingProduct",
        "properties": [
            {
                "propertyName": "pskg:productCode",
                "value": PRODUCT_CODE,
                "evidence": [_evidence(PRODUCT_CODE)],
            },
            {
                "propertyName": "pskg:bankingProductEffectiveFrom",
                "value": "2026-08-01",
                "evidence": [_evidence("2026-08-01")],
            },
        ],
        "evidence": [_evidence(PRODUCT_CODE)],
        "confidence": 0.98,
    }


def _business_rule_node() -> dict:
    return {
        "tempId": RULE_ID,
        "className": "pskg:BusinessRule",
        "properties": [
            {
                "propertyName": "pskg:businessRuleCondition",
                "value": RULE_CONDITION,
                "evidence": [_evidence(RULE_CONDITION)],
            }
        ],
        "evidence": [_evidence(RULE_CONDITION)],
        "confidence": 0.95,
    }


def _eligibility_edge() -> dict:
    return {
        "edgeName": "pskg:hasEligibilityRule",
        "sourceTempId": PRODUCT_ID,
        "targetTempId": RULE_ID,
        "evidence": [_evidence(EDGE_EVIDENCE)],
        "confidence": 0.9,
    }


def _rule_edge(edge_name: str) -> dict:
    edge = _eligibility_edge()
    edge["edgeName"] = edge_name
    return edge


def _coverage() -> list[dict]:
    return [
        {
            "chunkIndex": 0,
            "decision": "MAPPED",
            "reason": "Contains product eligibility rule",
        }
    ]


def _context() -> FakeToolContext:
    context = FakeToolContext()
    context.state[ingestion_use_case.ARTIFACT_DIGEST_STATE_KEY] = "digest"
    workspace = IngestionWorkspaceService().begin(
        artifact_name=SOURCE,
        provenance=ingestion_use_case._current_provenance(context),
        chunks=[
            DocumentChunk(
                index=0,
                source=SOURCE,
                section="Eligibility",
                content=(
                    f"{PRODUCT_CODE}\n"
                    "2026-08-01\n"
                    f"{RULE_CONDITION}\n"
                    f"{EDGE_EVIDENCE}\n"
                ),
            )
        ],
    )
    ingestion_use_case._store_workspace(context, workspace)
    return context


def test_compiler_derives_rule_type_from_eligibility_edge():
    draft = GraphPatchDraft.model_validate(
        {
            "nodes": [_product_node(), _business_rule_node()],
            "edges": [_eligibility_edge()],
            "coverage": _coverage(),
            "warnings": [],
        }
    )

    compiled = GraphPatchCompiler().compile(draft).compiled_patch

    assert compiled is not None
    rule = next(node for node in compiled.nodes if node.temp_id == RULE_ID)
    assert rule.properties["pskg:ruleType"] == "ELIGIBILITY"


def test_compiler_derives_rule_type_from_all_ontology_rule_edges():
    expected = {
        "pskg:governedByPolicy": "POLICY",
        "pskg:hasEligibilityRule": "ELIGIBILITY",
        "pskg:hasSalesConditionRule": "SALES_CONDITION",
    }

    for edge_name, rule_type in expected.items():
        draft = GraphPatchDraft.model_validate(
            {
                "nodes": [_product_node(), _business_rule_node()],
                "edges": [_rule_edge(edge_name)],
                "coverage": _coverage(),
                "warnings": [],
            }
        )

        compiled = GraphPatchCompiler().compile(draft).compiled_patch

        assert compiled is not None
        rule = next(node for node in compiled.nodes if node.temp_id == RULE_ID)
        assert rule.properties["pskg:ruleType"] == rule_type


def test_edge_grounding_does_not_require_keyword_cues():
    source_text = (
        f"{PRODUCT_CODE}\n"
        "2026-08-01\n"
        "Thanh toán tối thiểu 5% tổng dư nợ sao kê, tối thiểu 200.000 VND.\n"
        f"{RULE_CONDITION}\n"
    )
    draft = GraphPatchDraft.model_validate(
        {
            "nodes": [_product_node(), _business_rule_node()],
            "edges": [
                {
                    "edgeName": "pskg:hasSalesConditionRule",
                    "sourceTempId": PRODUCT_ID,
                    "targetTempId": RULE_ID,
                    "evidence": [
                        _evidence(
                            "Thanh toán tối thiểu 5% tổng dư nợ sao kê, tối thiểu 200.000 VND."
                        )
                    ],
                    "confidence": 0.9,
                }
            ],
            "coverage": _coverage(),
            "warnings": [],
        }
    )
    chunks = [
        DocumentChunk(
            index=0,
            source=SOURCE,
            section="Eligibility",
            content=source_text,
        )
    ]

    assessment = GraphValidation().assess(draft, "digest", chunks)

    assert assessment.result.valid_for_extraction is True
    assert "EDGE_RELATION_NOT_GROUNDED" not in {
        issue.code for issue in assessment.result.errors
    }


def test_edge_grounding_rejects_only_when_semantic_judge_rejects():
    class UnsupportedJudge:
        def judge_edge(self, **kwargs):
            return SemanticGroundingDecision(
                verdict="unsupported",
                reason="The evidence does not state the requested relation.",
            )

    draft = GraphPatchDraft.model_validate(
        {
            "nodes": [_product_node(), _business_rule_node()],
            "edges": [_rule_edge("pskg:hasSalesConditionRule")],
            "coverage": _coverage(),
            "warnings": [],
        }
    )
    chunks = [
        DocumentChunk(
            index=0,
            source=SOURCE,
            section="Eligibility",
            content=(
                f"{PRODUCT_CODE}\n"
                "2026-08-01\n"
                f"{RULE_CONDITION}\n"
                f"{EDGE_EVIDENCE}\n"
            ),
        )
    ]

    assessment = GraphValidation(
        semantic_grounding_judge=UnsupportedJudge()
    ).assess(draft, "digest", chunks)

    assert assessment.result.valid_for_extraction is False
    assert assessment.result.errors[0].code == "EDGE_RELATION_NOT_GROUNDED"


def test_graph_validation_rejects_business_rule_without_rule_type_edge():
    draft = GraphPatchDraft.model_validate({
        "nodes": [_business_rule_node()], "edges": [],
        "coverage": _coverage(), "warnings": [],
    })
    chunks = [DocumentChunk(
        index=0, source=SOURCE, section="Eligibility", content=RULE_CONDITION
    )]

    assessment = GraphValidation().assess(draft, "digest", chunks)

    assert assessment.result.valid_for_extraction is False
    issue = next(i for i in assessment.result.errors if i.code == "DERIVED_PROPERTY_REQUIRES_EDGE_EVIDENCE")
    assert issue.property_name == "pskg:ruleType"


def test_batch_accepts_business_rule_with_deriving_edge():
    context = _context()
    workspace = ingestion_use_case._load_workspace(context)
    assert workspace is not None
    fragment = GraphPatchFragment.model_validate(
        {
            "nodes": [_product_node(), _business_rule_node()],
            "edges": [_eligibility_edge()],
            "coverage": _coverage(),
            "warnings": [],
        }
    )

    response = ingestion_use_case.submit_ingestion_batch(
        workspace.ingestion_id,
        0,
        fragment,
        context,
    )

    assert response["success"] is True
    assert response["workspaceStats"]["candidateEdges"] == 1
    assert response["fragmentStats"]["edges"] == 1


def test_workspace_merges_banking_product_alias_and_rewrites_edges():
    first = GraphPatchFragment.model_validate(
        {
            "nodes": [_product_node()],
            "edges": [],
            "coverage": _coverage(),
            "warnings": [],
        }
    )
    alias_rule = _business_rule_node()
    alias_edge = _eligibility_edge()
    alias_edge["sourceTempId"] = "product-flexi-rewards"
    second = GraphPatchFragment.model_validate(
        {
            "nodes": [
                {
                    "tempId": "product-flexi-rewards",
                    "className": "pskg:BankingProduct",
                    "properties": [],
                    "evidence": [_evidence("Flexi Rewards")],
                    "confidence": 0.8,
                },
                alias_rule,
            ],
            "edges": [alias_edge],
            "coverage": _coverage(),
            "warnings": [],
        }
    )

    merged = IngestionWorkspaceService.merge_fragments([first, second])

    products = [
        node for node in merged.nodes if node.class_name == "pskg:BankingProduct"
    ]
    assert len(products) == 1
    assert merged.edges[0].source_temp_id == PRODUCT_ID


def test_extractor_coerces_evidence_content_key_to_text():
    payload = {
        "nodes": [
            {
                "tempId": PRODUCT_ID,
                "className": "pskg:BankingProduct",
                "properties": [
                    {
                        "propertyName": "pskg:productCode",
                        "value": PRODUCT_CODE,
                        "evidence": [
                            {
                                "source": SOURCE,
                                "chunkIndex": 0,
                                "section": "Eligibility",
                                "text": PRODUCT_CODE,
                            }
                        ],
                    }
                ],
                "evidence": [_evidence(PRODUCT_CODE)],
                "confidence": 0.98,
            }
        ],
        "edges": [],
        "coverage": _coverage(),
        "warnings": [],
    }

    fragment = GraphPatchFragment.model_validate(payload)

    assert fragment.nodes[0].properties[0].evidence[0].text == PRODUCT_CODE
