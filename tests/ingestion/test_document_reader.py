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
