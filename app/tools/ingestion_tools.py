import logging
from functools import lru_cache
from typing import Any

from google.adk.tools import ToolContext
from pydantic import ValidationError

from app.core.schemas.ingestion.graph_patch import GraphPatch
from app.services.ingestion.fill_factory import create_fill_service
from app.services.ingestion.fill_service import FillValidationError
from app.services.ingestion.prepare_extraction_context import (
    ExtractionContextService,
)
from app.services.ingestion.validate_graph_patch import (
    GraphPatchValidationService,
)

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _get_context_service() -> ExtractionContextService:
    return ExtractionContextService()


@lru_cache(maxsize=1)
def _get_validation_service() -> GraphPatchValidationService:
    return GraphPatchValidationService()


# ============================================================
# TOOL 1
# ============================================================


async def prepare_extraction_context(
    artifact_name: str,
    tool_context: ToolContext,
) -> dict[str, Any]:
    """
    Load an uploaded ADK artifact and prepare document chunks
    together with ontology context for semantic extraction.

    The artifact_name must refer to a file uploaded in the
    current ADK session.
    """

    logger.info(
        "Ingestion prepare_extraction_context started artifact=%s",
        artifact_name,
    )

    try:
        # ------------------------------------------
        # 1. Load uploaded artifact from ADK
        # ------------------------------------------

        artifact = await tool_context.load_artifact(
            filename=artifact_name,
        )

        if artifact is None:
            logger.warning(
                "Ingestion artifact load failed artifact=%s reason=not_found",
                artifact_name,
            )
            return {
                "success": False,
                "stage": "artifact_loading",
                "error": f"Artifact not found: {artifact_name}",
            }

        logger.info(
            "Ingestion artifact loaded artifact=%s has_inline_data=%s has_text=%s",
            artifact_name,
            artifact.inline_data is not None,
            artifact.text is not None,
        )

        # ------------------------------------------
        # 2. Extract bytes + MIME type
        # ------------------------------------------

        data: bytes
        mime_type: str | None = None

        if artifact.inline_data is not None:
            raw_data = artifact.inline_data.data
            mime_type = artifact.inline_data.mime_type

            if raw_data is None:
                logger.warning(
                    "Ingestion artifact has no binary data artifact=%s",
                    artifact_name,
                )
                return {
                    "success": False,
                    "stage": "artifact_loading",
                    "error": (f"Artifact contains no binary data: {artifact_name}"),
                }

            if isinstance(raw_data, bytes):
                data = raw_data

            elif isinstance(raw_data, bytearray):
                data = bytes(raw_data)

            else:
                logger.warning(
                    "Ingestion artifact unsupported data type artifact=%s data_type=%s",
                    artifact_name,
                    type(raw_data).__name__,
                )
                return {
                    "success": False,
                    "stage": "artifact_loading",
                    "error": (
                        f"Unsupported artifact data type: {type(raw_data).__name__}"
                    ),
                }

            logger.info(
                "Ingestion artifact inline data parsed artifact=%s mime_type=%s byte_count=%s",
                artifact_name,
                mime_type,
                len(data),
            )

        elif artifact.text is not None:
            data = artifact.text.encode("utf-8")
            mime_type = "text/plain"
            logger.info(
                "Ingestion artifact text parsed artifact=%s char_count=%s byte_count=%s",
                artifact_name,
                len(artifact.text),
                len(data),
            )

        else:
            logger.warning(
                "Ingestion artifact contains no supported content artifact=%s",
                artifact_name,
            )
            return {
                "success": False,
                "stage": "artifact_loading",
                "error": (
                    "Artifact does not contain supported "
                    f"inline data or text: {artifact_name}"
                ),
            }

        # ------------------------------------------
        # 3. Remember ingestion source
        # ------------------------------------------

        tool_context.state["temp:ingestion_source"] = artifact_name
        logger.info(
            "Ingestion source stored in tool context artifact=%s",
            artifact_name,
        )

        # ------------------------------------------
        # 4. Prepare extraction context
        # ------------------------------------------

        context = _get_context_service().prepare_uploaded_document(
            filename=artifact_name,
            data=data,
            mime_type=mime_type,
        )

        logger.info(
            "Ingestion extraction context prepared artifact=%s chunk_count=%s ontology_context_chars=%s",
            artifact_name,
            len(context.chunks),
            len(context.ontology_context),
        )
        logger.info(
            "Ingestion handoff ready for model GraphPatch extraction artifact=%s",
            artifact_name,
        )

        return {
            "success": True,
            "stage": "completed",
            **context.model_dump(),
        }

    except Exception as exc:
        logger.exception(
            "Failed to prepare extraction context for artifact '%s'",
            artifact_name,
        )

        return {
            "success": False,
            "stage": "prepare_extraction_context",
            "error": str(exc),
        }


# ============================================================
# TOOL 2
# ============================================================


def validate_graph_patch(
    graph_patch: GraphPatch,
) -> dict[str, Any]:
    """
    Validate a root-model-generated GraphPatch against
    the Product Sales ontology.

    This tool does not write to Neo4j.
    """

    logger.info(
        "Ingestion validate_graph_patch started input_type=%s",
        type(graph_patch).__name__,
    )

    result = _get_validation_service().validate(graph_patch)
    log_method = logger.info if result["valid"] else logger.warning
    log_method(
        "Ingestion validate_graph_patch completed valid=%s node_count=%s edge_count=%s error_count=%s",
        result["valid"],
        result["nodeCount"],
        result["edgeCount"],
        len(result["errors"]),
    )

    return result


# ============================================================
# TOOL 3
# ============================================================


def fill_graph_patch(
    graph_patch: GraphPatch,
) -> dict[str, Any]:
    """
    Validate and persist a GraphPatch to Neo4j.

    Neo4j is initialized lazily only when this tool is called.

    This tool:
    - does not perform semantic extraction
    - does not invent missing knowledge
    - does not modify the ontology
    - validates before writing
    - writes the patch inside a Neo4j transaction
    """

    logger.info(
        "Ingestion fill_graph_patch started input_type=%s",
        type(graph_patch).__name__,
    )

    # ------------------------------------------
    # 1. Validate GraphPatch schema
    # ------------------------------------------

    try:
        patch = GraphPatch.model_validate(graph_patch)

    except ValidationError as exc:
        errors: list[str] = []

        for error in exc.errors(include_url=False):
            location = ".".join(str(part) for part in error["loc"])

            errors.append(f"{location}: {error['msg']}")

        logger.warning(
            "Ingestion fill_graph_patch schema validation failed error_count=%s",
            len(errors),
        )

        return {
            "success": False,
            "stage": "schema_validation",
            "errors": errors,
        }

    logger.info(
        "Ingestion fill_graph_patch schema validation passed node_count=%s edge_count=%s warning_count=%s",
        len(patch.nodes),
        len(patch.edges),
        len(patch.warnings),
    )

    # ------------------------------------------
    # 2. Create Neo4j FillService lazily
    # ------------------------------------------

    service = None

    try:
        logger.info("Ingestion creating Neo4j fill service")
        service = create_fill_service()

        logger.info("Ingestion calling FillService.fill")
        result = service.fill(patch)

        logger.info(
            "Ingestion fill_graph_patch completed node_count=%s edge_count=%s node_id_count=%s",
            result["nodes"],
            result["edges"],
            len(result["nodeIds"]),
        )

        return {
            "success": True,
            "stage": "completed",
            **result,
        }

    except FillValidationError as exc:
        errors = [error for error in str(exc).splitlines() if error.strip()]
        logger.warning(
            "Ingestion fill_graph_patch ontology validation failed error_count=%s",
            len(errors),
        )
        return {
            "success": False,
            "stage": "ontology_validation",
            "errors": errors,
        }

    except Exception as exc:
        logger.exception("Failed to persist GraphPatch to Neo4j")

        return {
            "success": False,
            "stage": "persistence",
            "errors": [str(exc)],
        }

    finally:
        if service is not None:
            logger.info("Ingestion closing Neo4j fill service")
            service.close()


# ============================================================
# TOOL REGISTRY
# ============================================================


INGESTION_TOOLS = {
    "prepare_extraction_context": prepare_extraction_context,
    "validate_graph_patch": validate_graph_patch,
    "fill_graph_patch": fill_graph_patch,
}


def get_ingestion_tools() -> list:
    """Return all Python tools available to the ingestion skill."""

    return list(INGESTION_TOOLS.values())
