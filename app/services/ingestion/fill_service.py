from typing import Any

from app.config.neo4j import Neo4jClient
from app.core.schemas.ingestion.document import DocumentChunk
from app.core.schemas.ingestion.graph_patch import GraphPatchDraft
from app.core.schemas.ingestion.persistence import (
    CommitStatus,
    FillResult,
    FillStatus,
)
from app.core.schemas.ingestion.validation import GraphPatchValidationResult
from app.services.ingestion.neo4j_writer import Neo4jGraphStore
from app.services.ingestion.readback import verify_persisted_graph
from app.services.ingestion.validate_graph_patch import GraphPatchValidationService


class FillValidationError(ValueError):
    def __init__(self, result: GraphPatchValidationResult):
        self.result = result
        super().__init__("Graph patch is not ready for persistence")


class FillService:
    def __init__(
        self,
        client: Neo4jClient,
        validation_service: GraphPatchValidationService,
        writer: Neo4jGraphStore,
    ):
        self.client = client
        self.validation_service = validation_service
        self.writer = writer

    def fill(
        self,
        graph_patch: GraphPatchDraft | dict[str, Any],
        artifact_content_digest: str | None,
        source_chunks: list[DocumentChunk] | list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        assessment = self.validation_service.assess(
            graph_patch,
            artifact_content_digest,
            source_chunks,
        )
        if (
            not assessment.result.valid_for_persistence
            or assessment.compiled_patch is None
        ):
            raise FillValidationError(assessment.result)

        patch = assessment.compiled_patch
        driver = self.client.get_driver()
        readback_error: Exception | None = None
        with driver.session(database=self.client.database_name) as session:
            write_result = session.execute_write(
                lambda tx: self.writer.write_graph_patch(tx, patch)
            )
            try:
                readback = session.execute_read(
                    lambda tx: self.writer.read_graph_patch(tx, write_result)
                )
            except Exception as exc:  # noqa: BLE001 - commit already succeeded
                readback_error = exc
                readback = {"nodes": [], "relationships": []}

        receipt = verify_persisted_graph(
            patch,
            write_result,
            readback,
            self.writer.mapper,
        )
        if readback_error is not None:
            receipt.verified = False
            receipt.mismatches.insert(0, f"readback failed: {readback_error}")

        return FillResult(
            status=(
                FillStatus.SUCCESS
                if receipt.verified
                else FillStatus.READBACK_MISMATCH
            ),
            commitStatus=CommitStatus.COMMITTED,
            nodes=len(patch.nodes),
            edges=len(patch.edges),
            nodeIds=write_result.node_ids,
            relationshipIds=write_result.relationship_ids,
            receipt=receipt,
        ).model_dump(by_alias=True, mode="json")

    def close(self) -> None:
        self.client.close_driver()
