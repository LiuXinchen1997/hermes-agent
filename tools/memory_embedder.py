"""Embedding backend with keyword fallback.

Two implementations behind one common interface:
  - FastembedEmbedder: BGE-small-zh via ONNX runtime
  - KeywordEmbedder:   Jaccard token overlap, no model required

Factory `make_embedder()` returns the best available implementation.
"""
from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from math import sqrt
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)


# Tokenizer used by KeywordEmbedder. Python 3's \w with default flags already
# matches Unicode word characters (incl. CJK), so we don't need a separate
# range. Lowercase normalization happens in _tokenize.
_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


def _tokenize(text: str) -> List[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text)]


class Embedder(ABC):
    """Encode + compare. Implementations may store vectors or just raw text."""

    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def encode(self, text: str) -> Optional[List[float]]:
        """Pre-compute and return a vector for *text*.

        May return None if the embedder doesn't pre-encode (e.g., keyword
        backend). Callers must then pass the raw text to `similarity`.
        """

    @abstractmethod
    def similarity(
        self,
        query: str,
        candidate: str,
        candidate_vec: Optional[List[float]] = None,
    ) -> float:
        """Return similarity in [0, 1]. Higher = more related.

        If `candidate_vec` is provided AND this embedder supports vectors,
        use it for speed. Otherwise fall back to encoding `candidate` on
        the fly, or use the raw text directly (keyword backend).
        """

    def batch_similarity(
        self,
        query: str,
        candidates: List[Tuple[str, Optional[List[float]]]],
    ) -> List[float]:
        """Score `query` against many `(text, vec)` candidate pairs.

        Default implementation calls `similarity` per pair. Vector-based
        backends MUST override to encode `query` only once per call —
        the per-pair `similarity()` re-encodes the query each time, which
        is O(N) wasted model inferences and is the dominant cost at recall
        time with model-backed embedders.
        """
        return [self.similarity(query, text, vec) for text, vec in candidates]


class FastembedEmbedder(Embedder):
    """BGE-small-zh via fastembed (ONNX runtime). Falls back during init."""

    def __init__(self, model_name: str = "BAAI/bge-small-zh-v1.5"):
        from fastembed import TextEmbedding  # type: ignore

        self._model_name = model_name
        self._model = TextEmbedding(model_name)
        # Probe dim by encoding a token
        probe = next(self._model.embed(["probe"]))
        self._dim = len(probe)
        logger.info("FastembedEmbedder loaded: %s (dim=%d)", model_name, self._dim)

    @property
    def name(self) -> str:
        return f"fastembed:{self._model_name}"

    @property
    def dim(self) -> int:
        return self._dim

    def encode(self, text: str) -> List[float]:
        vec = next(self._model.embed([text]))
        return [float(x) for x in vec]

    def similarity(
        self,
        query: str,
        candidate: str,
        candidate_vec: Optional[List[float]] = None,
    ) -> float:
        """Cosine similarity in [0, 1].

        BGE-family embeddings are L2-normalized AND empirically positive for
        natural language (random pairs cosine ~0.3-0.4, not 0). So we return
        raw cosine and clamp to [0, 1] for safety. Callers should set the
        retriever's `min_similarity` to ~0.5 to separate "unrelated noise"
        from "real hit" — see retriever defaults.
        """
        q_vec = self.encode(query)
        c_vec = candidate_vec if candidate_vec is not None else self.encode(candidate)
        return self._cosine(q_vec, c_vec)

    def batch_similarity(
        self,
        query: str,
        candidates: List[Tuple[str, Optional[List[float]]]],
    ) -> List[float]:
        # Encode query ONCE — this is the whole point of the override.
        q_vec = self.encode(query)
        out: List[float] = []
        for text, vec in candidates:
            c_vec = vec if vec is not None else self.encode(text)
            out.append(self._cosine(q_vec, c_vec))
        return out

    def _cosine(self, q_vec: List[float], c_vec: List[float]) -> float:
        if len(q_vec) != len(c_vec):
            return 0.0
        dot = sum(a * b for a, b in zip(q_vec, c_vec))
        # Defensive normalization (cheap insurance against an upstream
        # change that breaks the L2-normalized assumption).
        if abs(dot) > 1.0 + 1e-3:
            na = sqrt(sum(a * a for a in q_vec)) or 1.0
            nb = sqrt(sum(b * b for b in c_vec)) or 1.0
            dot = dot / (na * nb)
        return max(0.0, min(1.0, dot))


class KeywordEmbedder(Embedder):
    """Jaccard similarity on tokenized text. No model required."""

    @property
    def name(self) -> str:
        return "keyword:jaccard"

    def encode(self, text: str) -> Optional[List[float]]:
        # No pre-encoding for the keyword backend. Similarity recomputes
        # tokens from raw text per query.
        return None

    def similarity(
        self,
        query: str,
        candidate: str,
        candidate_vec: Optional[List[float]] = None,
    ) -> float:
        # candidate_vec is ignored for keyword backend.
        a = set(_tokenize(query))
        b = set(_tokenize(candidate))
        if not a or not b:
            return 0.0
        return len(a & b) / len(a | b)

    def batch_similarity(
        self,
        query: str,
        candidates: List[Tuple[str, Optional[List[float]]]],
    ) -> List[float]:
        # Tokenize query once (small win — tokenizer is fast).
        a = set(_tokenize(query))
        if not a:
            return [0.0] * len(candidates)
        out: List[float] = []
        for text, _vec in candidates:
            b = set(_tokenize(text))
            if not b:
                out.append(0.0)
            else:
                out.append(len(a & b) / len(a | b))
        return out


def make_embedder(prefer_local: bool = True) -> Embedder:
    """Best-effort embedder factory.

    Tries `FastembedEmbedder` first when `prefer_local`; falls back to
    `KeywordEmbedder` on any import / load failure.
    """
    if prefer_local:
        try:
            return FastembedEmbedder()
        except Exception as e:
            logger.warning(
                "fastembed unavailable (%s); falling back to keyword similarity",
                e.__class__.__name__,
            )
    return KeywordEmbedder()
