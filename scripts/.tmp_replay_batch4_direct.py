import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv
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
for node in merged.nodes:
    identity={p.property_name:p.value for p in node.properties if not isinstance(p.value,(dict,list))}
    lines += [f'- ref={node.temp_id}', f'  class={node.class_name}', '  identity='+json.dumps(identity,ensure_ascii=False,sort_keys=True)]
lines += ['EDGES']+[f'- {e.edge_name}: {e.source_temp_id} -> {e.target_temp_id}' for e in merged.edges]
context='\n'.join(lines)
prepared=DocumentPreparation().prepare(Path('docs/FLEXI_REWARDS_CREDIT_CARD_PRODUCT_BUSINESS_GUIDE_EN.md'))
chunks=[c for c in prepared.chunks if 20 <= c.index <= 24]
payload={'batchIndex':4,'chunkIndexes':[c.index for c in chunks],'chunks':[c.model_dump(by_alias=True,mode='json') for c in chunks]}
v=GraphValidation(); mapper=SemanticGraphMapper(registry=v.validator.registry,compiler=v.compiler,ontology_validator=v.validator)
try:
    fragment=mapper.map_batch(batch_payload=payload,chunks=chunks,graph_context=context)
    print('OK',len(fragment.nodes),len(fragment.edges)); print(json.dumps(fragment.model_dump(by_alias=True,mode='json'),ensure_ascii=False,indent=2))
except Exception as exc:
    print('ERROR',type(exc).__name__,str(exc)); print(json.dumps(getattr(exc,'summary',{}),ensure_ascii=False,indent=2,default=str))
