import logging

from app.config.neo4j import (
    Neo4jClient,
)
from app.services.ingestion.neo4j_writer import (
    Neo4jWriter,
)
from app.services.ingestion.validator import (
    OntologyValidator,
)

logger = logging.getLogger(__name__)


class FillValidationError(ValueError):
    pass


class FillService:
    def __init__(
        self,
        client: Neo4jClient,
        validator: OntologyValidator,
        writer: Neo4jWriter,
    ):
        self.client = client
        self.validator = validator
        self.writer = writer

    def fill(
        self,
        patch,
    ) -> dict:
        logger.info(
            "FillService.fill started node_count=%s edge_count=%s warning_count=%s",
            len(patch.nodes),
            len(patch.edges),
            len(patch.warnings),
        )
        logger.info("FillService validating GraphPatch before Neo4j write")
        errors = self.validator.validate_graph_patch(patch)

        if errors:
            logger.warning(
                "FillService validation failed error_count=%s",
                len(errors),
            )
            raise FillValidationError("\n".join(errors))

        logger.info("FillService validation passed")
        logger.info("FillService acquiring Neo4j driver")
        driver = self.client.get_driver()

        logger.info(
            "FillService opening Neo4j session database=%s",
            self.client.database_name,
        )
        with driver.session(
            database=self.client.database_name
        ) as session:
            logger.info("FillService executing Neo4j write transaction")
            node_ids = session.execute_write(
                lambda tx: self.writer.write_graph_patch(
                    tx,
                    patch,
                )
            )
            logger.info(
                "FillService Neo4j write transaction completed node_id_count=%s",
                len(node_ids),
            )

        logger.info(
            "FillService.fill completed node_count=%s edge_count=%s",
            len(patch.nodes),
            len(patch.edges),
        )
        return {
            "status": "success",
            "nodes": len(patch.nodes),
            "edges": len(patch.edges),
            "nodeIds": node_ids,
        }

    def close(self) -> None:
        logger.info("FillService closing Neo4j driver")
        self.client.close_driver()
