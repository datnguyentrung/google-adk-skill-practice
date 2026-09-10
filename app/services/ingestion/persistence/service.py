"""Phase 5 — Cổng ghi graph patch xuống Neo4j (facade `GraphPersistence`).

Module này ghép các mảnh của phase persistence lại: validate lần cuối, resolve
identity, ghi node/edge, đọc lại và đối chiếu. Caller chỉ cần gọi `fill()` với patch
đã được validate; mọi chi tiết về Neo4j được giữ bên trong."""

from pathlib import Path
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

from app.services.ingestion.document.preparation import DEFAULT_ONTOLOGY_PATH
from app.services.ingestion.identity.resolver import create_product_sales_identity_resolver
from app.services.ingestion.ontology.loader import OntologyLoader
from app.services.ingestion.ontology.registry import OntologyRegistry
from app.services.ingestion.persistence.mapping import Neo4jMapper
from app.services.ingestion.persistence.readback import verify_persisted_graph
from app.services.ingestion.persistence.writer import Neo4jGraphStore
from app.services.ingestion.validation.graph_validation import GraphValidation


class FillValidationError(ValueError):
    """
    Lỗi khi patch chưa vượt qua cổng kiểm định nên không được phép ghi.

    Args:
        result: Kết quả validate kèm danh sách issue để caller báo lỗi.
    """
    def __init__(self, result: GraphPatchValidationResult):
        self.result = result
        super().__init__("Graph patch is not ready for persistence")


class GraphPersistence:
    """
    Cổng ghi graph patch xuống Neo4j và xác minh bằng readback.
    """
    def __init__(
        self,
        client: Neo4jClient,
        validation: GraphValidation,
        writer: Neo4jGraphStore,
    ):
        """
        Khởi tạo store Neo4j, mapper và identity resolver.

        Args:
            client: Client Neo4j đã kết nối.
            ontology_path: Đường dẫn ontology JSON.
        """
        self.client = client
        self.validation = validation
        self.writer = writer

    def fill(
        self,
        graph_patch: GraphPatchDraft | dict[str, Any],
        artifact_content_digest: str | None,
        source_chunks: list[DocumentChunk] | list[dict[str, Any]] | None = None,
        *,
        allow_partial_persistence: bool = False,
    ) -> dict[str, Any]:
        """
        Ghi patch xuống Neo4j sau khi validate lại lần cuối.

        Args:
            graph_patch: Patch đã compile.
            artifact_content_digest: Digest artifact nguồn (nếu có).
            source_chunks: Chunk nguồn dùng để validate.
            allow_partial_persistence: Cho phép ghi một phần khi patch chưa đầy đủ.

        Returns:
            `FillResult` mô tả kết quả ghi.

        Raises:
            FillValidationError: Patch không vượt qua cổng validate.
        """
        assessment = self.validation.assess(
            graph_patch,
            artifact_content_digest,
            source_chunks,
        )
        if assessment.compiled_patch is None or not assessment.result.valid_for_extraction:
            raise FillValidationError(assessment.result)
        partial_persistence = not assessment.result.valid_for_persistence
        if partial_persistence and not allow_partial_persistence:
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
            partialPersistence=partial_persistence,
            persistenceMode="partial" if partial_persistence else "strict",
            readinessIssuesIgnored=(
                [
                    issue.model_dump(by_alias=True, exclude_none=True)
                    for issue in assessment.result.readiness_issues
                ]
                if partial_persistence
                else []
            ),
        ).model_dump(by_alias=True, mode="json")

    def close(self) -> None:
        """
        Đóng kết nối Neo4j đang giữ.
        """
        self.client.close_driver()


def create_graph_persistence(
    ontology_path: str | Path = DEFAULT_ONTOLOGY_PATH,
    *,
    validation: GraphValidation | None = None,
) -> GraphPersistence:
    """
    Tạo `GraphPersistence` với kết nối Neo4j và ontology mặc định.

    Args:
        ontology_path: Đường dẫn ontology JSON (mặc định lấy từ compiler).
    """
    ontology = OntologyLoader.load(ontology_path)
    registry = OntologyRegistry(ontology)
    return GraphPersistence(
        client=Neo4jClient(),
        validation=validation or GraphValidation(ontology_path),
        writer=Neo4jGraphStore(
            mapper=Neo4jMapper(registry),
            identity_resolver=create_product_sales_identity_resolver(registry),
        ),
    )
