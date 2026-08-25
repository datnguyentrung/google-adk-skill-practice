import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

FIXTURE_ROOT = Path("tests/fixtures/ingestion")


def test_minimally_migrated_baseline_only_changes_property_example_shape():
    legacy = (FIXTURE_ROOT / "skills/legacy/SKILL.md").read_text(encoding="utf-8")
    migrated = (
        FIXTURE_ROOT / "skills/minimally_migrated/SKILL.md"
    ).read_text(encoding="utf-8")

    assert migrated == legacy.replace('"properties": {},', '"properties": [],')
    metadata = json.loads(
        (FIXTURE_ROOT / "skills/baseline-metadata.json").read_text(
            encoding="utf-8"
        )
    )
    normalized = legacy.replace("\r\n", "\n").encode("utf-8")
    assert hashlib.sha256(normalized).hexdigest() == metadata[
        "legacy_skill_sha256_lf"
    ]


def test_benchmark_defines_two_layers_three_documents_and_three_runs():
    plan = json.loads(
        (FIXTURE_ROOT / "evals/benchmark-plan.json").read_text(encoding="utf-8")
    )
    evals = json.loads(
        (FIXTURE_ROOT / "evals/evals.json").read_text(encoding="utf-8")
    )

    assert plan["fixed_point"] == "ee714f97e9cf4259c19fd58130e2d17ad2cd1ff3"
    assert plan["runs_per_eval_per_configuration"] == 3
    assert [layer["name"] for layer in plan["layers"]] == [
        "end_to_end_regression",
        "skill_only_ablation",
    ]
    assert len(evals["evals"]) == 3
    assert all(len(item["expectations"]) >= 5 for item in evals["evals"])
    assert all(
        {
            "productCode",
            "effectiveDate",
            "allowedStatuses",
            "requiresEligibility",
            "fillWhenReady",
        }
        <= set(item["oracle"])
        for item in evals["evals"]
    )
    assert (FIXTURE_ROOT / "evals/run_case.py").is_file()
    assert (FIXTURE_ROOT / "evals/run_matrix.py").is_file()


def test_benchmark_recording_adapter_never_needs_neo4j():
    runner_path = FIXTURE_ROOT / "evals/run_case.py"
    spec = importlib.util.spec_from_file_location("ingestion_run_case", runner_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    result = module.RecordingFillService().fill(
        {"nodes": [{"tempId": "node-1"}], "edges": []}
    )

    assert result["status"] == "success"
    assert result["nodeIds"] == {"node-1": "recording-node-0"}
    assert result["receipt"]["verified"] is True

    auto_patch = {
        "nodes": [
            {
                "tempId": "product-1",
                "className": "pskg:BankingProduct",
                "properties": [
                    {"propertyName": "pskg:productCode", "value": "AUTO-FLEX-01"},
                    {
                        "propertyName": "pskg:bankingProductEffectiveFrom",
                        "value": "2026-08-01",
                    },
                    {"propertyName": "pskg:bankingProductStatus", "value": "Active"},
                ],
                "evidence": [
                    {"source": "auto.md", "text": "AUTO-FLEX-01, 01/08/2026"}
                ],
            }
        ],
        "edges": [],
    }
    grading, quality = module.objective_grade(
        {
            "oracle": {
                "productCode": "AUTO-FLEX-01",
                "effectiveDate": "2026-08-01",
                "allowedStatuses": [],
                "requiresEligibility": False,
                "fillWhenReady": False,
            }
        },
        "auto.md",
        [{"name": "validate_graph_patch", "args": {"graph_patch": auto_patch}}],
        [
            {
                "name": "validate_graph_patch",
                "response": {
                    "validForExtraction": True,
                    "validForPersistence": False,
                    "errors": [],
                },
            }
        ],
    )
    status_check = next(item for item in grading if item["text"].startswith("Status"))
    assert status_check["passed"] is False
    assert quality["hallucination_rate"] == 1.0
    assert module.expectations_satisfied(grading) is False


def test_matrix_records_all_runs_before_returning_failure(monkeypatch, tmp_path):
    runner_path = FIXTURE_ROOT / "evals/run_matrix.py"
    spec = importlib.util.spec_from_file_location("ingestion_run_matrix", runner_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    case_calls = 0

    def fake_run(command, **kwargs):
        nonlocal case_calls
        if str(command[1]).endswith("run_case.py"):
            case_calls += 1
            return SimpleNamespace(returncode=1 if case_calls == 1 else 0)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    args = SimpleNamespace(workspace=tmp_path / "workspace", runs=3, model="test")

    failed = module.run_layer(
        "recording",
        [("with_skill", tmp_path, tmp_path / "skill")],
        args,
        tmp_path,
    )

    assert case_calls == 9
    assert failed == 1


def test_flexi_oracle_requires_semantic_anchors_not_only_total_node_count():
    runner_path = FIXTURE_ROOT / "evals/run_case.py"
    spec = importlib.util.spec_from_file_location("ingestion_run_case", runner_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    case = json.loads(
        (FIXTURE_ROOT / "evals/evals.json").read_text(encoding="utf-8")
    )["evals"][0]
    evidence = [{"source": "flexi.md", "text": "grounded"}]
    nodes = [
        {
            "tempId": "product",
            "className": "pskg:BankingProduct",
            "properties": [
                {
                    "propertyName": "pskg:productCode",
                    "value": "CC-FLEXI-001",
                    "evidence": evidence,
                },
                {
                    "propertyName": "pskg:bankingProductEffectiveFrom",
                    "value": "2026-08-01",
                    "evidence": evidence,
                },
            ],
            "evidence": evidence,
        }
    ]
    nodes.extend(
        {
            "tempId": f"generic-{index}",
            "className": "pskg:SalesKnowledge",
            "properties": [],
            "evidence": evidence,
        }
        for index in range(30)
    )
    grading, quality = module.objective_grade(
        case,
        "flexi.md",
        [{"name": "validate_graph_patch", "args": {"graph_patch": {"nodes": nodes, "edges": []}}}],
        [
            {
                "name": "validate_graph_patch",
                "response": {
                    "validForExtraction": True,
                    "validForPersistence": False,
                    "errors": [],
                },
            }
        ],
    )

    semantic = next(
        item for item in grading if "semantic anchors" in item["text"]
    )
    assert semantic["passed"] is False
    assert quality["semantic_anchor_pass_rate"] == 0.0


def test_skill_snapshots_are_outside_production_skill_tree():
    assert (FIXTURE_ROOT / "skills/legacy/SKILL.md").is_file()
    assert (FIXTURE_ROOT / "skills/minimally_migrated/SKILL.md").is_file()
    assert not Path("app/skills/ingestion/skill-snapshot").exists()


def test_regression_assertions_are_grounded_in_the_three_documents():
    documents = {
        path.name: path.read_text(encoding="utf-8")
        for path in Path("docs").glob("*.md")
    }
    flexi = next(text for name, text in documents.items() if "FLEXI REWARDS" in name)
    deposit = next(text for name, text in documents.items() if "TIỀN GỬI ONLINE" in name)
    auto = next(text for name, text in documents.items() if "VAY MUA Ô TÔ" in name)

    assert "CC-FLEXI-001" in flexi and "01/08/2026" in flexi
    assert "TD-ONLINE-001" in deposit and "01/07/2026" in deposit
    assert "Published" in deposit
    assert "AUTO-FLEX-01" in auto and "01/08/2026" in auto
