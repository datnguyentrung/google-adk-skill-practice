from pathlib import Path

from app.services.ingestion.document_reader import (
    DocumentReader,
)

DOCUMENT_PATH = "docs/HƯỚNG DẪN NGHIỆP VỤ SẢN PHẨM THẺ TÍN DỤNG FLEXI REWARDS.md"


def test_read_markdown_document():
    reader = DocumentReader()

    chunks = reader.read(DOCUMENT_PATH)

    assert len(chunks) > 0

    assert any(
        chunk.section == "2.1. Thẻ tín dụng Flexi Rewards là gì?" for chunk in chunks
    )

    assert any("CC-FLEXI-001" in chunk.content for chunk in chunks)
    assert all(chunk.chunk_id == f"chunk_{chunk.index:04d}" for chunk in chunks)
    assert all(chunk.start_line is not None for chunk in chunks)
    assert all(chunk.end_line is not None for chunk in chunks)
    assert all(chunk.start_line <= chunk.end_line for chunk in chunks)


def test_document_contains_eligibility_section():
    reader = DocumentReader()

    chunks = reader.read(DOCUMENT_PATH)

    eligibility_chunks = [
        chunk
        for chunk in chunks
        if chunk.section and "Điều kiện để tiếp nhận hồ sơ" in chunk.section
    ]

    assert len(eligibility_chunks) == 1

    content = eligibility_chunks[0].content

    assert "20 tuổi" in content
    assert "10 triệu VND/tháng" in content


def test_document_contains_campaign():
    reader = DocumentReader()

    chunks = reader.read(DOCUMENT_PATH)

    assert any("Flexi Dining & Shopping" in chunk.content for chunk in chunks)


def test_read_uploaded_markdown_bytes():
    reader = DocumentReader()
    data = b"# Product\n\nCode: CARD-001\n"

    chunks = reader.read_bytes(
        filename="product.md",
        data=data,
        mime_type="text/markdown",
    )

    assert len(chunks) == 1
    assert chunks[0].source == "product.md"
    assert chunks[0].section == "Product"
    assert "CARD-001" in chunks[0].content
    assert chunks[0].chunk_id == "chunk_0000"
    assert chunks[0].start_line == 3
    assert chunks[0].end_line == 3


def test_flexi_representative_facts_are_locatable_in_chunks():
    reader = DocumentReader()

    chunks = reader.read(DOCUMENT_PATH)
    source = "\n".join(chunk.content for chunk in chunks)

    for fact in [
        "CC-FLEXI-001",
        "POL-CC-2026-03",
        "20 triệu đến 500 triệu",
        "699.000 VND",
        "32%/năm",
        "50 ngày",
        "Flexi Dining & Shopping",
        "3.000.000 VND",
    ]:
        assert fact in source


def test_three_reference_docs_have_ingestion_anchor_facts():
    reader = DocumentReader()
    docs = list(Path("docs").glob("*.md"))

    discovered = {}
    for path in docs:
        source = "\n".join(chunk.content for chunk in reader.read(path))
        for code, date in {
            "CC-FLEXI-001": "01/08/2026",
            "TD-ONLINE-001": "01/07/2026",
            "AUTO-FLEX-01": "01/08/2026",
        }.items():
            if code in source:
                discovered[code] = date in source

    assert discovered == {
        "CC-FLEXI-001": True,
        "TD-ONLINE-001": True,
        "AUTO-FLEX-01": True,
    }
