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

DEFAULT_SOURCE = next((REPO_ROOT / "docs").glob("*FLEXI REWARDS.md"))
SOURCE_FILE = Path(os.getenv("FLEXI_E2E_SOURCE", str(DEFAULT_SOURCE)))
if not SOURCE_FILE.is_absolute():
    SOURCE_FILE = REPO_ROOT / SOURCE_FILE
ARTIFACT_NAME = SOURCE_FILE.name
OUT_DIR = Path(os.getenv("FLEXI_E2E_OUT", str(REPO_ROOT / ".e2e_run_out")))
if not OUT_DIR.is_absolute():
    OUT_DIR = REPO_ROOT / OUT_DIR
OUT_DIR.mkdir(parents=True, exist_ok=True)

captured = {
    "finalized": None,
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
    from app.services.ingestion.orchestration import tools as uc

    orig_mapper_factory = uc._get_graph_mapper

    def mapper_factory():
        mapper = orig_mapper_factory()
        if getattr(mapper, "_flexi_capture_wrapped", False):
            return mapper
        orig_map_batch = mapper.map_batch

        def map_batch_wrapper(*, batch_payload, **kwargs):
            outcome = orig_map_batch(batch_payload=batch_payload, **kwargs)
            batch_index = batch_payload.get("batchIndex")
            captured["batches"].setdefault(batch_index, []).append(
                {
                    "stage": "direct_graph_mapping",
                    "graph_context": kwargs.get("graph_context"),
                    "previous_error": kwargs.get("previous_error"),
                    "stats": {
                        "nodes": len(outcome.nodes),
                        "edges": len(outcome.edges),
                        "coverage": len(outcome.coverage),
                    },
                    "fragment": outcome.model_dump(by_alias=True, mode="json"),
                }
            )
            return outcome

        mapper.map_batch = map_batch_wrapper
        mapper._flexi_capture_wrapped = True
        return mapper

    uc._get_graph_mapper = mapper_factory

    orig_finalize = uc.finalize_ingestion

    def finalize_wrapper(ingestion_id, tool_context):
        response = orig_finalize(ingestion_id, tool_context)
        captured["finalized"] = response
        return response

    uc.finalize_ingestion = finalize_wrapper


def main() -> None:
    install_capture_wrappers()

    from app.services.ingestion.orchestration import IngestionUseCase

    if not os.getenv("GOOGLE_API_KEY"):
        raise SystemExit("GOOGLE_API_KEY is not set")
    persist = os.getenv("FLEXI_E2E_PERSIST", "true").strip().lower() not in {
        "0", "false", "no"
    }
    if persist:
        from app.config.neo4j import Neo4jClient
        try:
            Neo4jClient.connect().verify_connectivity()
            print("NEO4J_CONNECTIVITY=ok")
        except Exception as exc:
            print(f"NEO4J_CONNECTIVITY=failed {exc!r}")
            raise

    adapter = RuntimeAdapter()
    result = asyncio.run(
        IngestionUseCase().ingest_end_to_end(
            ARTIFACT_NAME,
            adapter,
            persist=persist,
        )
    )

    workspace_stats = result.get("workspaceStats", {})
    finalized = captured["finalized"] or {}
    readiness = finalized.get("readinessIssues", [])
    compiled_nodes = finalized.get("nodeCount")
    compiled_edges = finalized.get("edgeCount")
    valid_for_persistence = finalized.get("validForPersistence")

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
