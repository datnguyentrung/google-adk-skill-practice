from pathlib import Path
import re

p = Path('app/services/ingestion/semantic_placement.py')
s = p.read_text(encoding='utf-8')
s = re.sub(
    r'        ontology_validator: OntologyValidator,\n        model: str = DEFAULT_SEMANTIC_MAPPING_MODEL,\n        client: genai\.Client \| None = None,\n    \):\n        self\.registry = registry\n        self\.compiler = compiler\n        self\.ontology_validator = ontology_validator\n        self\.model = model\n        injected_client = client is not None\n        self\.client = client or genai\.Client\(api_key=os\.getenv\("GOOGLE_API_KEY"\)\)\n        self\.call_executor = GeminiCallExecutor\(\n            client=self\.client,\n            model=self\.model,\n            \*\*\(\{"rpm_budget": 0\} if injected_client else \{}\),\n        \)\n',
    '        ontology_validator: OntologyValidator,\n        model: str = DEFAULT_SEMANTIC_MAPPING_MODEL,\n        structured_executor: AdkStructuredCallExecutor | None = None,\n    ):\n        self.registry = registry\n        self.compiler = compiler\n        self.ontology_validator = ontology_validator\n        self.model = model\n        self.structured_executor = structured_executor or AdkStructuredCallExecutor(\n            model=self.model\n        )\n',
    s,
    count=1,
)
s = re.sub(
    r'        response = self\.call_executor\.generate_content\(\n            operation="direct_graph_mapping",\n            contents=prompt,\n            config=types\.GenerateContentConfig\(\n                response_mime_type="application/json",\n                response_json_schema=schema,\n                temperature=0,\n            \),\n        \)\n        payload = getattr\(response, "parsed", None\)\n        if payload is None:\n            text = getattr\(response, "text", None\)\n            if not text:\n                raise DirectGraphMappingError\("Gemini returned no graph fragment"\)\n            try:\n                payload = json\.loads\(text\)\n            except json\.JSONDecodeError as exc:\n                raise DirectGraphMappingError\(\n                    "Gemini returned invalid graph JSON",\n                    summary=\{"line": exc\.lineno, "column": exc\.colno\},\n                \) from exc\n',
    '        payload = self.structured_executor.run(\n            operation="direct_graph_mapping",\n            instruction=prompt,\n            output_schema=schema,\n            message="Map this batch and return the GraphPatchFragment.",\n        )\n',
    s,
    count=1,
)
p.write_text(s, encoding='utf-8')
print('patched ADK mapper transport')
