import asyncio
import json
import time
from pathlib import Path

from google.genai import types

from app.tools import ingestion_tools

DOC_NAME = "HƯỚNG DẪN NGHIỆP VỤ SẢN PHẨM THẺ TÍN DỤNG FLEXI REWARDS.md"
DOC_PATH = Path("docs") / DOC_NAME


class DryRunContext:
    def __init__(self):
        self.state = {}

    async def load_artifact(self, filename: str):
        assert filename == DOC_NAME
        return types.Part(text=DOC_PATH.read_text(encoding="utf-8"))

    async def save_artifact(self, **kwargs):
        return 0


async def main():
    context = DryRunContext()
    ingestion_tools._pace_next_model_turn = lambda _: time.sleep(12)
    result = await ingestion_tools.ingest_document_end_to_end(
        DOC_NAME,
        context,
        persist=False,
        max_retries_per_batch=3,
    )
    rendered = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    Path(".tmp_live_ingestion_dry_run_result.json").write_text(
        rendered,
        encoding="utf-8",
    )
    print(rendered)


if __name__ == "__main__":
    asyncio.run(main())
