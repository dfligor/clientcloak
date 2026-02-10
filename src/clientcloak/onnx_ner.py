"""Lightweight ONNX inference wrapper for GLiNER NER.

Replicates GLiNER's predict_entities() API using only onnxruntime and
the tokenizers (Rust) library — no torch dependency required.  This
keeps the bundled app ~400 MB smaller by avoiding PyTorch.

The module loads a pre-exported ONNX model (produced by
``scripts/prepare_gliner_model.py``) and performs:

1. Whitespace-based word splitting (same regex as GLiNER)
2. Entity-label prompt construction (<<ENT>> label <<SEP>>)
3. Subword tokenization via the model's DeBERTa tokenizer
4. words_mask / span_idx / span_mask tensor preparation
5. ONNX inference
6. Sigmoid → threshold → greedy non-overlapping span selection
7. Character-offset mapping back to the original text
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

# Same word-splitting regex used by GLiNER's WhitespaceTokenSplitter.
_WORD_RE = re.compile(r"\w+(?:[-_]\w+)*|\S")


class OnnxNerModel:
    """Drop-in replacement for ``GLiNER`` that uses ONNX Runtime."""

    def __init__(
        self,
        session,  # ort.InferenceSession
        tokenizer,  # tokenizers.Tokenizer
        *,
        max_width: int = 12,
        max_len: int = 384,
        ent_token: str = "<<ENT>>",
        sep_token: str = "<<SEP>>",
    ) -> None:
        self._session = session
        self._tokenizer = tokenizer
        self._max_width = max_width
        self._max_len = max_len
        self._ent_token = ent_token
        self._sep_token = sep_token

        # Cache ONNX I/O names for validation.
        self._input_names = {inp.name for inp in session.get_inputs()}

    # ------------------------------------------------------------------
    # Public API — matches GLiNER.predict_entities()
    # ------------------------------------------------------------------

    def predict_entities(
        self,
        text: str,
        labels: list[str],
        threshold: float = 0.5,
        flat_ner: bool = True,
    ) -> list[dict[str, Any]]:
        """Detect named entities in *text*.

        Returns a list of dicts identical to GLiNER's output::

            [{"start": 0, "end": 10, "text": "John Smith",
              "label": "person", "score": 0.92}, ...]
        """
        # 1. Word-split the raw text.
        words, starts, ends = _split_words(text)
        if not words:
            return []

        num_words = min(len(words), self._max_len)
        words = words[:num_words]
        starts = starts[:num_words]
        ends = ends[:num_words]

        # 2. Build the entity-label prompt.
        labels = list(dict.fromkeys(labels))  # dedupe, preserve order
        prompt: list[str] = []
        for label in labels:
            prompt.append(self._ent_token)
            prompt.append(label)
        prompt.append(self._sep_token)
        prompt_len = len(prompt)

        # 3. Merge prompt + text words and tokenize.
        all_words = prompt + words
        encoding = self._tokenizer.encode(all_words, is_pretokenized=True)
        input_ids = encoding.ids
        attention_mask = encoding.attention_mask
        word_ids_list = encoding.word_ids  # None for special tokens

        seq_len = len(input_ids)

        # 4. Build words_mask (1-indexed text-word positions; 0 for prompt/special).
        words_mask = _build_words_mask(word_ids_list, prompt_len)

        # 5. Build span_idx and span_mask.
        span_idx, span_mask = _build_spans(num_words, self._max_width)

        # 6. Assemble numpy inputs (batch size 1).
        feed: dict[str, np.ndarray] = {
            "input_ids": np.array([input_ids], dtype=np.int64),
            "attention_mask": np.array([attention_mask], dtype=np.int64),
            "words_mask": np.array([words_mask], dtype=np.int64),
            "text_lengths": np.array([[num_words]], dtype=np.int64),
            "span_idx": np.array([span_idx], dtype=np.int64),
            "span_mask": np.array([span_mask], dtype=np.bool_),
        }
        # Filter to only inputs the ONNX graph expects.
        feed = {k: v for k, v in feed.items() if k in self._input_names}

        # 7. Run ONNX inference → logits.
        (logits,) = self._session.run(["logits"], feed)
        # logits shape: (1, num_words, max_width, num_classes)

        # 8. Decode spans.
        num_classes = len(labels)
        id_to_class = {i: label for i, label in enumerate(labels)}
        raw_spans = _decode_logits(
            logits[0], num_words, self._max_width, num_classes,
            id_to_class, threshold, flat_ner,
        )

        # 9. Map token indices → character offsets.
        entities: list[dict[str, Any]] = []
        for start_tok, end_tok, ent_type, score in raw_spans:
            if start_tok >= len(starts) or end_tok >= len(ends):
                continue
            start_char = starts[start_tok]
            end_char = ends[end_tok]
            entities.append({
                "start": start_char,
                "end": end_char,
                "text": text[start_char:end_char],
                "label": ent_type,
                "score": float(score),
            })

        return entities


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------

def _split_words(text: str) -> tuple[list[str], list[int], list[int]]:
    """Whitespace-regex word split matching GLiNER's tokenizer."""
    words: list[str] = []
    starts: list[int] = []
    ends_list: list[int] = []
    for m in _WORD_RE.finditer(text):
        words.append(m.group())
        starts.append(m.start())
        ends_list.append(m.end())
    return words, starts, ends_list


def _build_words_mask(word_ids: list[int | None], prompt_len: int) -> list[int]:
    """Create the words_mask that maps subwords → 1-indexed text-word positions.

    Prompt words (indices 0..prompt_len-1) are masked as 0.
    Only the *first* subword of each text word gets a non-zero value.
    """
    mask: list[int] = []
    prev_wid: int | None = None
    seen_words = 0

    for wid in word_ids:
        if wid is None:
            mask.append(0)
        elif wid != prev_wid:
            seen_words += 1
            if seen_words <= prompt_len:
                mask.append(0)
            else:
                mask.append(seen_words - prompt_len)
        else:
            mask.append(0)
        prev_wid = wid

    return mask


def _build_spans(
    num_tokens: int, max_width: int,
) -> tuple[list[list[int]], list[bool]]:
    """Enumerate (start, end) spans and validity mask.

    Mirrors ``gliner.data_processing.utils.prepare_span_idx``.
    """
    span_idx: list[list[int]] = []
    span_mask: list[bool] = []
    for i in range(num_tokens):
        for j in range(max_width):
            end = i + j
            span_idx.append([i, end])
            span_mask.append(end < num_tokens)
    return span_idx, span_mask


def _decode_logits(
    logits: np.ndarray,
    num_tokens: int,
    max_width: int,
    num_classes: int,
    id_to_class: dict[int, str],
    threshold: float,
    flat_ner: bool,
) -> list[tuple[int, int, str, float]]:
    """Sigmoid → threshold → greedy NMS → span tuples.

    *logits* has shape ``(L, K, C)`` where L = num_tokens, K = max_width,
    C = num_classes.

    Returns list of ``(start_tok, end_tok, entity_type, score)``.
    """
    probs = _sigmoid(logits)  # (L, K, C)

    # Find all (start, width, class) positions above threshold.
    s_idx, k_idx, c_idx = np.where(probs > threshold)

    candidates: list[tuple[int, int, str, float]] = []
    for s, k, c in zip(s_idx.tolist(), k_idx.tolist(), c_idx.tolist()):
        end = s + k
        if end >= num_tokens:
            continue
        score = float(probs[s, k, c])
        ent_type = id_to_class[c]
        candidates.append((s, end, ent_type, score))

    if flat_ner:
        candidates = _greedy_search(candidates)

    return candidates


def _sigmoid(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-x.astype(np.float64))).astype(np.float32)


def _greedy_search(
    spans: list[tuple[int, int, str, float]],
) -> list[tuple[int, int, str, float]]:
    """Keep highest-scoring non-overlapping spans."""
    sorted_spans = sorted(spans, key=lambda x: -x[-1])
    selected: list[tuple[int, int, str, float]] = []
    for candidate in sorted_spans:
        if not any(_overlaps(candidate, s) for s in selected):
            selected.append(candidate)
    return sorted(selected, key=lambda x: x[0])


def _overlaps(
    a: tuple[int, int, str, float],
    b: tuple[int, int, str, float],
) -> bool:
    """True if spans a and b overlap (inclusive endpoints)."""
    return not (a[1] < b[0] or b[1] < a[0])


# ------------------------------------------------------------------
# Model loading
# ------------------------------------------------------------------

def load_onnx_model(model_dir: str | Path) -> OnnxNerModel:
    """Load an ONNX GLiNER model from *model_dir*.

    Expected contents::

        model_dir/
            model_quantized.onnx   (or model.onnx)
            tokenizer.json
            gliner_config.json

    Returns an :class:`OnnxNerModel` ready for ``predict_entities()``.
    """
    import onnxruntime as ort  # noqa: PLC0415
    from tokenizers import Tokenizer  # noqa: PLC0415

    model_dir = Path(model_dir)

    # Load ONNX session — prefer full-precision model for accuracy.
    # INT8 quantization degrades NER scores (e.g. person names drop below
    # detection threshold), so we only fall back to quantized when the
    # full-precision model is unavailable.
    onnx_path = model_dir / "model.onnx"
    if not onnx_path.exists():
        onnx_path = model_dir / "model_quantized.onnx"
    if not onnx_path.exists():
        raise FileNotFoundError(f"No ONNX model found in {model_dir}")

    logger.info("Loading ONNX NER model from %s", onnx_path)
    sess_options = ort.SessionOptions()
    sess_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

    # Use CoreML (Metal/ANE) on macOS when available, falling back to CPU.
    providers = ort.get_available_providers()
    preferred = []
    if "CoreMLExecutionProvider" in providers:
        preferred.append("CoreMLExecutionProvider")
    preferred.append("CPUExecutionProvider")

    session = ort.InferenceSession(str(onnx_path), sess_options, providers=preferred)

    # Load tokenizer.
    tok_path = model_dir / "tokenizer.json"
    if not tok_path.exists():
        raise FileNotFoundError(f"tokenizer.json not found in {model_dir}")
    tokenizer = Tokenizer.from_file(str(tok_path))

    # Load config for max_width / max_len / special tokens.
    cfg_path = model_dir / "gliner_config.json"
    max_width = 12
    max_len = 384
    ent_token = "<<ENT>>"
    sep_token = "<<SEP>>"
    if cfg_path.exists():
        with open(cfg_path) as f:
            cfg = json.load(f)
        max_width = cfg.get("max_width", max_width)
        max_len = cfg.get("max_len", max_len)
        ent_token = cfg.get("ent_token", ent_token)
        sep_token = cfg.get("sep_token", sep_token)

    logger.info("Using ONNX NER backend (max_width=%d, max_len=%d)", max_width, max_len)
    return OnnxNerModel(
        session, tokenizer,
        max_width=max_width, max_len=max_len,
        ent_token=ent_token, sep_token=sep_token,
    )
