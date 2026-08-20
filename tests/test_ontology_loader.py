from app.services.ingestion.loader import OntologyLoader

ONTOLOGY_PATH = (
    "app/data/ontology/product_sales_knowledge_graph_base_v3_1.ontology.json"
)


def test_load_ontology():
    ontology = OntologyLoader.load(ONTOLOGY_PATH)

    assert ontology.summary.classes == 15
    assert ontology.summary.edges == 36
    assert ontology.summary.attributes == 82
