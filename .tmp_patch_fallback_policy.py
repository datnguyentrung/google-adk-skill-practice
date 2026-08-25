from pathlib import Path
p=Path(r"D:\Thuc_tap_MB\google-adk-skill-practice\app\services\ingestion\partial_batch_fallback.py")
s=p.read_text(encoding='utf-8')
anchor='from app.services.ingestion.source_grounding import SourceGroundingValidator\n\n\n'
insert='''from app.services.ingestion.source_grounding import SourceGroundingValidator\n\nCRITICAL_NON_SKIPPABLE_PROPERTIES = {\n    "pskg:productCode",\n    "pskg:bankingProductStatus",\n    "pskg:bankingProductEffectiveFrom",\n}\nCRITICAL_NON_SKIPPABLE_EDGES = {"pskg:hasEligibilityRule"}\n\n\n'''
assert anchor in s
s=s.replace(anchor,insert,1)
p.write_text(s,encoding='utf-8')p=Path(r"D:\Thuc_tap_MB\google-adk-skill-practice\app\services\ingestion\partial_batch_fallback.py")
s=p.read_text(encoding='utf-8')
anchor='\ndef prune_fragment_for_skips(\n'
fn='''\ndef can_skip_chunks_safely(\n    fragment: GraphPatchFragment,\n    skipped_indexes: set[int],\n) -> bool:\n    for node in fragment.nodes:\n        for prop in node.properties:\n            if prop.property_name not in CRITICAL_NON_SKIPPABLE_PROPERTIES:\n                continue\n            if any(item.chunk_index in skipped_indexes for item in prop.evidence):\n                return False\n    for edge in fragment.edges:\n        if edge.edge_name not in CRITICAL_NON_SKIPPABLE_EDGES:\n            continue\n        if any(item.chunk_index in skipped_indexes for item in edge.evidence):\n            return False\n    return True\n\n\n'''
assert anchor in s
s=s.replace(anchor,fn+anchor,1)
s=s.replace('''__all__ = [\n    "failed_chunk_indexes",\n    "prune_fragment_for_skips",\n]\n''','''__all__ = [\n    "can_skip_chunks_safely",\n    "failed_chunk_indexes",\n    "prune_fragment_for_skips",\n]\n''')
p.write_text(s,encoding='utf-8')
print('POLICY_OK')