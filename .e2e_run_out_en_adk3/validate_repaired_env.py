import json
from pathlib import Path
from dotenv import load_dotenv

load_dotenv('.env')

from app.core.schemas.ingestion.graph_patch import GraphPatchDraft, GraphPatchFragment
from app.services.ingestion.document_preparation import DocumentPreparation
from app.services.ingestion.graph_validation import GraphValidation
from app.services.ingestion.staged_ingestion import IngestionWorkspaceService

cap = json.loads(Path('.e2e_run_out_en_adk3/batch_capture.json').read_text(encoding='utf-8'))
frags = [GraphPatchFragment.model_validate(cap[str(i)][-1]['fragment']) for i in range(19)]
frags[5] = GraphPatchFragment.model_validate_json(Path('.e2e_run_out_en_adk3/b5_repaired_complete.json').read_text(encoding='utf-8'))
frags[6] = GraphPatchFragment.model_validate_json(Path('.e2e_run_out_en_adk3/b6_repaired_final.json').read_text(encoding='utf-8'))
merged = IngestionWorkspaceService.merge_fragments(frags)
prep = DocumentPreparation().prepare(Path('docs/FLEXI_REWARDS_CREDIT_CARD_PRODUCT_BUSINESS_GUIDE_EN.md'))
validator = GraphValidation()
print('semantic_value_judge=', type(validator.source_grounding.semantic_value_judge).__name__ if validator.source_grounding.semantic_value_judge else 'none')
draft = GraphPatchDraft.model_validate(merged.model_dump(by_alias=True, mode='json'))
assessment = validator.assess(draft, 'manual-repaired-e2e-env', prep.chunks)
out = {
    'validForExtraction': assessment.result.valid_for_extraction,
    'validForPersistence': assessment.result.valid_for_persistence,
    'errors': [e.model_dump(by_alias=True, exclude_none=True) for e in assessment.result.errors],
    'readinessIssues': [e.model_dump(by_alias=True, exclude_none=True) for e in assessment.result.readiness_issues],
    'warnings': [e.model_dump(by_alias=True, exclude_none=True) for e in assessment.result.warnings],
    'nodes': len(merged.nodes),
    'edges': len(merged.edges),
    'compiledNodes': len(assessment.compiled_patch.nodes) if assessment.compiled_patch else None,
    'compiledEdges': len(assessment.compiled_patch.edges) if assessment.compiled_patch else None,
}
print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
Path('.e2e_run_out_en_adk3/repaired_validation_env.json').write_text(
    json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding='utf-8'
)
