import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv
from app.core.schemas.ingestion.graph_patch import GraphPatchFragment
from app.services.ingestion.document_preparation import DocumentPreparation
from app.services.ingestion.graph_validation import GraphValidation
from app.services.ingestion.semantic_placement import SemanticGraphMapper
from app.services.ingestion.staged_ingestion import IngestionWorkspaceService
load_dotenv('.env')
cap=json.loads(Path('.e2e_run_out_en/batch_capture.json').read_text(encoding='utf-8'))
frags=[GraphPatchFragment.model_validate(cap[str(i)][-1]['fragment']) for i in range(4)]
merged=IngestionWorkspaceService.merge_fragments(frags)
lines=[]
for n in merged.nodes:
    ident={p.property_name:p.value for p in n.properties if not isinstance(p.value,(dict,list))}
    lines += [f'- ref={n.temp_id}', f'  class={n.class_name}', '  identity='+json.dumps(ident,ensure_ascii=False,sort_keys=True)]
context='\n'.join(lines)
prepared=DocumentPreparation().prepare(Path('docs/FLEXI_REWARDS_CREDIT_CARD_PRODUCT_BUSINESS_GUIDE_EN.md'))
chunks=[c for c in prepared.chunks if 20 <= c.index <= 24]
payload={'batchIndex':4,'chunkIndexes':[c.index for c in chunks],'chunks':[c.model_dump(by_alias=True,mode='json') for c in chunks]}
v=GraphValidation()
m=SemanticGraphMapper(registry=v.validator.registry,compiler=v.compiler,ontology_validator=v.validator)
try:
    frag=m.map_batch(batch_payload=payload,chunks=chunks,graph_context=context)
    print(json.dumps({'ok':True,'nodes':len(frag.nodes),'edges':len(frag.edges),'coverage':[x.model_dump(by_alias=True) for x in frag.coverage]},ensure_ascii=False,indent=2))
except Exception as exc:
    print(json.dumps({'ok':False,'type':type(exc).__name__,'error':str(exc),'summary':getattr(exc,'summary',{})},ensure_ascii=False,indent=2,default=str))
# production-style local repair loop
previous=None
for attempt in range(1,4):
    try:
        frag=m.map_batch(batch_payload=payload,chunks=chunks,graph_context=context,previous_error=previous)
        print(json.dumps({'retryOk':True,'attempt':attempt,'nodes':len(frag.nodes),'edges':len(frag.edges),'coverage':[x.model_dump(by_alias=True) for x in frag.coverage]},ensure_ascii=False,indent=2))
        break
    except Exception as exc:
        previous={'stage':'direct_graph_mapping','message':str(exc),'validation':getattr(exc,'summary',{}),'repairInstructions':'Correct only the reported schema, coverage, ontology, edge-direction, or verbatim evidence defects.'}
        print(json.dumps({'retryOk':False,'attempt':attempt,'type':type(exc).__name__,'summary':getattr(exc,'summary',{})},ensure_ascii=False,default=str))
