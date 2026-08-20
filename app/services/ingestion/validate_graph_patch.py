from typing import Any

from pydantic import ValidationError

from app.core.schemas.ingestion.models import (
    GraphPatch,
)
from app.services.ingestion.loader import (
    OntologyLoader,
)
from app.services.ingestion.registry import (
    OntologyRegistry,
)
from app.services.ingestion.validator import (
    OntologyValidator,
)

DEFAULT_ONTOLOGY_PATH = (
    "app/data/ontology/product_sales_knowledge_graph_base_v3_1.ontology.json"
)


class GraphPatchValidationService:
    def __init__(
        self,
        ontology_path: str = DEFAULT_ONTOLOGY_PATH,
    ):
        ontology = OntologyLoader.load(ontology_path)

        registry = OntologyRegistry(ontology)

        self.validator = OntologyValidator(registry)

    def validate(
        self,
        graph_patch: GraphPatch | dict[str, Any],
    ) -> dict[str, Any]:

        try:
            if isinstance(graph_patch, dict):
                patch = GraphPatch.model_validate(graph_patch)
            else:
                patch = graph_patch

        except ValidationError as exc:
            errors = []

            for error in exc.errors(include_url=False):
                location = ".".join(str(part) for part in error["loc"])

                errors.append(f"{location}: {error['msg']}")

            return {
                "valid": False,
                "errors": errors,
                "nodeCount": 0,
                "edgeCount": 0,
            }

        errors = self.validator.validate_graph_patch(patch)

        return {
            "valid": len(errors) == 0,
            "errors": errors,
            "nodeCount": len(patch.nodes),
            "edgeCount": len(patch.edges),
        }
