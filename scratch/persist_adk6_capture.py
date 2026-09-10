from __future__ import annotations
import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(r"D:\Thuc_tap_MB\google-adk-skill-practice")
os.chdir(ROOT)
for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    key, _, value = line.partition("=")
    os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))

uri = os.environ.get("NEO4J_URI", "")
if uri.startswith("neo4j+s://"):
    os.environ["NEO4J_URI"] = "neo4j+ssc://" + uri[len("neo4j+s://"):]

from app.config.neo4j import Neo4jClient
from app.core.schemas.ingestion.graph_patch import GraphPatchFragment
from app.services.ingestion.orchestration import (
    begin_ingestion,
    fill_ingestion,
    finalize_ingestion,
    submit_ingestion_batch,
)

SOURCE = ROOT / "docs" / "FLEXI_REWARDS_CREDIT_CARD_PRODUCT_BUSINESS_GUIDE_EN.md"
OUT = ROOT / ".e2e_run_out_en_adk6"
CAPTURE = json.loads((OUT / "batch_capture.json").read_text(encoding="utf-8"))
class RuntimeAdapter:
    def __init__(self):
        self.state = {}
        self.saved_artifacts = {}

    async def load_artifact(self, filename: str):
        if filename != SOURCE.name:
            return None
        return SimpleNamespace(inline_data=None, text=SOURCE.read_text(encoding="utf-8"))

    async def save_artifact(self, filename: str, artifact, **kwargs):
        self.saved_artifacts[filename] = artifact
        text = getattr(artifact, "text", None)
        if text is None:
            text = json.dumps(artifact, ensure_ascii=False, default=str)
        (OUT / filename).write_text(text, encoding="utf-8")
        return 1

async def main():
    Neo4jClient.connect().verify_connectivity()
    print("neo4j=ok")
    ctx = RuntimeAdapter()
    begin = await begin_ingestion(SOURCE.name, ctx)
    assert begin["success"], begin
    ingestion_id = begin["ingestionId"]
    assert begin["batchCount"] == len(CAPTURE) == 19
    for batch_index in range(begin["batchCount"]):
        entries = CAPTURE[str(batch_index)]
        fragment = GraphPatchFragment.model_validate(entries[-1]["fragment"])
        submitted = submit_ingestion_batch(ingestion_id, batch_index, fragment, ctx)
        assert submitted["success"], (batch_index, submitted)
    finalized = finalize_ingestion(ingestion_id, ctx)
    print("finalized=", json.dumps({
        "stage": finalized.get("stage"),
        "validForExtraction": finalized.get("validForExtraction"),
        "validForPersistence": finalized.get("validForPersistence"),
        "errors": finalized.get("errors"),
        "readinessIssues": finalized.get("readinessIssues"),
        "nodeCount": finalized.get("nodeCount"),
        "edgeCount": finalized.get("edgeCount"),
    }, ensure_ascii=False))
    assert finalized.get("validForPersistence") is True, finalized
    result = await fill_ingestion(ingestion_id, ctx, allow_partial_persistence=False)
    (OUT / "persist_replay_result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print("persist=", json.dumps(result, ensure_ascii=False, default=str))
    assert result.get("success") is True, result

if __name__ == "__main__":
    asyncio.run(main())