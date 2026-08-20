from pydantic import ValidationError
from sqlalchemy.dialects.postgresql import Any

from app.core.schemas.ingestion.models import (
    GraphPatch,
)
from app.services.ingestion.fill_factory import create_fill_service
from app.services.ingestion.fill_service import FillValidationError
from app.services.ingestion.prepare_extraction_context import ExtractionContextService
from app.services.ingestion.validate_graph_patch import GraphPatchValidationService

_context_service = ExtractionContextService()

_validation_service = GraphPatchValidationService()


# ============================================================
# TOOL 1
# ============================================================


def prepare_extraction_context(
    document_path: str,
) -> dict[str, Any]:
    """
    Read a Product Sales business document and prepare
    document chunks plus ontology context for the root model.

    This tool:
    - does not perform semantic reasoning
    - does not call another LLM
    - does not modify the ontology
    - does not write to Neo4j
    """

    context = _context_service.prepare(document_path)

    return context.model_dump()


# ============================================================
# TOOL 2
# ============================================================


def validate_graph_patch(
    graph_patch: dict[str, Any],
) -> dict[str, Any]:
    """
    Validate a root-model-generated GraphPatch against
    the Product Sales ontology.

    This tool does not write to Neo4j.
    """

    return _validation_service.validate(graph_patch)


# ============================================================
# TOOL 3
# ============================================================


def fill_graph_patch(
    graph_patch: dict[str, Any],
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

        return {
            "success": False,
            "stage": "schema_validation",
            "errors": errors,
        }

    # ------------------------------------------
    # 2. Create Neo4j FillService LAZILY
    # ------------------------------------------

    service = None

    try:
        service = create_fill_service()()

        # Optional nhưng hữu ích:
        service.client.verify_connectivity()

        result = service.fill(patch)

        return {
            "success": True,
            "stage": "completed",
            **result,
        }

    except FillValidationError as exc:
        return {
            "success": False,
            "stage": "ontology_validation",
            "errors": [error for error in str(exc).splitlines() if error.strip()],
        }

    except Exception as exc:
        return {
            "success": False,
            "stage": "persistence",
            "errors": [str(exc)],
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
    """Return all Python tools available to the ingestion skill."""
    return list(INGESTION_TOOLS.values())
