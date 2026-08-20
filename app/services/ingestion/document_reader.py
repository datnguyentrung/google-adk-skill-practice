import re
from pathlib import Path

from app.core.schemas.ingestion.document import DocumentChunk


class DocumentReadError(ValueError):
    """Lỗi nghiệp vụ khi đường dẫn/tài liệu không phù hợp để đọc ingestion."""

    pass


class DocumentReader:
    """Đọc tài liệu nguồn và chuyển thành danh sách DocumentChunk để ingestion."""

    # Chỉ hỗ trợ các định dạng text đơn giản để tránh phải parse binary/PDF ở tầng này.
    SUPPORTED_SUFFIXES = {
        ".md",
        ".txt",
    }

    # Nhận diện markdown heading từ cấp 1 đến cấp 6, ví dụ "# Title" hoặc "### Section".
    HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.+?)\s*$")

    def read(
        self,
        path: str | Path,
    ) -> list[DocumentChunk]:
        """
        Đọc một file tài liệu và trả về các chunk nội dung.

        - File .md được tách theo heading; mỗi section thành một DocumentChunk.
        - File .txt được giữ nguyên thành một DocumentChunk duy nhất.
        - Raise lỗi rõ nghĩa nếu file không tồn tại, không phải file, sai định dạng,
          hoặc nội dung rỗng.
        """

        # Chuẩn hóa input str/Path về Path để dùng chung các API filesystem.
        document_path = Path(path)

        # Không tự tạo file thiếu; ingestion cần báo lỗi nguồn dữ liệu ngay.
        if not document_path.exists():
            raise FileNotFoundError(f"Document not found: {document_path}")

        # Chỉ đọc file đơn lẻ, không đọc folder hoặc path đặc biệt.
        if not document_path.is_file():
            raise DocumentReadError(f"Document path is not a file: {document_path}")

        # So sánh suffix dạng lowercase để hỗ trợ ".MD", ".Txt", ...
        suffix = document_path.suffix.lower()

        # Chặn sớm định dạng chưa hỗ trợ để tránh parse sai nội dung.
        if suffix not in self.SUPPORTED_SUFFIXES:
            raise DocumentReadError(f"Unsupported document type: {suffix}")

        # Tất cả tài liệu hiện được đọc bằng UTF-8.
        text = document_path.read_text(encoding="utf-8")

        # Tài liệu toàn khoảng trắng cũng được xem là rỗng.
        if not text.strip():
            raise DocumentReadError(f"Document is empty: {document_path}")

        # Markdown cần giữ thông tin section để các bước sau có ngữ cảnh tốt hơn.
        if suffix == ".md":
            return self._split_markdown(
                source=document_path.name,
                text=text,
            )

        # Plain text không có section metadata nên gom toàn bộ file thành một chunk.
        return [
            DocumentChunk(
                index=0,
                source=document_path.name,
                section=None,
                content=text.strip(),
            )
        ]

    def _split_markdown(
        self,
        source: str,
        text: str,
    ) -> list[DocumentChunk]:
        """
        Tách markdown thành chunk theo heading.

        Nội dung nằm dưới heading nào sẽ được gán vào section đó. Phần nội dung
        trước heading đầu tiên vẫn được giữ lại với section=None.
        """

        # Danh sách chunk kết quả, index được gán theo thứ tự append.
        chunks: list[DocumentChunk] = []

        # Section hiện tại là heading gần nhất đã gặp.
        current_section: str | None = None

        # Buffer các dòng nội dung thuộc section hiện tại.
        current_lines: list[str] = []

        def flush() -> None:
            """Đẩy buffer hiện tại thành DocumentChunk nếu có nội dung thực."""

            nonlocal current_lines

            # Trim khoảng trắng đầu/cuối để chunk không chứa dòng rỗng thừa.
            content = "\n".join(current_lines).strip()

            # Nếu section chỉ có heading hoặc dòng rỗng thì bỏ qua chunk trống.
            if not content:
                current_lines = []
                return

            # Tạo chunk với index tăng dần theo số chunk đã có.
            chunks.append(
                DocumentChunk(
                    index=len(chunks),
                    source=source,
                    section=current_section,
                    content=content,
                )
            )

            # Reset buffer để bắt đầu gom nội dung cho section tiếp theo.
            current_lines = []

        # Duyệt từng dòng để phát hiện heading và gom nội dung theo section.
        for line in text.splitlines():
            heading_match = self.HEADING_PATTERN.match(line)

            if heading_match:
                # Gặp heading mới thì đóng section cũ trước.
                flush()

                # group(2) là phần text heading, bỏ các dấu # phía trước.
                current_section = heading_match.group(2).strip()

                continue

            # Dòng thường được gom vào buffer của section hiện tại.
            current_lines.append(line)

        # Đóng section cuối cùng sau khi duyệt hết file.
        flush()

        return chunks
