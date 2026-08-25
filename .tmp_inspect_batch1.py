import json
from pathlib import Path

from app.services.ingestion.prepare_extraction_context import ExtractionContextService
from app.services.ingestion.orchestrator import GeminiBatchExtractor
from app.tools.ingestion_tools import _compact_ontology_context

DOC = next(Path("docs").glob("*FLEXI REWARDS.md"))
context = ExtractionContextService().prepare(DOC)
chunks = [c for c in context.chunks if c.index in {5, 6, 7, 8, 9}]
payload = {
    "batchIndex": 1,
    "chunkIndexes": [c.index for c in chunks],
    "contentChars": sum(len(c.content) for c in chunks),
    "chunks": [c.model_dump(by_alias=True, exclude_none=True) for c in chunks],
}
fragment = GeminiBatchExtractor().extract_fragment(
    batch_payload=payload,
    ontology_catalog=_compact_ontology_context(context.ontology_context),
)
out = fragment.model_dump(by_alias=True, mode="json")
Path(".batch1_fragment.json").write_text(
    json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
)
chunk_by_index = {c.index: c for c in chunks}
for ni, node in enumerate(fragment.nodes):
    for pi, prop in enumerate(node.properties):
        for ei, ev in enumerate(prop.evidence):
            ok = ev.text in chunk_by_index[ev.chunk_index].content
            print(ni, pi, prop.property_name, ev.chunk_index, ok, repr(ev.text))
