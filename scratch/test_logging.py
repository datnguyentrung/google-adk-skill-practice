import asyncio
import logging
from app.core.logging_config import configure_logging
from app.services.ingestion.use_case import ingest_document_end_to_end, IngestionRuntime
from app.core.schemas.ingestion.document import DocumentChunk

configure_logging(logging.INFO)
logger = logging.getLogger("test_logging")

class MockRuntime:
    def __init__(self):
        self.state = {}

    async def load_artifact(self, filename: str):
        if filename == "non_existent_doc.md":
            return None
        class FakeArtifact:
            text = "Sample content for testing ingestion"
            inline_data = None
        return FakeArtifact()

    async def save_artifact(self, filename: str, artifact, **kwargs):
        return "v1"

async def test_error_flow():
    runtime = MockRuntime()
    logger.info("--- Testing Missing Artifact Error Flow ---")
    res1 = await ingest_document_end_to_end("non_existent_doc.md", runtime)
    print("Result 1:", res1)

    logger.info("--- Testing Valid Artifact Flow ---")
    res2 = await ingest_document_end_to_end("sample_doc.md", runtime, persist=False)
    print("Result 2:", res2)

if __name__ == "__main__":
    asyncio.run(test_error_flow())
