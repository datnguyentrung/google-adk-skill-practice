import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv
from google.genai import types
from app.core.schemas.ingestion.graph_patch import GraphPatchFragment
from app.services.ingestion.document_preparation import DocumentPreparation
from app.services.ingestion.graph_validation import GraphValidation
from app.services.ingestion.semantic_placement import SemanticGraphMapper
from app.services.ingestion.staged_ingestion import IngestionWorkspaceService
load_dotenv('.env')
capture=json.loads(Path('.e2e_run_out_en/batch_capture.json').read_text(encoding='utf-8'))
fragments=[GraphPatchFragment.model_validate(capture[str(i)][-1]['fragment']) for i in range(4)]
merged=IngestionWorkspaceService.merge_fragments(fragments)
lines=[]
for n in merged.nodes:
    ident={p.property_name:p.value for p in n.properties if not isinstance(p.value,(dict,list))}
    lines += [f'- ref={n.temp_id}',f'  class={n.class_name}','  identity='+json.dumps(ident,ensure_ascii=False,sort_keys=True)]
context='\n'.join(lines)
prepared=DocumentPreparation().prepare(Path('docs/FLEXI_REWARDS_CREDIT_CARD_PRODUCT_BUSINESS_GUIDE_EN.md'))
chunks=[c for c in prepared.chunks if 20<=c.index<=24]
payload={'batchIndex':4,'chunkIndexes':[c.index for c in chunks],'chunks':[c.model_dump(by_alias=True,mode='json') for c in chunks]}
v=GraphValidation(); m=SemanticGraphMapper(registry=v.validator.registry,compiler=v.compiler,ontology_validator=v.validator)
r=m.call_executor.generate_content(operation='debug_batch4',contents=m._prompt(batch_payload=payload,chunks=chunks,graph_context=context,previous_error=None),config=types.GenerateContentConfig(response_mime_type='application/json',response_json_schema=m._response_schema([20,21,22,23,24]),temperature=0))
print(json.dumps(getattr(r,'parsed',None),ensure_ascii=False,indent=2,default=str))
