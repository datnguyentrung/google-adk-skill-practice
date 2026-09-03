from __future__ import annotations

from typing import Any


class InvalidGraphPatchFragmentError(ValueError):
    """Retryable legacy error for callers still reporting fragment parse failures."""

    error_kind = "invalid_graph_patch_fragment"
    retryable = True

    def __init__(self, message: str, *, summary: dict[str, Any] | None = None):
        super().__init__(message)
        self.summary = summary or {}


__all__ = ["InvalidGraphPatchFragmentError"]
