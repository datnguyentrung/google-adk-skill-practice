import json
from pathlib import Path
from types import SimpleNamespace
from dotenv import load_dotenv

load_dotenv('.env')

from app.core.schemas.ingestion.graph_patch import GraphPatchFragment
from app.services.ingestion.document_preparation import DocumentPreparation
from app.services.ingestion import use_case as uc

SOURCE = Path('docs/FLEXI_REWARDS_CREDIT_CARD_PRODUCT_BUSINESS_GUIDE_EN.md')
OUT = Path('.e2e_run_out_en_adk4')
prep = DocumentPreparation().prepare(SOURCE)
context = SimpleNamespace(state={uc.ARTIFACT_DIGEST_STATE_KEY: 'targeted-adk4-replay'})
workspace = uc._get_workspace_service().begin(
    artifact_name=SOURCE.name,
    provenance=uc._current_provenance(context),
    chunks=prep.chunks,
)
capture = json.loads((OUT / 'batch_capture.json').read_text(encoding='utf-8'))
for batch in workspace.batches:
    batch.fragment = GraphPatchFragment.model_validate(
        capture[str(batch.index)][-1]['fragment']
    )
uc._store_workspace(context, workspace)
finalized = json.loads((OUT / 'raw_result.json').read_text(encoding='utf-8'))
mapper = uc._get_graph_mapper()
orig_map_batch = mapper.map_batch
attempts = []

def capture_map_batch(**kwargs):
    fragment = orig_map_batch(**kwargs)
    item = {
        'batchIndex': kwargs['batch_payload'].get('batchIndex'),
        'previousError': kwargs.get('previous_error'),
        'coverage': [
            {'chunkIndex': x.chunk_index, 'decision': x.decision, 'reason': x.reason}
            for x in fragment.coverage
        ],
        'fragment': fragment.model_dump(by_alias=True, mode='json', exclude_none=True),
    }
    attempts.append(item)
    (OUT / 'final_repair_attempts.json').write_text(
        json.dumps(attempts, ensure_ascii=False, indent=2), encoding='utf-8'
    )
    print('ATTEMPT', len(attempts), 'BATCH', item['batchIndex'], 'COVERAGE', item['coverage'])
    return fragment

mapper.map_batch = capture_map_batch
repaired = uc._repair_final_coverage_batches(
    ingestion_id=workspace.ingestion_id,
    tool_context=context,
    mapper=mapper,
    finalized=finalized,
    max_retries_per_batch=3,
)
print('repair_returned=', repaired)
