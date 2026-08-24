from typing import Any

from app.config.neo4j import Neo4jClient
from app.core.schemas.ingestion.graph_patch import GraphPatchDraft
from app.core.schemas.ingestion.validation import GraphPatchValidationResult
from app.services.ingestion.neo4j_writer import Neo4jWriter
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
        writer: Neo4jWriter,
    ):
        self.client = client
        self.validation_service = validation_service
        self.writer = writer

    def fill(
        self,
        graph_patch: GraphPatchDraft | dict[str, Any],
        artifact_content_digest: str | None,
    ) -> dict[str, Any]:
        assessment = self.validation_service.assess(
            graph_patch,
            artifact_content_digest,
        )
        if (
            not assessment.result.valid_for_persistence
            or assessment.compiled_patch is None
        ):
            raise FillValidationError(assessment.result)

        patch = assessment.compiled_patch
        driver = self.client.get_driver()
        with driver.session(database=self.client.database_name) as session:
            node_ids = session.execute_write(
                lambda tx: self.writer.write_graph_patch(tx, patch)
            )

        return {
            "status": "success",
            "nodes": len(patch.nodes),
            "edges": len(patch.edges),
            "nodeIds": node_ids,
        }

    def close(self) -> None:
        self.client.close_driver()
