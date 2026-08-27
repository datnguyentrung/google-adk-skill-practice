import asyncio
import json
import os
from pathlib import Path

import pytest

from app.tools import ingestion_tools
from app.services.ingestion import use_case as ingestion_use_case


DOCS = [
    Path("docs/HƯỚNG DẪN NGHIỆP VỤ SẢN PHẨM THẺ TÍN DỤNG FLEXI REWARDS.md"),
    Path("docs/HƯỚNG DẪN NGHIỆP VỤ SẢN PHẨM VAY MUA Ô TÔ LINH HOẠT.md"),
    Path("docs/TÀI LIỆU SẢN PHẨM TIỀN GỬI ONLINE AN TÂM.md"),
]

BLOCKED_ERROR_CODES = {
    "DANGLING_REFERENCE",
    "DERIVED_PROPERTY_REQUIRES_EDGE_EVIDENCE",
    "EDGE_RELATION_NOT_GROUNDED",
    "UNKNOWN_CLASS",
    "UNKNOWN_EDGE",
    "UNKNOWN_PROPERTY",
    "UNCHANGED_RETRY",
}


class FakeArtifact:
    def __init__(self, text: str):
        self.inline_data = None
        self.text = text


class FakeToolContext:
    def __init__(self, artifacts: dict[str, str]):
        self.state = {}
        self._artifacts = artifacts
        self.saved_artifacts = {}

    async def load_artifact(self, filename: str):
        text = self._artifacts.get(filename)
        if text is None:
            return None
        return FakeArtifact(text)

    async def save_artifact(self, filename: str, artifact, **kwargs):
        self.saved_artifacts[filename] = artifact
        return 1


def _load_dotenv_into_environ() -> None:
    env_path = Path(".env")
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _required_env_present() -> bool:
    _load_dotenv_into_environ()
    if os.getenv("RUN_INGESTION_DOCS_INTEGRATION") != "1":
        return False
    required = [
        "GOOGLE_API_KEY",
        "NEO4J_URI",
        "NEO4J_USERNAME",
        "NEO4J_PASSWORD",
        "NEO4J_DATABASE",
    ]
    return all(os.getenv(key) for key in required)


def _walk_errors(value):
    if isinstance(value, dict):
        if isinstance(value.get("code"), str):
            yield value["code"]
        for item in value.values():
            yield from _walk_errors(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_errors(item)


@pytest.mark.integration
@pytest.mark.skipif(
    not _required_env_present(),
    reason=(
        "Docs-to-Neo4j integration requires "
        "RUN_INGESTION_DOCS_INTEGRATION=1, GOOGLE_API_KEY, and NEO4J_* env vars."
    ),
)
@pytest.mark.parametrize("doc_path", DOCS)
def test_docs_ingest_end_to_end_and_commit_to_neo4j(doc_path, monkeypatch):
    ingestion_use_case._get_context_service.cache_clear()
    ingestion_use_case._get_validation_service.cache_clear()
    ingestion_use_case._get_workspace_service.cache_clear()
    ingestion_use_case._get_batch_extractor.cache_clear()
    context = FakeToolContext(
        {doc_path.name: doc_path.read_text(encoding="utf-8")}
    )

    result = asyncio.run(
        ingestion_tools.ingest_document_end_to_end(
            doc_path.name,
            context,
            persist=True,
            max_retries_per_batch=3,
        )
    )

    debug_payload = json.dumps(result, ensure_ascii=False, indent=2)
    assert result["success"] is True, debug_payload
    assert result["stage"] == "completed", debug_payload
    assert result["commitStatus"] == "committed", debug_payload
    assert result["verificationStatus"] == "verified", debug_payload
    assert result["nodes"] > 0, debug_payload
    assert not (set(_walk_errors(result)) & BLOCKED_ERROR_CODES), debug_payload
    if result.get("workspaceStats", {}).get("candidateNodes", 0) > 1:
        assert result["workspaceStats"]["candidateEdges"] > 0, debug_payload
