from app.scripts.preprocessing.deduplicator import (
    DocumentDedupIndex,
    exact_line_dedup,
    section_dedup,
    semantic_fact_dedup,
)
from app.scripts.preprocessing.markdown_preprocessor import (
    MarkdownPreprocessor,
    MarkdownPreprocessorConfig,
)
from app.scripts.preprocessing.validator import (
    ExpectedDatatype,
    FieldRule,
)

ATTRIBUTE_ALIASES = {
    "phí thường niên": "annual_fee",
    "annual fee": "annual_fee",
    "hạn mức": "credit_limit",
    "hạn mức tín dụng": "credit_limit",
    "credit limit": "credit_limit",
    "lãi suất": "interest_rate",
    "interest rate": "interest_rate",
}

FIELD_RULES = {
    "annual_fee": FieldRule(
        datatype=ExpectedDatatype.MONEY,
        required=True,
        min_value=0,
    ),
    "credit_limit": FieldRule(
        datatype=ExpectedDatatype.MONEY,
        min_value=0,
    ),
    "interest_rate": FieldRule(
        datatype=ExpectedDatatype.PERCENTAGE,
        min_value=0,
        max_value=100,
    ),
}


def test_markdown_preprocessor_pipeline():
    config = MarkdownPreprocessorConfig(
        attribute_aliases=ATTRIBUTE_ALIASES,
        field_rules=FIELD_RULES,
        reject_on_validation_error=False,
    )

    preprocessor = MarkdownPreprocessor(config)

    markdown = """
# FLEXI REWARDS

## Thông tin sản phẩm

• Phí thường niên : 699.000 đồng
- Annual fee: 699k

- Hạn mức tín dụng: 500 triệu
- Credit limit: 500.000.000 VND

- Lãi suất: 32 %
- Interest rate: 32%

Trang 1/20
"""

    result = preprocessor.preprocess(markdown)

    assert result.is_valid
    assert not result.duplicate_document

    # Page marker should be removed
    assert "Trang 1/20" not in result.processed_text

    # Redundant semantic facts should be deduplicated
    assert result.processed_text.count("annual_fee") == 1
    assert result.processed_text.count("credit_limit") == 1
    assert result.processed_text.count("interest_rate") == 1

    # Canonical values should be normalized
    assert "- annual_fee: 699000 VND" in result.processed_text
    assert "- credit_limit: 500000000 VND" in result.processed_text
    assert "- interest_rate: 32 %" in result.processed_text


def test_exact_line_dedup():
    text = "line 1\nline 1\nline 2"
    assert exact_line_dedup(text) == "line 1\nline 2"


def test_semantic_fact_dedup():
    text = "## Test\n- Phí thường niên: 699.000 đồng\n- Annual fee: 699k"
    res = semantic_fact_dedup(text, ATTRIBUTE_ALIASES)
    assert res == "## Test\n- Phí thường niên: 699.000 đồng"


def test_section_dedup():
    text = "## Sec 1\ncontent 1\n## Sec 1\ncontent 1"
    res = section_dedup(text)
    assert res == "## Sec 1\ncontent 1"


def test_document_dedup_index():
    index = DocumentDedupIndex()
    doc = "# Doc Title\nContent"
    assert not index.check_and_add(doc)
    assert index.check_and_add(doc)


def test_product_guide_preprocessing():
    from pathlib import Path

    doc_path = Path("docs/AN TAM ONLINE TERM DEPOSIT PRODUCT GUIDE.md")
    assert doc_path.exists(), f"Document not found at {doc_path}"

    raw_text = doc_path.read_text(encoding="utf-8")

    aliases = {
        "minimum deposit amount": "minimum_deposit_amount",
        "maximum deposit amount": "maximum_deposit_amount",
        "document name": "document_name",
        "document code": "document_code",
        "product code": "product_code",
        "commercial name": "commercial_name",
        "version": "version",
        "status": "status",
        "effective date": "effective_date",
        "expiry date": "expiry_date",
        "applicable policy": "applicable_policy",
        "product management unit": "product_management_unit",
        "primary distribution channel": "primary_distribution_channel",
        "intended users": "intended_users",
        "eligible customer type": "eligible_customer_type",
        "product type": "product_type",
        "opening channel": "opening_channel",
        "management channel": "management_channel",
        "source of funds": "source_of_funds",
        "interest payment": "interest_payment",
        "partial early withdrawal": "partial_early_withdrawal",
        "full early withdrawal": "full_early_withdrawal",
        "number of deposits": "number_of_deposits",
    }

    config = MarkdownPreprocessorConfig(
        attribute_aliases=aliases,
        reject_on_validation_error=False,
    )

    preprocessor = MarkdownPreprocessor(config)
    result = preprocessor.preprocess(raw_text)

    raw_chars = len(raw_text)
    processed_chars = len(result.processed_text)
    raw_lines = len(raw_text.splitlines())
    processed_lines = len(result.processed_text.splitlines())
    raw_words = len(raw_text.split())
    processed_words = len(result.processed_text.split())

    # Token estimate: ~4 chars per token for English text
    raw_tokens_est = round(raw_chars / 4)
    processed_tokens_est = round(processed_chars / 4)
    char_saved = raw_chars - processed_chars
    token_saved = raw_tokens_est - processed_tokens_est
    pct_saved = (char_saved / raw_chars) * 100 if raw_chars else 0

    print("\n" + "=" * 60)
    print("=== PRODUCT GUIDE PREPROCESSING METRICS ===")
    print("=" * 60)
    print(f"Raw Document     : {raw_lines} lines | {raw_words} words | {raw_chars} chars | ~{raw_tokens_est} tokens")
    print(f"Cleaned Document : {processed_lines} lines | {processed_words} words | {processed_chars} chars | ~{processed_tokens_est} tokens")
    print(f"Reduction / Saved: {raw_lines - processed_lines} lines | {raw_words - processed_words} words | {char_saved} chars | ~{token_saved} tokens ({pct_saved:.2f}%)")
    print(f"Validation Status: {'VALID' if result.is_valid else 'INVALID'}")
    print(f"Fingerprint      : {result.fingerprint}")
    print("=" * 60)

    assert result.is_valid
    assert processed_chars <= raw_chars


