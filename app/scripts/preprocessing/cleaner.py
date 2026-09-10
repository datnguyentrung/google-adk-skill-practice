from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable

_HEADING_RE = re.compile(r"^(#{1,6})[ \t]*(.*?)[ \t]*$")
_BULLET_RE = re.compile(r"^(\s*)[•●▪◦*+]\s+")
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)

_TABLE_SEPARATOR_CELL_RE = re.compile(r"^:?-{3,}:?$")

_PAGE_MARKER_RE = re.compile(
    r"^\s*(?:page|trang)\s+\d+\s*(?:/|of)?\s*\d*\s*$",
    flags=re.IGNORECASE,
)


_HR_RE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")


@dataclass(frozen=True)
class CleanerConfig:
    remove_html_comments: bool = True
    remove_page_markers: bool = True
    remove_horizontal_rules: bool = True
    normalize_tables: bool = True
    max_blank_lines: int = 1



def _iter_lines_preserving_fences(
    text: str,
) -> Iterable[tuple[str, bool]]:
    """
    Yield:
        (line, inside_code_block_before_this_line)

    Không clean nội dung bên trong fenced code block.
    """

    inside = False
    fence_token: str | None = None

    for line in text.splitlines():
        stripped = line.lstrip()

        is_fence = stripped.startswith("```") or stripped.startswith("~~~")

        yield line, inside

        if is_fence:
            token = stripped[:3]

            if not inside:
                inside = True
                fence_token = token

            elif token == fence_token:
                inside = False
                fence_token = None


def normalize_unicode(text: str) -> str:
    """
    Chuẩn hóa Unicode compatibility characters.
    """
    return unicodedata.normalize("NFKC", text)


def normalize_newlines(text: str) -> str:
    """
    Windows / Unix / old Mac newline -> \n
    """
    return text.replace("\r\n", "\n").replace("\r", "\n")


def remove_html_comments(text: str) -> str:
    """
    Xóa:
        <!-- comment -->
    """
    return _HTML_COMMENT_RE.sub("", text)


def normalize_trailing_whitespace(text: str) -> str:
    """
    Xóa whitespace cuối dòng.

    Không collapse whitespace toàn document vì có thể
    làm hỏng Markdown/code/table.
    """

    return "\n".join(line.rstrip() for line in text.splitlines())


def normalize_headings(text: str) -> str:
    """
    Ví dụ:

        ##Title
        ##    Title

    ->
        ## Title
    """

    result: list[str] = []

    for line, inside_fence in _iter_lines_preserving_fences(text):
        stripped = line.lstrip()

        if inside_fence or stripped.startswith(("```", "~~~")):
            result.append(line)
            continue

        match = _HEADING_RE.match(line)

        if not match:
            result.append(line)
            continue

        hashes, title = match.groups()
        title = title.strip()

        if title:
            result.append(f"{hashes} {title}")
        else:
            result.append(hashes)

    return "\n".join(result)


def normalize_bullets(text: str) -> str:
    """
    Chuẩn hóa:

        * item
        + item
        • item
        ● item

    thành:

        - item
    """

    result: list[str] = []

    for line, inside_fence in _iter_lines_preserving_fences(text):
        stripped = line.lstrip()

        if inside_fence or stripped.startswith(("```", "~~~")):
            result.append(line)
            continue

        result.append(_BULLET_RE.sub(r"\1- ", line))

    return "\n".join(result)


def _split_table_cells(
    line: str,
) -> list[str] | None:

    stripped = line.strip()

    if not (stripped.startswith("|") and stripped.endswith("|")):
        return None

    # Không tự sửa table có escaped pipe.
    if r"\|" in stripped:
        return None

    return [cell.strip() for cell in stripped[1:-1].split("|")]


def normalize_tables(text: str) -> str:
    """
    Chuẩn hóa spacing của Markdown table.

    Ví dụ:

        |Name| Fee |
        |---|---|

    ->
        | Name | Fee |
        | --- | --- |
    """

    result: list[str] = []

    for line, inside_fence in _iter_lines_preserving_fences(text):
        stripped = line.lstrip()

        if inside_fence or stripped.startswith(("```", "~~~")):
            result.append(line)
            continue

        cells = _split_table_cells(line)

        if cells is None:
            result.append(line)
            continue

        # Separator row
        if cells and all(_TABLE_SEPARATOR_CELL_RE.fullmatch(cell) for cell in cells):
            normalized_cells: list[str] = []

            for cell in cells:
                left_align = cell.startswith(":")
                right_align = cell.endswith(":")

                normalized_cells.append(
                    f"{':' if left_align else ''}---{':' if right_align else ''}"
                )

            result.append("|" + "|".join(normalized_cells) + "|")

        else:
            result.append("| " + " | ".join(cells) + " |")

    return "\n".join(result)



def remove_page_markers(text: str) -> str:
    """
    Xóa các dòng kiểu:

        Trang 1/20
        Page 3 of 50
    """

    result: list[str] = []

    for line, inside_fence in _iter_lines_preserving_fences(text):
        if inside_fence:
            result.append(line)
            continue

        if _PAGE_MARKER_RE.match(line):
            continue

        result.append(line)

    return "\n".join(result)


def remove_horizontal_rules(text: str) -> str:
    """
    Xóa các dòng kẻ phân cách:
        ---
        ***
        ___
    """
    result: list[str] = []

    for line, inside_fence in _iter_lines_preserving_fences(text):
        if inside_fence:
            result.append(line)
            continue

        if _HR_RE.match(line):
            continue

        result.append(line)

    return "\n".join(result)


def normalize_blank_lines(
    text: str,
    max_blank_lines: int = 1,
) -> str:
    """
    Giới hạn số dòng trắng liên tiếp.
    """

    max_blank_lines = max(
        0,
        max_blank_lines,
    )

    max_newlines = max_blank_lines + 1

    return re.sub(
        rf"\n{{{max_newlines + 1},}}",
        "\n" * max_newlines,
        text,
    )


def clean_markdown(
    text: str,
    config: CleanerConfig | None = None,
) -> str:

    config = config or CleanerConfig()

    if not text:
        return ""

    text = normalize_unicode(text)
    text = normalize_newlines(text)

    if config.remove_html_comments:
        text = remove_html_comments(text)

    if config.remove_horizontal_rules:
        text = remove_horizontal_rules(text)

    text = normalize_trailing_whitespace(text)
    text = normalize_headings(text)
    text = normalize_bullets(text)

    if config.normalize_tables:
        text = normalize_tables(text)

    if config.remove_page_markers:
        text = remove_page_markers(text)

    text = normalize_blank_lines(
        text,
        max_blank_lines=config.max_blank_lines,
    )

    return text.strip()

