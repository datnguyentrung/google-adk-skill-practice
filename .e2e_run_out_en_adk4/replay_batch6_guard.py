from __future__ import annotations

import json
from pathlib import Path

from dotenv import load_dotenv

load_dotenv('.env')

from app.services.ingestion.adk_graph_mapper import AdkGraphMapper, DirectGraphMappingError
from app.services.ingestion.document_preparation import DocumentPreparation
from app.services.ingestion.graph_validation import GraphValidation
from app.services.ingestion.use_case import _extractor_retry_error

source = Path('docs/FLEXI_REWARDS_CREDIT_CARD_PRODUCT_BUSINESS_GUIDE_EN.md')
context = DocumentPreparation().prepare_uploaded_document(
    filename=source.name,
    data=source.read_bytes(),
    mime_type='text/markdown',
)
chunks = [chunk for chunk in context.chunks if 30 <= chunk.index <= 34]
batch_payload = {
    'batchIndex': 6,
    'chunkIndexes': [chunk.index for chunk in chunks],
    'contentChars': sum(len(chunk.content) for chunk in chunks),
    'chunks': [chunk.model_dump(by_alias=True, exclude_none=True) for chunk in chunks],
}
graph_context = (
    'Existing canonical graph:\n'
    'Nodes:\n'
    '- ref=product-flexi\n'
    '  class=pskg:BankingProduct\n'
    '  identity={"pskg:productCode":"CC-FLEXI-001"}'
)
validation = GraphValidation()
mapper = AdkGraphMapper(
    registry=validation.validator.registry,
    compiler=validation.compiler,
    ontology_validator=validation.validator,
)
print('model=', mapper.model)
previous_error = None
out_dir = Path('.e2e_run_out_en_adk4')
for attempt in range(1, 4):
    print(f'attempt={attempt}')
    try:
        fragment = mapper.map_batch(
            batch_payload=batch_payload,
            chunks=chunks,
            graph_context=graph_context,
            previous_error=previous_error,
        )
    except DirectGraphMappingError as exc:
        summary = exc.summary
        print('errors=', json.dumps(summary.get('errors', []), ensure_ascii=False))
        candidate = summary.get('candidateFragment')
        if candidate is not None:
            (out_dir / f'batch6_guard_attempt_{attempt}_candidate.json').write_text(
                json.dumps(candidate, ensure_ascii=False, indent=2), encoding='utf-8'
            )
        previous_error = _extractor_retry_error(exc)
        continue

    payload = fragment.model_dump(by_alias=True, mode='json', exclude_none=True)
    (out_dir / 'batch6_guard_success.json').write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8'
    )
    print('success nodes=', len(fragment.nodes), 'edges=', len(fragment.edges))
    for node in fragment.nodes:
        if node.class_name == 'pskg:BusinessRule':
            print('rule=', node.temp_id)
            incoming = [
                edge.edge_name
                for edge in fragment.edges
                if edge.target_temp_id == node.temp_id
            ]
            print('  incoming=', incoming)
    break
else:
    raise SystemExit('batch 6 did not produce a guard-valid fragment')
