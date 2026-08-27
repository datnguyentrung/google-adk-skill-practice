"""Live compatibility probe for the GraphPatchFragment response schema.

Uses the same model, client, and GenerateContentConfig as the ingestion
extractor, with a minimal prompt. Succeeds only if the Gemini API accepts the
schema configuration and returns a valid GraphPatchFragment. Exit code 0 means
PROBE PASS; non-zero means the schema/configuration must be fixed before any
full ingestion run.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from google import genai
from google.genai import types

from app.core.schemas.ingestion.graph_patch import GraphPatchFragment


def _load_dotenv() -> None:
    env_path = Path(".env")
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def main() -> int:
    _load_dotenv()
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        print("PROBE FAILED: GOOGLE_API_KEY is not set")
        return 2
    model = os.getenv(
        "INGESTION_ORCHESTRATOR_MODEL",
        os.getenv("GOOGLE_ADK_MODEL", "gemini-3.1-flash-lite"),
    )
    client = genai.Client(api_key=api_key)
    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_json_schema=GraphPatchFragment.model_json_schema(),
        temperature=0,
    )
    prompt = (
        'Return the minimal valid GraphPatchFragment for a batch whose only '
        'chunkIndex is 0: nodes=[], edges=[], coverage=[{"chunkIndex": 0, '
        '"decision": "NO_RELEVANT_FACT", "reason": "No facts in this chunk"}], '
        "warnings=[]."
    )
    try:
        response = client.models.generate_content(
            model=model,
            contents=prompt,
            config=config,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"PROBE FAILED: {type(exc).__name__}: {exc}")
        return 1
    try:
        raw = getattr(response, "parsed", None)
        if raw is None:
            raw = json.loads(getattr(response, "text", "") or "{}")
        fragment = GraphPatchFragment.model_validate(raw)
    except Exception as exc:  # noqa: BLE001
        print(
            "PROBE FAILED: response did not validate as GraphPatchFragment: "
            f"{type(exc).__name__}: {exc}"
        )
        return 1
    print("PROBE PASS")
    print(
        json.dumps(
            fragment.model_dump(by_alias=True, mode="json"),
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
