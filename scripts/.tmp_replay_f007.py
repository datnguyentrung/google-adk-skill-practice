import sys
sys.path.insert(0, r'D:\Thuc_tap_MB\google-adk-skill-practice')
import json
from app.services.ingestion.use_case import _get_validation_service
from app.services.ingestion.semantic_placement import (
    SemanticGraphMapper, DeterministicRepresentationSelector,
)
from app.core.schemas.ingestion.semantic_placement import AtomicFact

class DummyExtractor:
    def extract_facts(self, **kwargs):
        raise RuntimeError("unused")

v = _get_validation_service()
m = SemanticGraphMapper(
    registry=v.validator.registry,
    compiler=v.compiler,
    ontology_validator=v.validator,
    fact_extractor=DummyExtractor(),
    selector=DeterministicRepresentationSelector(),
)
cap = json.load(open('.e2e_run_out_en/batch_capture.json', encoding='utf-8'))
fact_json = next(x for x in cap['0'][-1]['facts']['facts'] if x['factId'] == 'f007')
fact = AtomicFact.model_validate(fact_json)
candidates = m._build_candidates(facts=[fact], graph_context=None)[fact.fact_id]
for c in sorted(candidates, key=lambda x: x.semantic_fit, reverse=True):
    print(round(c.semantic_fit, 3), c.kind, c.fallback_role, c.validity.passed,
          [e.edge_name for e in c.fragment.edges],
          [(n.class_name, [(p.property_name, p.value) for p in n.properties]) for n in c.fragment.nodes])
