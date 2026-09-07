from pathlib import Path

p = Path('app/services/ingestion/semantic_placement.py')
s = p.read_text(encoding='utf-8')
s = s.replace(
    '                if text not in chunk.content and text not in (chunk.section or ""):\n'
    '                    text = _closest_source_excerpt(chunk, text) or text\n',
    '',
)
p.write_text(s, encoding='utf-8')
print('removed fuzzy evidence repair call')
