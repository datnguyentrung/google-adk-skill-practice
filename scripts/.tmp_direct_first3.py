import json
from pathlib import Path
from dotenv import load_dotenv
from app.services.ingestion.document_preparation import DocumentPreparation
from app.services.ingestion.graph_validation import GraphValidation
from app.services.ingestion.semantic_placement import SemanticGraphMapper
from app.services.ingestion.staged_ingestion import IngestionWorkspaceService

load_dotenv('.env')
ctx = DocumentPreparation().prepare(Path('docs/FLEXI_REWARDS_CREDIT_CARD_PRODUCT_BUSINESS_GUIDE_EN.md'))
validation = GraphValidation()
mapper = SemanticGraphMapper(registry=validation.validator.registry, compiler=validation.compiler, ontology_validator=validation.validator)
fragments = []

def context():
    if not fragments:
        return ''
    merged = IngestionWorkspaceService.merge_fragments(fragments)
    lines=[]
    for n in merged.nodes:
        ident={p.property_name:p.value for p in n.properties if not isinstance(p.value,(dict,list))}
        lines += [f'- ref={n.temp_id}', f'  class={n.class_name}', '  identity='+json.dumps(ident,ensure_ascii=False,sort_keys=True)]
    lines += ['EDGES']
    lines += [f'- {e.edge_name}: {e.source_temp_id} -> {e.target_temp_id}' for e in merged.edges]
    return '\n'.join(lines)

for batch_index, start in enumerate((0,5,10)):
    chunks=[c for c in ctx.chunks if start <= c.index <= start+4]
    payload={'batchIndex':batch_index,'chunkIndexes':[c.index for c in chunks],'chunks':[c.model_dump(by_alias=True,mode='json') for c in chunks]}
    try:
        frag=mapper.map_batch(batch_payload=payload,chunks=chunks,graph_context=context())
    except Exception as exc:
        print('ERROR', type(exc).__name__, str(exc), getattr(exc,'summary',{}))
        raise
    fragments.append(frag)
    print('BATCH',batch_index,'nodes',len(frag.nodes),'edges',len(frag.edges))
    print(' classes',sorted({n.class_name for n in frag.nodes}))
    print(' edges',sorted({e.edge_name for e in frag.edges}))
    print(' props',sorted({p.property_name for n in frag.nodes for p in n.properties}))
print('MERGED',len(IngestionWorkspaceService.merge_fragments(fragments).nodes),len(IngestionWorkspaceService.merge_fragments(fragments).edges))