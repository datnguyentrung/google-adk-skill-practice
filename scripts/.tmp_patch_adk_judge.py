from pathlib import Path
import re

p = Path('app/services/ingestion/graph_validation.py')
s = p.read_text(encoding='utf-8')
s = re.sub(
    r'    def __init__\(\n        self,\n        \*,\n        model: str = DEFAULT_SEMANTIC_GROUNDING_MODEL,\n        client: genai\.Client \| None = None,\n    \):\n        self\.model = model\n        injected_client = client is not None\n        self\.client = client or genai\.Client\(api_key=os\.getenv\("GOOGLE_API_KEY"\)\)\n        self\.call_executor = GeminiCallExecutor\(\n            client=self\.client,\n            model=self\.model,\n            \*\*\(\{"rpm_budget": 0\} if injected_client else \{}\),\n        \)\n',
    '    def __init__(\n        self,\n        *,\n        model: str = DEFAULT_SEMANTIC_GROUNDING_MODEL,\n        structured_executor: AdkStructuredCallExecutor | None = None,\n    ):\n        self.model = model\n        self.structured_executor = structured_executor or AdkStructuredCallExecutor(\n            model=self.model\n        )\n',
    s,
    count=1,
)
s = re.sub(
    r'        try:\n            from google\.genai import types\n\n            response = self\.call_executor\.generate_content\([\s\S]*?            model_response = _JudgeResponse\.model_validate\(payload\)\n',
    '        try:\n            payload = self.structured_executor.run(\n                operation=operation,\n                instruction=prompt,\n                output_schema=_JudgeResponse.model_json_schema(by_alias=True),\n                message="Judge the supplied evidence and return the structured verdict.",\n            )\n            model_response = _JudgeResponse.model_validate(payload)\n',
    s,
    count=1,
)
p.write_text(s, encoding='utf-8')
print('patched grounding judge to ADK structured executor')
