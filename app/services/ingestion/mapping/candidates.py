"""Optional cheap candidate generation before ontology-aware graph mapping."""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from typing import Any, ClassVar, Protocol

from app.core.schemas.ingestion.document import DocumentChunk
from app.services.ingestion.ontology.registry import OntologyRegistry

logger = logging.getLogger(__name__)
_DEFAULT: Any = object()
UNKNOWN_LABEL = "Unknown"


@dataclass(frozen=True)
class CandidateMention:
    chunk_index: int
    text: str
    label: str
    score: float
    start: int | None = None
    end: int | None = None


class CandidateGenerator(Protocol):
    def generate(self, chunks: list[DocumentChunk]) -> list[CandidateMention]: ...


class NullCandidateGenerator:
    def generate(self, chunks: list[DocumentChunk]) -> list[CandidateMention]:
        del chunks
        return []


class GlinerCandidateGenerator:
    """GLiNER is candidate-only; it never decides ontology facts or merges."""

    _WINDOW_MARGIN = 34
    DEFAULT_MODEL = "urchade/gliner_medium-v2.1"
    DEFAULT_THRESHOLDS: ClassVar[dict[str, float]] = {
        "urchade/gliner_medium-v2.1": 0.75,
        "urchade/gliner_large-v2.1": 0.75,
        "gliner-community/gliner_medium-v2.5": 0.75,
        "knowledgator/gliner-bi-small-v2.0": 0.5,
        "knowledgator/gliner-bi-base-v2.0": 0.5,
    }
    CANDIDATE_BAND = 0.25
    _MODEL_CACHE: ClassVar[dict[str, Any]] = {}
    _CACHE_LOCK: ClassVar[threading.Lock] = threading.Lock()

    def __init__(
        self,
        registry: OntologyRegistry,
        *,
        model_name: str | None = None,
        threshold: float | None = None,
        window_tokens: int | None = None,
        window_overlap: int = 48,
        candidate_threshold: float | None | object = _DEFAULT,
    ) -> None:
        self.registry = registry
        self.model_name = model_name or os.getenv("INGESTION_GLINER_MODEL", self.DEFAULT_MODEL)
        if threshold is None:
            threshold = self.DEFAULT_THRESHOLDS.get(self.model_name, 0.5)
        if not 0.0 <= threshold <= 1.0:
            raise ValueError(f"threshold ({threshold}) must be within [0, 1]")
        if candidate_threshold is _DEFAULT:
            candidate_threshold = round(threshold * (1.0 - self.CANDIDATE_BAND), 4)
        if candidate_threshold is not None and not 0.0 <= candidate_threshold <= threshold:
            raise ValueError(
                f"candidate_threshold ({candidate_threshold}) must be within [0, {threshold}]"
            )
        if window_tokens is not None and (not isinstance(window_tokens, int) or window_tokens <= 0):
            raise ValueError("window_tokens must be a positive integer")
        if not isinstance(window_overlap, int) or window_overlap < 0:
            raise ValueError("window_overlap must be a non-negative integer")
        if window_tokens is not None and window_overlap >= window_tokens:
            raise ValueError("window_overlap must be smaller than window_tokens")
        self.threshold = threshold
        self.candidate_threshold = candidate_threshold
        self.window_tokens = window_tokens
        self.window_overlap = window_overlap
        self._model = None
        self._labels = self._candidate_labels()

    def _candidate_labels(self) -> list[str]:
        labels: list[str] = []
        for name in self.registry.list_classes():
            cls = self.registry.get_class(name)
            if cls is not None:
                labels.append(cls.label or cls.local_name)
        return list(dict.fromkeys(labels))

    @classmethod
    def _get_shared_model(cls, model_name: str):
        cached = cls._MODEL_CACHE.get(model_name)
        if cached is not None:
            return cached
        with cls._CACHE_LOCK:
            cached = cls._MODEL_CACHE.get(model_name)
            if cached is None:
                try:
                    from gliner import GLiNER
                except ImportError as exc:
                    raise RuntimeError(
                        "GLiNER candidate generation is enabled but package 'gliner' is not installed"
                    ) from exc
                cached = GLiNER.from_pretrained(model_name)
                cls._MODEL_CACHE[model_name] = cached
        return cached

    def _load_model(self):
        if self._model is None:
            self._model = self._get_shared_model(self.model_name)
        return self._model

    def _resolve_window(self, model: Any) -> int:
        if self.window_tokens is not None:
            window = self.window_tokens
        else:
            max_len = getattr(getattr(model, "config", None), "max_len", None)
            if not isinstance(max_len, int) or max_len <= 0:
                max_len = 384
            window = max(64, max_len - self._WINDOW_MARGIN)
        if self.window_overlap >= window:
            raise ValueError("window_overlap must be smaller than the resolved GLiNER window")
        return window

    @staticmethod
    def _word_spans(model: Any, text: str) -> list[tuple[str, int, int]]:
        splitter = getattr(getattr(model, "data_processor", None), "words_splitter", None)
        if splitter is None:
            from gliner.data_processing import WordsSplitter

            splitter = WordsSplitter()
        return list(splitter(text))

    @staticmethod
    def _merge(predictions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        chain: list[dict[str, Any]] = []
        max_end = -1
        for item in sorted(
            (item for item in predictions if item["end"] > item["start"]),
            key=lambda item: (item["start"], -item["end"], -item.get("score", 0.0)),
        ):
            if item["end"] <= max_end:
                continue
            max_end = item["end"]
            chain.append(item)

        state = [0] * len(chain)
        by_score = sorted(
            range(len(chain)),
            key=lambda index: (
                -chain[index].get("score", 0.0),
                chain[index]["start"] - chain[index]["end"],
                chain[index]["start"],
            ),
        )
        for index in by_score:
            if state[index]:
                continue
            state[index] = 1
            start, end = chain[index]["start"], chain[index]["end"]
            left = index - 1
            while left >= 0 and chain[left]["end"] > start:
                state[left] = -1
                left -= 1
            right = index + 1
            while right < len(chain) and chain[right]["start"] < end:
                state[right] = -1
                right += 1
        return [item for item, status in zip(chain, state, strict=True) if status == 1]

    def _predict_sync(self, text: str) -> list[dict[str, Any]]:
        model = self._load_model()
        labels = [label.lower() for label in self._labels]
        window = self._resolve_window(model)
        floor = self.threshold if self.candidate_threshold is None else self.candidate_threshold
        words = self._word_spans(model, text)
        if len(words) <= window:
            return model.predict_entities(text, labels, threshold=floor)

        step = max(1, window - self.window_overlap)
        output: list[dict[str, Any]] = []
        for begin in range(0, len(words), step):
            span = words[begin : begin + window]
            if not span:
                break
            lo, hi = span[0][1], span[-1][2]
            for prediction in model.predict_entities(text[lo:hi], labels, threshold=floor):
                item = dict(prediction)
                item["start"] += lo
                item["end"] += lo
                item["text"] = text[item["start"] : item["end"]]
                output.append(item)
            if begin + window >= len(words):
                break
        return self._merge(output)

    def generate(self, chunks: list[DocumentChunk]) -> list[CandidateMention]:
        mentions: list[CandidateMention] = []
        for chunk in chunks:
            for item in self._predict_sync(chunk.content):
                text = str(item.get("text") or "").strip()
                score = float(item.get("score") or 0.0)
                label = str(item.get("label") or "").strip()
                if score < self.threshold:
                    label = UNKNOWN_LABEL
                if not text or not label:
                    continue
                mentions.append(
                    CandidateMention(
                        chunk_index=chunk.index,
                        text=text,
                        label=label,
                        score=score,
                        start=item.get("start"),
                        end=item.get("end"),
                    )
                )
        return mentions


def create_candidate_generator(registry: OntologyRegistry) -> CandidateGenerator:
    enabled = os.getenv("INGESTION_GLINER_ENABLED", "false").strip().lower()
    if enabled not in {"1", "true", "yes", "on"}:
        return NullCandidateGenerator()
    return GlinerCandidateGenerator(registry)


def format_candidate_hints(mentions: list[CandidateMention]) -> str:
    if not mentions:
        return ""
    lines = [
        "UNTRUSTED CANDIDATE MENTIONS (candidate generator only; verify against source/ontology):"
    ]
    for item in mentions:
        lines.append(
            f"- chunk={item.chunk_index} label={item.label!r} "
            f"score={item.score:.3f} text={item.text!r}"
        )
    return "\n".join(lines)


__all__ = [
    "CandidateGenerator",
    "CandidateMention",
    "GlinerCandidateGenerator",
    "NullCandidateGenerator",
    "create_candidate_generator",
    "format_candidate_hints",
]
