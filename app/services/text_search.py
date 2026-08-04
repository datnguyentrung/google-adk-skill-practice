"""Small text-normalization helper shared by local search services."""

import unicodedata


def normalize_text(value: str) -> str:
    """Return case-folded, accent-insensitive text with normalized spacing."""

    decomposed = unicodedata.normalize("NFD", value)
    without_marks = "".join(
        character for character in decomposed if unicodedata.category(character) != "Mn"
    )
    return " ".join(
        without_marks.replace("đ", "d").replace("Đ", "D").casefold().split()
    )


__all__ = ["normalize_text"]
