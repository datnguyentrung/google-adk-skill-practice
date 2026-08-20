from app.config.neo4j import (
    Neo4jClient,
)
from app.services.ingestion.neo4j_writer import (
    Neo4jWriter,
)
from app.services.ingestion.validator import (
    OntologyValidator,
)


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
        errors = self.validator.validate_graph_patch(patch)

        if errors:
            raise FillValidationError("\n".join(errors))

        with self.client.driver.session(
            database=self.client.settings.database
        ) as session:
            node_ids = session.execute_write(
                lambda tx: self.writer.write_graph_patch(
                    tx,
                    patch,
                )
            )

        return {
            "status": "success",
            "nodes": len(patch.nodes),
            "edges": len(patch.edges),
            "nodeIds": node_ids,
        }
