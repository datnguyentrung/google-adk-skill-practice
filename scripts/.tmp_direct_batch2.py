from pathlib import Path
from app.services.ingestion.document_preparation import DocumentPreparation
from app.services.ingestion.graph_validation import GraphValidation
from app.services.ingestion.semantic_placement import SemanticGraphMapper

source = Path('docs/FLEXI_REWARDS_CREDIT_CARD_PRODUCT_BUSINESS_GUIDE_EN.md')
prep = DocumentPreparation()
ctx = prep.prepare(source)
chunks = [c for c in ctx.chunks if 10 <= c.index <= 14]
validation = GraphValidation()
mapper = SemanticGraphMapper(
    registry=validation.validator.registry,
    compiler=validation.compiler,
    ontology_validator=validation.validator,
)
payload = {
    'batchIndex': 2,
    'chunkIndexes': [c.index for c in chunks],
    'chunks': [c.model_dump(by_alias=True, mode='json') for c in chunks],
}
fragment = mapper.map_batch(batch_payload=payload, chunks=chunks)
print('NODES', len(fragment.nodes), 'EDGES', len(fragment.edges))
for node in fragment.nodes:
    print(node.class_name, node.temp_id, [(p.property_name, p.value) for p in node.properties])
for edge in fragment.edges:
    print(edge.edge_name, edge.source_temp_id, '->', edge.target_temp_id)
print('COVERAGE', [(c.chunk_index, c.decision) for c in fragment.coverage])