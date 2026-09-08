import json
from pathlib import Path

from app.services.ingestion.document_preparation import DocumentPreparation

prep = DocumentPreparation().prepare(
    Path('docs/FLEXI_REWARDS_CREDIT_CARD_PRODUCT_BUSINESS_GUIDE_EN.md')
)
wanted = {7, 9, 15, 18, 19, 73}
for chunk in prep.chunks:
    if chunk.index in wanted:
        print(f'### CHUNK {chunk.index} | {chunk.section}')
        print(chunk.content)
        print()

capture = json.loads(
    Path('.e2e_run_out_en_adk4/batch_capture.json').read_text(encoding='utf-8')
)
