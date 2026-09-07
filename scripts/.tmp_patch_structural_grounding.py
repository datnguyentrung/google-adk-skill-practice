from pathlib import Path
p=Path('app/services/ingestion/semantic_placement.py')
s=p.read_text(encoding='utf-8')
s=s.replace('''                text = _canonical_table_excerpt(chunk, item.text)\n                result.append(''','''                text = _canonical_table_excerpt(chunk, item.text)\n                text = _canonical_whitespace_excerpt(chunk, text)\n                result.append(''')
s=s.replace('''        fact_chunks = {\n            evidence.chunk_index\n            for node in fragment.nodes\n            for prop in node.properties\n            for evidence in prop.evidence\n        } | {''','''        fact_chunks = {\n            evidence.chunk_index\n            for node in fragment.nodes\n            for evidence in node.evidence\n        } | {\n            evidence.chunk_index\n            for node in fragment.nodes\n            for prop in node.properties\n            for evidence in prop.evidence\n        } | {''')
s=s.replace('''f"Chunk {item.chunk_index} is MAPPED but no property or "\n                            "edge fact cites that chunk"''','''f"Chunk {item.chunk_index} is MAPPED but no node, property, "\n                            "or edge cites that chunk"''')
p.write_text(s,encoding='utf-8')
print('patched structural coverage/evidence')
s=p.read_text(encoding='utf-8')
marker='''def _canonical_table_excerpt(chunk: DocumentChunk, quote: str) -> str:\n'''
helper='''def _canonical_whitespace_excerpt(chunk: DocumentChunk, quote: str) -> str:\n    if not quote.strip():\n        return quote\n    parts = [re.escape(part) for part in re.split(r"\\s+", quote.strip()) if part]\n    if not parts:\n        return quote\n    pattern = r"\\s+".join(parts)\n    for surface in (chunk.section or "", chunk.content):\n        match = re.search(pattern, surface, flags=re.MULTILINE)\n        if match is not None:\n            return match.group(0)\n    return quote\n\n\n'''
if helper not in s:
    s=s.replace(marker,helper+marker)
p.write_text(s,encoding='utf-8')
print('added whitespace-only canonicalizer')
