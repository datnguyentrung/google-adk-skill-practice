import json
from pathlib import Path
from dotenv import load_dotenv

load_dotenv('.env')

from app.core.schemas.ingestion.graph_patch import GraphPatchFragment
from app.services.ingestion.document_preparation import DocumentPreparation
from app.services.ingestion import use_case as uc

SOURCE = Path('docs/FLEXI_REWARDS_CREDIT_CARD_PRODUCT_BUSINESS_GUIDE_EN.md')
OUT = Path('.e2e_run_out_en_adk4')
prep = DocumentPreparation().prepare(SOURCE)
capture = json.loads((OUT / 'batch_capture.json').read_text(encoding='utf-8'))
attempts = json.loads((OUT / 'final_repair_attempts.json').read_text(encoding='utf-8'))
replacement = {item['batchIndex']: item['fragment'] for item in attempts}
fragments = []
for batch_index in range(19):
    payload = replacement.get(batch_index, capture[str(batch_index)][-1]['fragment'])
    fragments.append(GraphPatchFragment.model_validate(payload))
merged = uc._get_workspace_service().merge_fragments(fragments)
patch = uc.GraphPatchDraft.model_validate(merged.model_dump(by_alias=True, mode='json'))
assessment = uc._get_validation_service().assess(patch, 'targeted-repair', prep.chunks)
result = assessment.result.model_dump(by_alias=True, mode='json')
print(json.dumps({
    'validForExtraction': result['validForExtraction'],
    'validForPersistence': result['validForPersistence'],
    'errors': result['errors'],
    'readinessIssues': result['readinessIssues'],
    'warnings': result['warnings'],
    'nodeCount': result['nodeCount'],
    'edgeCount': result['edgeCount'],
}, ensure_ascii=False, indent=2))
(OUT / 'targeted_repair_validation.json').write_text(
    json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8'
)
