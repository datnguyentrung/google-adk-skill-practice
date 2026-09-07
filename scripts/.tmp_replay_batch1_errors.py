import json
from pathlib import Path
from dotenv import load_dotenv
from app.core.schemas.ingestion.graph_patch import GraphPatchFragment
from app.services.ingestion.document_preparation import DocumentPreparation
from app.services.ingestion.graph_validation import GraphValidation
from app.services.ingestion.semantic_placement import SemanticGraphMapper, DirectGraphMappingError
from app.services.ingestion.staged_ingestion import IngestionWorkspaceService
load_dotenv('.env')
cap=json.loads(Path('.e2e_run_out_en_adk/batch_capture.json').read_text(encoding='utf-8'))
f0=GraphPatchFragment.model_validate(cap['0'][-1]['fragment'])
merged=IngestionWorkspaceService.merge_fragments([f0])
lines=['Existing canonical graph:','Nodes:']
for n in merged.nodes:
    ident={p.property_name:p.value for p in n.properties if not isinstance(p.value,(dict,list))}
    lines += [f'- ref={n.temp_id}',f'  class={n.class_name}','  identity='+json.dumps(ident,ensure_ascii=False,sort_keys=True)]
context='\n'.join(lines)
prepared=DocumentPreparation().prepare(Path('docs/FLEXI_REWARDS_CREDIT_CARD_PRODUCT_BUSINESS_GUIDE_EN.md'))
chunks=[c for c in prepared.chunks if 5<=c.index<=9]
payload={'batchIndex':1,'chunkIndexes':[c.index for c in chunks],'contentChars':sum(len(c.content) for c in chunks),'chunks':[c.model_dump(by_alias=True,exclude_none=True) for c in chunks]}
v=GraphValidation(); m=SemanticGraphMapper(registry=v.validator.registry,compiler=v.compiler,ontology_validator=v.validator)
previous=None
for attempt in range(1,4):
    try:
        frag=m.map_batch(batch_payload=payload,chunks=chunks,graph_context=context,previous_error=previous)
        print(json.dumps({'attempt':attempt,'ok':True,'fragment':frag.model_dump(by_alias=True,mode='json')},ensure_ascii=False))
        break
    except DirectGraphMappingError as exc:
        print(json.dumps({'attempt':attempt,'ok':False,'summary':exc.summary},ensure_ascii=False))
        previous={'stage':'direct_graph_mapping','validation':exc.summary,'repairInstructions':'Correct only the reported deterministic validation defects.'}
