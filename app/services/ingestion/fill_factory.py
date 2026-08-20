from pathlib import Path

from app.config.neo4j import Neo4jClient
from app.services.ingestion.fill_service import FillService
from app.services.ingestion.identity import (
    create_product_sales_identity_resolver,
)
from app.services.ingestion.loader import OntologyLoader
from app.services.ingestion.neo4j_mapper import Neo4jMapper
from app.services.ingestion.neo4j_writer import Neo4jWriter
from app.services.ingestion.registry import OntologyRegistry
from app.services.ingestion.validator import OntologyValidator

DEFAULT_ONTOLOGY_PATH = Path(
    "app/data/ontology/product_sales_knowledge_graph_base_v3_1.ontology.json"
)


def create_fill_service(
    ontology_path: str | Path = DEFAULT_ONTOLOGY_PATH,
) -> FillService:
    ontology = OntologyLoader.load(ontology_path)

    registry = OntologyRegistry(ontology)

    validator = OntologyValidator(registry)

    identity_resolver = create_product_sales_identity_resolver(registry)

    mapper = Neo4jMapper(registry)

    client = Neo4jClient()

    writer = Neo4jWriter(
        mapper=mapper,
        identity_resolver=identity_resolver,
    )

    return FillService(
        client=client,
        validator=validator,
        writer=writer,
    )
