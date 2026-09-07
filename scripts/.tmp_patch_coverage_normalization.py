from pathlib import Path
p=Path('app/services/ingestion/semantic_placement.py')
s=p.read_text(encoding='utf-8')
old='''        edges = [\n            edge.model_copy(update={"evidence": normalize(edge.evidence)})\n            for edge in fragment.edges\n        ]\n        return fragment.model_copy(update={"nodes": nodes, "edges": edges})\n'''
new='''        edges = [\n            edge.model_copy(update={"evidence": normalize(edge.evidence)})\n            for edge in fragment.edges\n        ]\n        cited_chunks = {\n            evidence.chunk_index\n            for node in nodes\n            for evidence in node.evidence\n        } | {\n            evidence.chunk_index\n            for node in nodes\n            for prop in node.properties\n            for evidence in prop.evidence\n        } | {\n            evidence.chunk_index\n            for edge in edges\n            for evidence in edge.evidence\n        }\n        coverage = []\n        for item in fragment.coverage:\n            if item.chunk_index in cited_chunks and item.decision != "MAPPED":\n                item = item.model_copy(update={"decision": "MAPPED", "reason": "Graph evidence emitted for this chunk"})\n            elif item.chunk_index not in cited_chunks and item.decision == "MAPPED":\n                item = item.model_copy(update={"decision": "AMBIGUOUS", "reason": "Mapper marked MAPPED but emitted no graph evidence for this chunk"})\n            coverage.append(item)\n        return fragment.model_copy(update={"nodes": nodes, "edges": edges, "coverage": coverage})\n'''
if old not in s: raise SystemExit('canonicalize block not found')
s=s.replace(old,new)
p.write_text(s,encoding='utf-8')
print('coverage normalized from graph evidence')
s=p.read_text(encoding='utf-8')
needle='''- Preserve dates and numeric values in ontology-compatible JSON datatypes/formats.\\n\\n'''
replacement='''- Before returning, audit coverage against your own graph output: every MAPPED chunkIndex must appear in at least one emitted node/property/edge evidence item; every chunk that contributes no graph evidence must use a non-MAPPED decision.\\n- Preserve dates and numeric values in ontology-compatible JSON datatypes/formats.\\n\\n'''
if needle not in s: raise SystemExit('prompt anchor not found')
s=s.replace(needle,replacement)
p.write_text(s,encoding='utf-8')
print('added final coverage self-audit')
