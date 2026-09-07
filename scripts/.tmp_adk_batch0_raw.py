import json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv
from app.services.ingestion.document_preparation import DocumentPreparation
from app.services.ingestion.graph_validation import GraphValidation
from app.services.ingestion.semantic_placement import SemanticGraphMapper
load_dotenv('.env')
prepared=DocumentPreparation().prepare(Path('docs/FLEXI_REWARDS_CREDIT_CARD_PRODUCT_BUSINESS_GUIDE_EN.md'))
chunks=[c for c in prepared.chunks if 0<=c.index<=4]
payload={'batchIndex':0,'chunkIndexes':[c.index for c in chunks],'chunks':[c.model_dump(by_alias=True,mode='json') for c in chunks]}
v=GraphValidation(); m=SemanticGraphMapper(registry=v.validator.registry,compiler=v.compiler,ontology_validator=v.validator)
raw=m.structured_executor.run(operation='debug_batch0_raw',instruction=m._prompt(batch_payload=payload,chunks=chunks,graph_context='',previous_error=None),output_schema=m._response_schema([0,1,2,3,4]),message='Map this batch and return the GraphPatchFragment.')
print(json.dumps(raw,ensure_ascii=False,indent=2,default=str))
from app.core.schemas.ingestion.graph_patch import GraphPatchFragment
frag=GraphPatchFragment.model_validate(raw)
frag=m._resolve_context_nodes(frag,'',0)
frag=m._canonicalize_evidence(frag,chunks)
print('VALIDATION',json.dumps(m._validate_fragment(frag,chunks),ensure_ascii=False,indent=2,default=str))
