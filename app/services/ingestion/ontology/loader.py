"""Phase 0 — Nạp định nghĩa ontology từ file JSON.

Module này là điểm vào duy nhất để đọc file ontology của Product Sales
Knowledge Graph và chuyển thành `OntologyDefinition` đã được pydantic kiểm
tra. Mọi phase sau (registry, validation, persistence) đều dùng lại kết quả
này thay vì tự đọc file."""

import json
from pathlib import Path
from app.core.schemas.ingestion.models import OntologyDefinition


class OntologyLoader:
    """
    Nạp file ontology JSON thành `OntologyDefinition` đã validate.
    """
    # Load ontology từ file JSON
    @staticmethod
    def load(path: str | Path) -> OntologyDefinition:
        """
        Đọc file ontology và trả về `OntologyDefinition`.

        Args:
            path: Đường dẫn file ontology JSON.

        Returns:
            `OntologyDefinition` đã được pydantic kiểm tra hợp lệ.

        Raises:
            FileNotFoundError: File ontology không tồn tại.
            ValueError: Đường dẫn không phải file thường.
            pydantic.ValidationError: Nội dung JSON sai cấu trúc.
        """
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
