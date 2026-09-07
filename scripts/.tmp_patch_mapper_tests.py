from pathlib import Path
import re

p = Path('tests/test_semantic_placement.py')
s = p.read_text(encoding='utf-8')
s = s.replace('from types import SimpleNamespace\n\n', '')
s = re.sub(
    r'class _Models:[\s\S]*?def _evidence',
    '''class _StructuredExecutor:\n    def __init__(self, payload):\n        self.payload = payload\n        self.calls = []\n\n    def run(self, **kwargs):\n        self.calls.append(kwargs)\n        return self.payload\n\n\ndef _mapper(payload):\n    validation = GraphValidation()\n    executor = _StructuredExecutor(payload)\n    mapper = SemanticGraphMapper(\n        registry=validation.validator.registry,\n        compiler=validation.compiler,\n        ontology_validator=validation.validator,\n        structured_executor=executor,\n    )\n    return mapper, executor\n\n\ndef _evidence''',
    s,
    count=1,
)
s = s.replace(
    '    config = client.models.calls[0]["config"]\n'
    '    schema = config.response_json_schema\n',
    '    schema = client.calls[0]["output_schema"]\n',
)
s = s.replace(
    '    assert config.response_mime_type == "application/json"\n',
    '    assert client.calls[0]["operation"] == "direct_graph_mapping"\n',
)
p.write_text(s, encoding='utf-8')
print('updated mapper tests for ADK executor')
