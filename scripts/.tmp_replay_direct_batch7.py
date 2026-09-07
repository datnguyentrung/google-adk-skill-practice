import json
from pathlib import Path
from dotenv import load_dotenv
from app.core.schemas.ingestion.graph_patch import GraphPatchFragment
from app.services.ingestion.document_preparation import DocumentPreparation
from app.services.ingestion.graph_validation import GraphValidation
from app.services.ingestion.semantic_placement import SemanticGraphMapper
from app.services.ingestion.staged_ingestion import IngestionWorkspaceService

load_dotenv('.env')
capture=json.loads(Path('.e2e_run_out_en/batch_capture.json').read_text(encoding='utf-8'))
fragments=[]
for i in range(7):
    attempts=capture['batches'].get(str(i), capture['batches'].get(i, []))
    direct=[a for a in attempts if a.get('fragment')]
    fragments.append(GraphPatchFragment.model_validate(direct[-1]['fragment']))
merged=IngestionWorkspaceService.merge_fragments(fragments)
lines=[]
for n in merged.nodes:
    ident={p.property_name:p.value for p in n.properties if not isinstance(p.value,(dict,list))}
    lines += [f'- ref={n.temp_id}',f'  class={n.class_name}','  identity='+json.dumps(ident,ensure_ascii=False,sort_keys=True)]
lines += ['EDGES']+[f'- {e.edge_name}: {e.source_temp_id} -> {e.target_temp_id}' for e in merged.edges]
context='\n'.join(lines)
ctx=DocumentPreparation().prepare(Path('docs/FLEXI_REWARDS_CREDIT_CARD_PRODUCT_BUSINESS_GUIDE_EN.md'))
chunks=[c for c in ctx.chunks if 35 <= c.index <= 39]
payload={'batchIndex':7,'chunkIndexes':[c.index for c in chunks],'chunks':[c.model_dump(by_alias=True,mode='json') for c in chunks]}
v=GraphValidation(); m=SemanticGraphMapper(registry=v.validator.registry,compiler=v.compiler,ontology_validator=v.validator)
try:
    f=m.map_batch(batch_payload=payload,chunks=chunks,graph_context=context)
    print('OK',len(f.nodes),len(f.edges))
    print(json.dumps(f.model_dump(by_alias=True,mode='json'),ensure_ascii=False,indent=2))
except Exception as exc:
    print('ERROR',type(exc).__name__,str(exc))
    print(json.dumps(getattr(exc,'summary',{}),ensure_ascii=False,indent=2,default=str))