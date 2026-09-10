from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Iterable, Mapping

from app.scripts.preprocessing.normalizer import (
    normalize_for_comparison,
    parse_attribute_line,
)

_HEADING_RE = re.compile(r"^(#{1,6})(?:\s+(.+))?$")


def _is_fence_line(line: str) -> bool:
    stripped = line.lstrip()
    return stripped.startswith("```") or stripped.startswith("~~~")


def exact_line_dedup(text: str) -> str:
    """
    Omit consecutive identical lines or duplicate lines within the same section,
    preserving Markdown code fences, headings, and blank lines.
    """
    if not text:
        return ""

    result: list[str] = []
    seen_lines_in_section: set[str] = set()
    current_section = "<root>"
    inside_fence = False
    fence_token: str | None = None

    for line in text.splitlines():
        stripped_left = line.lstrip()

        if _is_fence_line(line):
            token = stripped_left[:3]
            result.append(line)
            if not inside_fence:
                inside_fence = True
                fence_token = token
            elif token == fence_token:
                inside_fence = False
                fence_token = None
            continue

        if inside_fence:
            result.append(line)
            continue

        match = _HEADING_RE.match(line.strip())
        if match:
            heading_title = (match.group(2) or "").strip()
            current_section = heading_title or f"H{len(match.group(1))}"
            seen_lines_in_section.clear()
            result.append(line)
            continue

        stripped_line = line.strip()
        if not stripped_line:
            result.append(line)
            continue

        if stripped_line in seen_lines_in_section:
            continue

        seen_lines_in_section.add(stripped_line)
        result.append(line)

    return "\n".join(result)


def normalized_line_dedup(text: str) -> str:
    """
    Remove lines that normalize to identical content within the same section.
    """
    if not text:
        return ""

    result: list[str] = []
    seen_normalized_in_section: set[str] = set()
    inside_fence = False
    fence_token: str | None = None

    for line in text.splitlines():
        stripped_left = line.lstrip()

        if _is_fence_line(line):
            token = stripped_left[:3]
            result.append(line)
            if not inside_fence:
                inside_fence = True
                fence_token = token
            elif token == fence_token:
                inside_fence = False
                fence_token = None
            continue

        if inside_fence:
            result.append(line)
            continue

        match = _HEADING_RE.match(line.strip())
        if match:
            seen_normalized_in_section.clear()
            result.append(line)
            continue

        stripped = line.strip()
        if not stripped:
            result.append(line)
            continue

        norm = normalize_for_comparison(stripped)
        if norm in seen_normalized_in_section:
            continue

        seen_normalized_in_section.add(norm)
        result.append(line)

    return "\n".join(result)


def semantic_fact_dedup(
    text: str,
    attribute_aliases: Mapping[str, str] | None = None,
) -> str:
    """
    Deduplicate attribute-value facts within each section.
    If two lines in the same section express the same canonical attribute and value
    (e.g., 'Phí thường niên: 699k' and 'annual_fee: 699000 VND'), keep only the first.
    """
    if not text:
        return ""

    result: list[str] = []
    seen_facts_in_section: set[str] = set()
    inside_fence = False
    fence_token: str | None = None

    for line in text.splitlines():
        stripped_left = line.lstrip()

        if _is_fence_line(line):
            token = stripped_left[:3]
            result.append(line)
            if not inside_fence:
                inside_fence = True
                fence_token = token
            elif token == fence_token:
                inside_fence = False
                fence_token = None
            continue

        if inside_fence:
            result.append(line)
            continue

        match = _HEADING_RE.match(line.strip())
        if match:
            seen_facts_in_section.clear()
            result.append(line)
            continue

        fact = parse_attribute_line(line, attribute_aliases)
        if fact is not None:
            fact_key = fact.key
            if fact_key in seen_facts_in_section:
                continue
            seen_facts_in_section.add(fact_key)

        result.append(line)

    return "\n".join(result)


def section_dedup(text: str) -> str:
    """
    Deduplicate identical sections (heading + body) in a document.
    """
    if not text:
        return ""

    lines = text.splitlines()
    sections: list[tuple[str, list[str]]] = []
    current_heading = ""
    current_lines: list[str] = []

    inside_fence = False
    fence_token: str | None = None

    for line in lines:
        stripped_left = line.lstrip()

        if _is_fence_line(line):
            token = stripped_left[:3]
            if not inside_fence:
                inside_fence = True
                fence_token = token
            elif token == fence_token:
                inside_fence = False
                fence_token = None
            current_lines.append(line)
            continue

        if not inside_fence:
            match = _HEADING_RE.match(line.strip())
            if match:
                sections.append((current_heading, current_lines))
                current_heading = line
                current_lines = []
                continue

        current_lines.append(line)

    sections.append((current_heading, current_lines))

    seen_section_keys: set[str] = set()
    result_lines: list[str] = []

    for heading, sec_lines in sections:
        if not heading and not any(sec_lines):
            continue

        normalized_heading = normalize_for_comparison(heading)
        normalized_body = "\n".join(
            normalize_for_comparison(l) for l in sec_lines if l.strip()
        )
        sec_key = f"{normalized_heading}\n{normalized_body}"

        if sec_key in seen_section_keys:
            continue

        if sec_key.strip():
            seen_section_keys.add(sec_key)

        if heading:
            result_lines.append(heading)
        result_lines.extend(sec_lines)

    return "\n".join(result_lines)


def document_fingerprint(text: str) -> str:
    """
    Compute a SHA-256 hash fingerprint for a document text.
    """
    cleaned = normalize_for_comparison(text)
    return hashlib.sha256(cleaned.encode("utf-8")).hexdigest()


@dataclass
class DocumentDedupIndex:
    fingerprints: set[str] = field(default_factory=set)

    def add(self, text_or_fp: str) -> str:
        fp = self._get_fingerprint(text_or_fp)
        self.fingerprints.add(fp)
        return fp

    def contains(self, text_or_fp: str) -> bool:
        fp = self._get_fingerprint(text_or_fp)
        return fp in self.fingerprints

    def check_and_add(self, text_or_fp: str) -> bool:
        """
        Check if document is duplicate.
        Returns True if duplicate (already seen), False if newly added.
        """
        fp = self._get_fingerprint(text_or_fp)
        if fp in self.fingerprints:
            return True
        self.fingerprints.add(fp)
        return False

    @staticmethod
    def _get_fingerprint(text_or_fp: str) -> str:
        if len(text_or_fp) == 64 and re.fullmatch(r"[0-9a-fA-F]{64}", text_or_fp):
            return text_or_fp.lower()
        return document_fingerprint(text_or_fp)
