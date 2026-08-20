import json
from pathlib import Path

from app.core.schemas.ingestion.models import OntologyDefinition


class OntologyLoader:
    # Load ontology từ file JSON
    @staticmethod
    def load(path: str | Path) -> OntologyDefinition:
        ontology_path = Path(path)

        # 1. Kiểm tra sự tồn tại của file
        if not ontology_path.exists():
            raise FileNotFoundError(f"Ontology file not found: {ontology_path}")

        # 2. Kiểm tra file có phải là file không
        if not ontology_path.is_file():
            raise ValueError(f"Ontology file is not a file: {ontology_path}")

        # 3. Đọc file
        with open(ontology_path, "r", encoding="utf-8") as f:
            raw_data = json.load(f)

        # 4. Validate dữ liệu và trả về OntologyDefinition
        return OntologyDefinition.model_validate(raw_data)
