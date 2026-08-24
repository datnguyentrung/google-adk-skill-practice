import hashlib
import logging
from functools import lru_cache
from typing import Any

from google.adk.tools import ToolContext

from app.core.schemas.ingestion.graph_patch import GraphPatchDraft
from app.core.schemas.ingestion.validation import ValidationIssue
from app.services.ingestion.fill_factory import create_fill_service
from app.services.ingestion.fill_service import FillValidationError
from app.services.ingestion.prepare_extraction_context import ExtractionContextService
from app.services.ingestion.validate_graph_patch import (
    GraphPatchAssessment,
    GraphPatchValidationService,
)

logger = logging.getLogger(__name__)

ARTIFACT_DIGEST_STATE_KEY = "temp:ingestion_source_artifact_digest"
ARTIFACT_NAME_STATE_KEY = "temp:ingestion_source_artifact_name"
VALIDATED_FINGERPRINT_STATE_KEY = "temp:ingestion_validated_fingerprint"
SOURCE_CHUNKS_STATE_KEY = "temp:ingestion_source_chunks"


@lru_cache(maxsize=1)
def _get_context_service() -> ExtractionContextService:
    return ExtractionContextService()


@lru_cache(maxsize=1)
def _get_validation_service() -> GraphPatchValidationService:
    return GraphPatchValidationService()


def _delete_state(tool_context: ToolContext, key: str) -> None:
    if key in tool_context.state:
        del tool_context.state[key]


def _clear_validation_gate(tool_context: ToolContext) -> None:
    _delete_state(tool_context, VALIDATED_FINGERPRINT_STATE_KEY)


def _public_assessment(assessment: GraphPatchAssessment) -> dict[str, Any]:
    return assessment.result.model_dump(by_alias=True, exclude_none=True)


async def prepare_extraction_context(
    artifact_name: str,
    tool_context: ToolContext,
) -> dict[str, Any]:
    """Load an artifact and prepare source-grounded extraction context."""

    _clear_validation_gate(tool_context)
    _delete_state(tool_context, ARTIFACT_DIGEST_STATE_KEY)
    _delete_state(tool_context, ARTIFACT_NAME_STATE_KEY)
    _delete_state(tool_context, SOURCE_CHUNKS_STATE_KEY)

    try:
        artifact = await tool_context.load_artifact(filename=artifact_name)
        if artifact is None:
            return {
                "success": False,
                "stage": "artifact_loading",
                "error": f"Artifact not found: {artifact_name}",
            }

        data: bytes
        mime_type: str | None = None
        if artifact.inline_data is not None:
            raw_data = artifact.inline_data.data
            mime_type = artifact.inline_data.mime_type
            if raw_data is None:
                return {
                    "success": False,
                    "stage": "artifact_loading",
                    "error": f"Artifact contains no binary data: {artifact_name}",
                }
            if isinstance(raw_data, bytes):
                data = raw_data
            elif isinstance(raw_data, bytearray):
                data = bytes(raw_data)
            else:
                return {
                    "success": False,
                    "stage": "artifact_loading",
                    "error": (
                        f"Unsupported artifact data type: {type(raw_data).__name__}"
                    ),
                }
        elif artifact.text is not None:
            data = artifact.text.encode("utf-8")
            mime_type = "text/plain"
        else:
            return {
                "success": False,
                "stage": "artifact_loading",
                "error": (
                    "Artifact does not contain supported inline data or text: "
                    f"{artifact_name}"
                ),
            }

        artifact_digest = hashlib.sha256(data).hexdigest()
        tool_context.state[ARTIFACT_DIGEST_STATE_KEY] = artifact_digest
        tool_context.state[ARTIFACT_NAME_STATE_KEY] = artifact_name

        context = _get_context_service().prepare_uploaded_document(
            filename=artifact_name,
            data=data,
            mime_type=mime_type,
        )
        tool_context.state[SOURCE_CHUNKS_STATE_KEY] = [
            chunk.model_dump() for chunk in context.chunks
        ]
        return {
            "success": True,
            "stage": "completed",
            "chunkCount": len(context.chunks),
            **context.model_dump(),
        }
    except Exception as exc:
        logger.exception("Failed to prepare extraction context for '%s'", artifact_name)
        _clear_validation_gate(tool_context)
        _delete_state(tool_context, ARTIFACT_DIGEST_STATE_KEY)
        _delete_state(tool_context, ARTIFACT_NAME_STATE_KEY)
        _delete_state(tool_context, SOURCE_CHUNKS_STATE_KEY)
        return {
            "success": False,
            "stage": "prepare_extraction_context",
            "error": str(exc),
        }


def validate_graph_patch(
    graph_patch: GraphPatchDraft,
    tool_context: ToolContext,
) -> dict[str, Any]:
    """Assess extraction correctness and persistence readiness without writing."""

    assessment = _get_validation_service().assess(
        graph_patch,
        tool_context.state.get(ARTIFACT_DIGEST_STATE_KEY),
        tool_context.state.get(SOURCE_CHUNKS_STATE_KEY),
    )
    if (
        assessment.result.valid_for_extraction
        and assessment.result.valid_for_persistence
        and assessment.fingerprint is not None
    ):
        tool_context.state[VALIDATED_FINGERPRINT_STATE_KEY] = assessment.fingerprint
    else:
        _clear_validation_gate(tool_context)
    return _public_assessment(assessment)


def fill_graph_patch(
    graph_patch: GraphPatchDraft,
    tool_context: ToolContext,
) -> dict[str, Any]:
    """Persist only the invocation-scoped, validated graph patch."""

    artifact_digest = tool_context.state.get(ARTIFACT_DIGEST_STATE_KEY)
    validation_service = _get_validation_service()
    validated_fingerprint = tool_context.state.get(
        VALIDATED_FINGERPRINT_STATE_KEY
    )
    candidate_fingerprint = validation_service.fingerprint_candidate(
        graph_patch,
        artifact_digest,
    )
    if (
        validated_fingerprint is None
        or candidate_fingerprint is None
        or validated_fingerprint != candidate_fingerprint
    ):
        issue = ValidationIssue(
            code="VALIDATION_PRECONDITION",
            message=(
                "The current graph patch and artifact must pass validation in "
                "this invocation before fill_graph_patch can run"
            ),
            location="graphPatch",
        )
        return {
            "success": False,
            "stage": "validation_precondition",
            "errors": [issue.model_dump(by_alias=True, exclude_none=True)],
        }

    source_chunks = tool_context.state.get(SOURCE_CHUNKS_STATE_KEY)
    assessment = validation_service.assess(
        graph_patch,
        artifact_digest,
        source_chunks,
    )
    if not assessment.result.valid_for_persistence:
        _clear_validation_gate(tool_context)
        return {
            "success": False,
            "stage": "validation",
            "validation": _public_assessment(assessment),
        }

    service = None
    try:
        service = create_fill_service(
            validation_service=validation_service,
        )
        result = service.fill(graph_patch, artifact_digest, source_chunks)
        return {"success": True, "stage": "completed", **result}
    except FillValidationError as exc:
        _clear_validation_gate(tool_context)
        return {
            "success": False,
            "stage": "validation",
            "validation": exc.result.model_dump(by_alias=True, exclude_none=True),
        }
    except Exception as exc:
        logger.exception("Failed to persist GraphPatch to Neo4j")
        issue = ValidationIssue(
            code="NEO4J_WRITE_FAILED",
            message=str(exc),
            location="persistence",
        )
        return {
            "success": False,
            "stage": "persistence",
            "errors": [issue.model_dump(by_alias=True, exclude_none=True)],
        }
    finally:
        if service is not None:
            service.close()


INGESTION_TOOLS = {
    "prepare_extraction_context": prepare_extraction_context,
    "validate_graph_patch": validate_graph_patch,
    "fill_graph_patch": fill_graph_patch,
}


def get_ingestion_tools() -> list:
    return list(INGESTION_TOOLS.values())
