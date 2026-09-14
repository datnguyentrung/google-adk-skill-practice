"""Integration tests for Dynamic Schema Selection in ingestion orchestration."""

import pytest
from app.core.schemas.ingestion.document import DocumentChunk
from app.services.ingestion.orchestration.state import (
    CANDIDATE_SCHEMA_SKILLS_STATE_KEY,
    _get_schema_projection_builder,
    _get_schema_router,
    _get_schema_skill_registry,
)
from app.services.ingestion.orchestration.tools import begin_ingestion


class DummyRuntime:

    def __init__(self):
        self.state = {}

    async def load_artifact(self, filename: str):
        class DummyArtifact:
            text = "Điều kiện đăng ký thẻ tín dụng và quy định hồ sơ yêu cầu."
            inline_data = None
            data = None
        return DummyArtifact()

    async def save_artifact(self, filename: str, artifact, **kwargs):
        pass


@pytest.mark.anyio
async def test_begin_ingestion_populates_candidate_schema_skills():
    runtime = DummyRuntime()
    result = await begin_ingestion("test_doc.txt", runtime)

    assert result["success"] is True
    assert CANDIDATE_SCHEMA_SKILLS_STATE_KEY in runtime.state
    candidates = runtime.state[CANDIDATE_SCHEMA_SKILLS_STATE_KEY]
    assert "business-rules" in candidates


def test_schema_services_cache():
    reg1 = _get_schema_skill_registry()
    reg2 = _get_schema_skill_registry()
    assert reg1 is reg2

    r1 = _get_schema_router()
    r2 = _get_schema_router()
    assert r1 is r2

    b1 = _get_schema_projection_builder()
    b2 = _get_schema_projection_builder()
    assert b1 is b2
