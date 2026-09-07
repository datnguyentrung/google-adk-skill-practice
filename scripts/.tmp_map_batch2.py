import os, sys
from pathlib import Path
sys.path.insert(0, r'D:\Thuc_tap_MB\google-adk-skill-practice')
root = Path(r'D:\Thuc_tap_MB\google-adk-skill-practice')
for line in (root / '.env').read_text(encoding='utf-8').splitlines():
    if line.strip() and not line.lstrip().startswith('#') and '=' in line:
        k, v = line.split('=', 1); os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
from app.services.ingestion.document_preparation import DocumentPreparation
from app.services.ingestion.use_case import _get_semantic_graph_mapper
from app.services.ingestion.semantic_placement import FactRolePolicy
ctx = DocumentPreparation().prepare(root / 'docs' / 'FLEXI_REWARDS_CREDIT_CARD_PRODUCT_BUSINESS_GUIDE_EN.md')
chunks = ctx.chunks[10:15]
payload = {'batchIndex': 2, 'chunkIndexes': [c.index for c in chunks],
           'chunks': [c.model_dump(by_alias=True, mode='json') for c in chunks]}
m = _get_semantic_graph_mapper()
facts = m.fact_extractor.extract_facts(batch_payload=payload, ontology_scope=ctx.ontology_context)
graph_facts = FactRolePolicy.graph_candidates(facts.facts)
cands = m._build_candidates(facts=graph_facts, graph_context=None)
decisions = m.selector.select_batch(facts=graph_facts, candidates_by_fact=cands)
by_decision = {d.fact_id: d for d in decisions}
for f in graph_facts:
    d = by_decision.get(f.fact_id)
    if not d:
        print(f.fact_id, 'NO_DECISION', f.predicate); continue
    c = next(x for x in cands[f.fact_id] if x.candidate_id == d.selected_candidate_id)
    print(f.fact_id, f.predicate, '=>', [e.edge_name for e in c.fragment.edges],
          [(n.class_name, [(p.property_name, p.value) for p in n.properties]) for n in c.fragment.nodes])