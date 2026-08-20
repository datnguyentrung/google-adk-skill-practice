import logging
from typing import Any

from pydantic import ValidationError

from app.core.schemas.ingestion.graph_patch import GraphPatch
from app.services.ingestion.loader import (
    OntologyLoader,
)
from app.services.ingestion.registry import (
    OntologyRegistry,
)
from app.services.ingestion.validator import (
    OntologyValidator,
)

logger = logging.getLogger(__name__)

DEFAULT_ONTOLOGY_PATH = (
    "app/data/ontology/product_sales_knowledge_graph_base_v3_1.ontology.json"
)


class GraphPatchValidationService:
    def __init__(
        self,
        ontology_path: str = DEFAULT_ONTOLOGY_PATH,
    ):
        logger.info(
            "Initializing GraphPatchValidationService ontology_path=%s",
            ontology_path,
        )
        ontology = OntologyLoader.load(ontology_path)

        registry = OntologyRegistry(ontology)

        self.validator = OntologyValidator(registry)

    def validate(
        self,
        graph_patch: GraphPatch | dict[str, Any],
    ) -> dict[str, Any]:

        logger.info(
            "GraphPatch validation started input_type=%s",
            type(graph_patch).__name__,
        )

        try:
            if isinstance(graph_patch, dict):
                patch = GraphPatch.model_validate(graph_patch)
                logger.info(
                    "GraphPatch schema conversion succeeded node_count=%s edge_count=%s warning_count=%s",
                    len(patch.nodes),
                    len(patch.edges),
                    len(patch.warnings),
                )
            else:
                patch = graph_patch
                logger.info(
                    "GraphPatch schema conversion skipped node_count=%s edge_count=%s warning_count=%s",
                    len(patch.nodes),
                    len(patch.edges),
                    len(patch.warnings),
                )

        except ValidationError as exc:
            errors = []

            for error in exc.errors(include_url=False):
                location = ".".join(str(part) for part in error["loc"])

                errors.append(f"{location}: {error['msg']}")

            logger.warning(
                "GraphPatch schema validation failed error_count=%s",
                len(errors),
            )

            return {
                "valid": False,
                "errors": errors,
                "nodeCount": 0,
                "edgeCount": 0,
            }

        errors = self.validator.validate_graph_patch(patch)
        log_method = logger.info if not errors else logger.warning
        log_method(
            "GraphPatch ontology validation completed valid=%s node_count=%s edge_count=%s error_count=%s",
            not errors,
            len(patch.nodes),
            len(patch.edges),
            len(errors),
        )

        return {
            "valid": len(errors) == 0,
            "errors": errors,
            "nodeCount": len(patch.nodes),
            "edgeCount": len(patch.edges),
        }
