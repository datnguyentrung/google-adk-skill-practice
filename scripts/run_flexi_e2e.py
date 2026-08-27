import asyncio
import json
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

# Load .env values into the process environment without overriding real env vars.
ENV_FILE = REPO_ROOT / ".env"
if ENV_FILE.exists():
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)

ARTIFACT_NAME = "HƯỚNG DẪN NGHIỆP VỤ SẢN PHẨM THẺ TÍN DỤNG FLEXI REWARDS.md"
SOURCE_FILE = REPO_ROOT / "docs" / ARTIFACT_NAME
OUT_DIR = REPO_ROOT / ".e2e_run_out"
OUT_DIR.mkdir(exist_ok=True)

captured = {
    "finalized": None,
    "reconcile": None,
    "contexts": [],
    "batches": {},
}


class RuntimeAdapter:
    def __init__(self):
        self.state = {}

    async def load_artifact(self, filename: str):
        if filename != ARTIFACT_NAME:
            return None
        return SimpleNamespace(
            inline_data=None,
            text=SOURCE_FILE.read_text(encoding="utf-8"),
        )

    async def save_artifact(self, filename: str, artifact, **kwargs):
        text = getattr(artifact, "text", None)
        if text is None:
            text = json.dumps(artifact, ensure_ascii=False, default=str)
        (OUT_DIR / filename).write_text(text, encoding="utf-8")
        return 1


def install_capture_wrappers() -> None:
    import app.services.ingestion.orchestrator as orch
    import app.services.ingestion.relationship_reconciliation as rr
    import app.services.ingestion.use_case as uc

    orig_extract = orch.GeminiBatchExtractor.extract_fragment

    def extract_wrapper(self, *, batch_payload, **kwargs):
        fragment = orig_extract(self, batch_payload=batch_payload, **kwargs)
        batch_index = batch_payload.get("batchIndex")
        captured["batches"].setdefault(batch_index, []).append(
            {
                "stage": "raw",
                "graph_context": kwargs.get("graph_context"),
                "previous_error": kwargs.get("previous_error"),
                "fragment": fragment.model_dump(by_alias=True, mode="json"),
            }
        )
        return fragment

    orch.GeminiBatchExtractor.extract_fragment = extract_wrapper

    orig_finalize = uc.finalize_ingestion

    def finalize_wrapper(ingestion_id, tool_context):
        response = orig_finalize(ingestion_id, tool_context)
        captured["finalized"] = response
        return response

    uc.finalize_ingestion = finalize_wrapper

    orig_reconcile = rr.RelationshipReconciler.reconcile

    def reconcile_wrapper(self, **kwargs):
        outcome = orig_reconcile(self, **kwargs)
        assessment = outcome.assessment
        captured["reconcile"] = {
            "reconciled": outcome.reconciled,
            "passes_used": outcome.passes_used,
            "exhausted": outcome.exhausted,
            "issues": [issue.code.value for issue in outcome.issues],
            "assessmentReadiness": (
                [issue.code.value for issue in assessment.result.readiness_issues]
                if assessment is not None
                else []
            ),
            "assessmentValidForPersistence": (
                assessment.result.valid_for_persistence
                if assessment is not None
                else None
            ),
            "assessmentNodeCount": (
                assessment.result.node_count if assessment is not None else None
            ),
            "assessmentEdgeCount": (
                assessment.result.edge_count if assessment is not None else None
            ),
        }
        return outcome

    rr.RelationshipReconciler.reconcile = reconcile_wrapper

    orig_build = rr.RelationshipReconciler._build_context

    def build_wrapper(self, **kwargs):
        context = orig_build(self, **kwargs)
        captured["contexts"].append(context)
        return context

    rr.RelationshipReconciler._build_context = build_wrapper


def selected_chunk_indexes(context: str) -> list[int]:
    return sorted({int(match) for match in re.findall(r"\[CHUNK (\d+)\]", context)})


def main() -> None:
    install_capture_wrappers()

    from app.config.neo4j import Neo4jClient
    from app.services.ingestion.use_case import IngestionUseCase

    if not os.getenv("GOOGLE_API_KEY"):
        raise SystemExit("GOOGLE_API_KEY is not set")
    try:
        Neo4jClient.connect().verify_connectivity()
        print("NEO4J_CONNECTIVITY=ok")
    except Exception as exc:  # noqa: BLE001
        print(f"NEO4J_CONNECTIVITY=failed {exc!r}")
        raise

    adapter = RuntimeAdapter()
    result = asyncio.run(
        IngestionUseCase().ingest_end_to_end(
            ARTIFACT_NAME,
            adapter,
            persist=True,
        )
    )

    workspace_stats = result.get("workspaceStats", {})
    reconcile = captured["reconcile"]
    reconciled_assessment = bool(reconcile and reconcile.get("reconciled"))
    last_context = captured["contexts"][-1] if captured["contexts"] else None
    selected_chunks = (
        selected_chunk_indexes(last_context) if last_context is not None else []
    )
    if reconcile is None:
        readiness = (captured["finalized"] or {}).get("readinessIssues", [])
        compiled_nodes = (captured["finalized"] or {}).get("nodeCount")
        compiled_edges = (captured["finalized"] or {}).get("edgeCount")
        valid_for_persistence = (captured["finalized"] or {}).get(
            "validForPersistence"
        )
    else:
        readiness = reconcile.get("assessmentReadiness", [])
        compiled_nodes = reconcile.get("assessmentNodeCount")
        compiled_edges = reconcile.get("assessmentEdgeCount")
        valid_for_persistence = reconcile.get("assessmentValidForPersistence")

    report = {
        "success": result.get("success"),
        "stage": result.get("stage"),
        "terminal": result.get("terminal"),
        "ingestionId": result.get("ingestionId"),
        "failureReason": result.get("failureReason"),
        "batchIndex": result.get("batchIndex"),
        "processedBatchesAtFailure": result.get("processedBatches"),
        "processedBatches": workspace_stats.get("processedBatches"),
        "totalBatches": workspace_stats.get("batches"),
        "documentChunks": workspace_stats.get("documentChunks"),
        "candidateNodes": workspace_stats.get("candidateNodes"),
        "candidateEdges": workspace_stats.get("candidateEdges"),
        "skippedChunks": result.get("skippedChunks"),
        "reconciliation": (
            None
            if reconcile is None
            else {
                "called": True,
                "reconciled": reconcile.get("reconciled"),
                "passes": reconcile.get("passes_used"),
                "exhausted": reconcile.get("exhausted"),
                "issues": reconcile.get("issues"),
            }
        ),
        "selectedChunks": selected_chunks,
        "selectedChunkCount": len(selected_chunks),
        "finalReadinessIssues": [issue.get("code") for issue in readiness],
        "readinessErrorsEmpty": len(readiness) == 0,
        "validForPersistence": valid_for_persistence,
        "compiledNodes": compiled_nodes,
        "compiledEdges": compiled_edges,
        "persistedNodes": result.get("nodes"),
        "persistedEdges": result.get("edges"),
        "labelDistribution": result.get("labelDistribution"),
        "relationshipTypeDistribution": result.get(
            "relationshipTypeDistribution"
        ),
        "commitStatus": result.get("commitStatus"),
        "verificationStatus": result.get("verificationStatus"),
        "mismatchCount": result.get("mismatchCount"),
        "artifactName": result.get("artifactName"),
        "artifactVersion": result.get("artifactVersion"),
        "partialPersistence": result.get("partialPersistence"),
        "persistenceMode": result.get("persistenceMode"),
        "readinessIssuesIgnored": result.get("readinessIssuesIgnored"),
        "ingestionWarnings": result.get("ingestionWarnings"),
        "errorSummary": result.get("errorSummary"),
        "errors": (result.get("errors") or [])[:12],
        "affectedChunkIndexes": result.get("affectedChunkIndexes"),
        "nextAction": result.get("nextAction"),
        "retryRequired": result.get("retryRequired"),
    }
    print("=== E2E_REPORT ===")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print("=== RAW_RESULT_KEYS ===")
    print(json.dumps(list(result.keys()), ensure_ascii=False))
    (OUT_DIR / "e2e_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    printable_result = {
        key: value for key, value in result.items() if key != "nextBatch"
    }
    (OUT_DIR / "raw_result.json").write_text(
        json.dumps(printable_result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (OUT_DIR / "batch_capture.json").write_text(
        json.dumps(captured["batches"], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
