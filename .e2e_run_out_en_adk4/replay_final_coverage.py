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
provenance = uc._current_provenance(context)
workspace = uc._get_workspace_service().begin(
    artifact_name=SOURCE.name,
    provenance=provenance,
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
repaired = uc._repair_final_coverage_batches(
    ingestion_id=workspace.ingestion_id,
    tool_context=context,
    mapper=mapper,
    finalized=finalized,
    max_retries_per_batch=3,
)
print('repair_returned=', repaired)
result = uc.finalize_ingestion(workspace.ingestion_id, context)
print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
current = uc._load_workspace(context)
for batch_index in (1, 3, 14):
    fragment = current.batches[batch_index].fragment
    (OUT / f'targeted_repaired_batch_{batch_index}.json').write_text(
        fragment.model_dump_json(by_alias=True, indent=2), encoding='utf-8'
    )
(OUT / 'targeted_repaired_final.json').write_text(
    json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding='utf-8'
)
