import os, sys
from pathlib import Path
sys.path.insert(0, r'D:\Thuc_tap_MB\google-adk-skill-practice')
root = Path(r'D:\Thuc_tap_MB\google-adk-skill-practice')
for line in (root / '.env').read_text(encoding='utf-8').splitlines():
    line = line.strip()
    if line and not line.startswith('#') and '=' in line:
        k, v = line.split('=', 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
from app.services.ingestion.document_preparation import DocumentPreparation
from app.services.ingestion.semantic_placement import GeminiAtomicFactExtractor
ctx = DocumentPreparation().prepare(root / 'docs' / 'FLEXI_REWARDS_CREDIT_CARD_PRODUCT_BUSINESS_GUIDE_EN.md')
chunks = ctx.chunks[10:15]
payload = {
    'batchIndex': 2,
    'chunkIndexes': [c.index for c in chunks],
    'chunks': [c.model_dump(by_alias=True, mode='json') for c in chunks],
}
result = GeminiAtomicFactExtractor().extract_facts(
    batch_payload=payload,
    ontology_scope=ctx.ontology_context,
)
for f in result.facts:
    print(f.fact_id, f.context.get('role'), f.fact_shape, '|', f.subject, '|', f.predicate, '|', f.object)
