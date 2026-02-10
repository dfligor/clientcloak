"""Tests for clientcloak.onnx_ner: lightweight ONNX NER inference wrapper.

Tests cover the internal helper functions (word splitting, words_mask,
span generation, decoding) and the public OnnxNerModel.predict_entities()
API with a mocked ONNX session.
"""

import numpy as np
import pytest
from unittest.mock import MagicMock, patch

from clientcloak.onnx_ner import (
    OnnxNerModel,
    _build_spans,
    _build_words_mask,
    _decode_logits,
    _greedy_search,
    _overlaps,
    _sigmoid,
    _split_words,
)


# ===================================================================
# Word splitting
# ===================================================================

class TestSplitWords:

    def test_simple_sentence(self):
        words, starts, ends = _split_words("John Smith works at Google")
        assert words == ["John", "Smith", "works", "at", "Google"]
        assert starts == [0, 5, 11, 17, 20]
        assert ends == [4, 10, 16, 19, 26]

    def test_empty_text(self):
        words, starts, ends = _split_words("")
        assert words == []

    def test_punctuation_as_separate_tokens(self):
        words, starts, ends = _split_words("Hello, world!")
        assert "Hello" in words
        assert "," in words
        assert "world" in words
        assert "!" in words

    def test_hyphenated_word(self):
        words, _, _ = _split_words("well-known fact")
        assert "well-known" in words

    def test_underscored_word(self):
        words, _, _ = _split_words("some_variable here")
        assert "some_variable" in words

    def test_character_offsets_roundtrip(self):
        text = "John Smith works at Google Inc."
        words, starts, ends = _split_words(text)
        for word, s, e in zip(words, starts, ends):
            assert text[s:e] == word


# ===================================================================
# Words mask
# ===================================================================

class TestBuildWordsMask:

    def test_skips_prompt_words(self):
        # word_ids: [None, 0, 1, 1, 2, 3, 4, 5, 6, None]
        # prompt_len = 5 (<<ENT>> person <<ENT>> org <<SEP>>)
        # Words 0-4 are prompt → mask 0
        # Word 5 → mask 1, word 6 → mask 2
        word_ids = [None, 0, 1, 1, 2, 3, 4, 5, 6, None]
        mask = _build_words_mask(word_ids, prompt_len=5)
        assert mask == [0, 0, 0, 0, 0, 0, 0, 1, 2, 0]

    def test_no_prompt(self):
        word_ids = [None, 0, 1, 2, None]
        mask = _build_words_mask(word_ids, prompt_len=0)
        assert mask == [0, 1, 2, 3, 0]

    def test_subword_continuation_masked(self):
        # Word 0 has two subwords
        word_ids = [None, 0, 0, 1, None]
        mask = _build_words_mask(word_ids, prompt_len=0)
        assert mask == [0, 1, 0, 2, 0]

    def test_all_special_tokens(self):
        word_ids = [None, None, None]
        mask = _build_words_mask(word_ids, prompt_len=0)
        assert mask == [0, 0, 0]


# ===================================================================
# Span generation
# ===================================================================

class TestBuildSpans:

    def test_basic_spans(self):
        span_idx, span_mask = _build_spans(3, 2)
        # 3 tokens × 2 widths = 6 spans
        assert len(span_idx) == 6
        assert span_idx[0] == [0, 0]   # (0, 0+0)
        assert span_idx[1] == [0, 1]   # (0, 0+1)
        assert span_idx[2] == [1, 1]   # (1, 1+0)
        assert span_idx[3] == [1, 2]   # (1, 1+1)
        assert span_idx[4] == [2, 2]   # (2, 2+0)
        assert span_idx[5] == [2, 3]   # (2, 2+1) — invalid

    def test_mask_validity(self):
        span_idx, span_mask = _build_spans(3, 2)
        # Last span (2, 3) is invalid since end >= num_tokens
        assert span_mask == [True, True, True, True, True, False]

    def test_single_token(self):
        span_idx, span_mask = _build_spans(1, 3)
        assert len(span_idx) == 3
        assert span_mask == [True, False, False]

    def test_total_count(self):
        span_idx, span_mask = _build_spans(10, 12)
        assert len(span_idx) == 10 * 12


# ===================================================================
# Sigmoid
# ===================================================================

class TestSigmoid:

    def test_zero(self):
        result = _sigmoid(np.array([0.0]))
        np.testing.assert_almost_equal(result, [0.5])

    def test_large_positive(self):
        result = _sigmoid(np.array([100.0]))
        assert result[0] > 0.99

    def test_large_negative(self):
        result = _sigmoid(np.array([-100.0]))
        assert result[0] < 0.01


# ===================================================================
# Overlap detection
# ===================================================================

class TestOverlaps:

    def test_no_overlap(self):
        assert not _overlaps((0, 1, "A", 0.9), (3, 4, "B", 0.8))

    def test_overlap(self):
        assert _overlaps((0, 2, "A", 0.9), (1, 3, "B", 0.8))

    def test_nested(self):
        assert _overlaps((0, 5, "A", 0.9), (1, 3, "B", 0.8))

    def test_adjacent_no_overlap(self):
        # (0,1) and (2,3) don't overlap
        assert not _overlaps((0, 1, "A", 0.9), (2, 3, "B", 0.8))

    def test_touching(self):
        # (0,2) and (2,4) overlap at position 2
        assert _overlaps((0, 2, "A", 0.9), (2, 4, "B", 0.8))


# ===================================================================
# Greedy search
# ===================================================================

class TestGreedySearch:

    def test_keeps_highest_score(self):
        spans = [
            (0, 2, "PERSON", 0.7),
            (1, 3, "PERSON", 0.9),  # overlaps but higher score
        ]
        result = _greedy_search(spans)
        assert len(result) == 1
        assert result[0] == (1, 3, "PERSON", 0.9)

    def test_keeps_non_overlapping(self):
        spans = [
            (0, 1, "PERSON", 0.9),
            (3, 4, "ORG", 0.8),
        ]
        result = _greedy_search(spans)
        assert len(result) == 2

    def test_empty_input(self):
        assert _greedy_search([]) == []

    def test_sorted_by_start(self):
        spans = [
            (5, 6, "ORG", 0.95),
            (0, 1, "PERSON", 0.9),
        ]
        result = _greedy_search(spans)
        assert result[0][0] < result[1][0]


# ===================================================================
# Logit decoding
# ===================================================================

class TestDecodeLogits:

    def test_above_threshold_detected(self):
        # 3 tokens, max_width=2, 1 class
        logits = np.zeros((3, 2, 1), dtype=np.float32)
        # Set high logit for span (0, 0) class 0 → sigmoid ≈ 0.95
        logits[0, 0, 0] = 3.0
        id_to_class = {0: "person"}

        result = _decode_logits(logits, 3, 2, 1, id_to_class, 0.5, flat_ner=True)
        assert len(result) == 1
        assert result[0][0] == 0  # start
        assert result[0][1] == 0  # end
        assert result[0][2] == "person"
        assert result[0][3] > 0.9

    def test_below_threshold_filtered(self):
        logits = np.zeros((3, 2, 1), dtype=np.float32)
        logits[0, 0, 0] = -1.0  # sigmoid ≈ 0.27
        id_to_class = {0: "person"}

        result = _decode_logits(logits, 3, 2, 1, id_to_class, 0.5, flat_ner=True)
        assert len(result) == 0

    def test_invalid_spans_rejected(self):
        # 2 tokens, max_width=3
        logits = np.zeros((2, 3, 1), dtype=np.float32)
        # Span (1, 3) — end (1+2=3) >= num_tokens (2) → invalid
        logits[1, 2, 0] = 5.0
        id_to_class = {0: "person"}

        result = _decode_logits(logits, 2, 3, 1, id_to_class, 0.5, flat_ner=True)
        assert len(result) == 0

    def test_multiple_classes(self):
        logits = np.zeros((5, 2, 2), dtype=np.float32)
        logits[0, 0, 0] = 3.0  # person at (0,0)
        logits[3, 0, 1] = 3.0  # org at (3,3)
        id_to_class = {0: "person", 1: "organization"}

        result = _decode_logits(logits, 5, 2, 2, id_to_class, 0.5, flat_ner=True)
        assert len(result) == 2
        labels = {r[2] for r in result}
        assert labels == {"person", "organization"}


# ===================================================================
# OnnxNerModel.predict_entities() with mocked session
# ===================================================================

class TestOnnxNerModelPredictEntities:

    def _make_model(self, logits_output: np.ndarray, num_classes: int = 1):
        """Create an OnnxNerModel with a mocked ONNX session."""
        session = MagicMock()
        session.get_inputs.return_value = [
            MagicMock(name="input_ids"),
            MagicMock(name="attention_mask"),
            MagicMock(name="words_mask"),
            MagicMock(name="text_lengths"),
            MagicMock(name="span_idx"),
            MagicMock(name="span_mask"),
        ]
        # Set .name attribute explicitly since MagicMock(name=...) sets _mock_name
        for inp, name in zip(
            session.get_inputs.return_value,
            ["input_ids", "attention_mask", "words_mask", "text_lengths", "span_idx", "span_mask"],
        ):
            inp.name = name

        session.run.return_value = [logits_output]

        # Mock tokenizer
        tokenizer = MagicMock()
        encoding = MagicMock()
        # We need to produce valid ids/attention_mask/word_ids
        # For "<<ENT>> person <<SEP>> John Smith" (5 words → prompt=3, text=2)
        # DeBERTa would produce something like:
        # [CLS] <<ENT>> person <<SEP>> John Smith [SEP]
        encoding.ids = [1, 100, 200, 101, 300, 400, 2]
        encoding.attention_mask = [1, 1, 1, 1, 1, 1, 1]
        encoding.word_ids = [None, 0, 1, 2, 3, 4, None]
        tokenizer.encode.return_value = encoding

        model = OnnxNerModel(
            session, tokenizer,
            max_width=12, max_len=384,
        )
        return model, session

    def test_basic_detection(self):
        # Text: "John Smith" → 2 words
        # Logits shape: (1, 2, 12, 1) — batch=1, words=2, max_width=12, classes=1
        logits = np.zeros((1, 2, 12, 1), dtype=np.float32)
        # Span (0, 1) — "John Smith" — width offset = 1
        logits[0, 0, 1, 0] = 3.0  # high confidence

        model, session = self._make_model(logits, num_classes=1)
        entities = model.predict_entities(
            "John Smith", ["person"], threshold=0.5,
        )

        assert len(entities) == 1
        assert entities[0]["text"] == "John Smith"
        assert entities[0]["label"] == "person"
        assert entities[0]["score"] > 0.9

    def test_no_detections(self):
        logits = np.zeros((1, 2, 12, 1), dtype=np.float32)
        model, _ = self._make_model(logits)
        entities = model.predict_entities("John Smith", ["person"], threshold=0.5)
        assert entities == []

    def test_empty_text(self):
        logits = np.zeros((1, 1, 12, 1), dtype=np.float32)
        model, _ = self._make_model(logits)
        entities = model.predict_entities("", ["person"])
        assert entities == []

    def test_label_deduplication(self):
        logits = np.zeros((1, 2, 12, 1), dtype=np.float32)
        model, session = self._make_model(logits)
        # Passing duplicate labels should deduplicate them
        model.predict_entities("John Smith", ["person", "person"], threshold=0.5)
        # Verify tokenizer was called (model ran without error)
        assert session.run.called


# ===================================================================
# Integration: load_onnx_model (file I/O mocked)
# ===================================================================

class TestLoadOnnxModel:

    @patch("clientcloak.onnx_ner.Path")
    def test_missing_onnx_file_raises(self, mock_path_cls):
        """load_onnx_model raises FileNotFoundError if no .onnx file exists."""
        mock_dir = MagicMock()
        mock_path_cls.return_value = mock_dir
        # Neither model_quantized.onnx nor model.onnx exist
        mock_dir.__truediv__ = lambda self, name: MagicMock(exists=MagicMock(return_value=False))

        from clientcloak.onnx_ner import load_onnx_model
        with pytest.raises(FileNotFoundError):
            load_onnx_model("/fake/dir")


# ===================================================================
# Integration with detector: _get_gliner_model ONNX path
# ===================================================================

class TestDetectorOnnxIntegration:

    def test_onnx_path_tried_with_env_var(self, tmp_path, monkeypatch, caplog):
        """_get_gliner_model tries ONNX when CLIENTCLOAK_ONNX_MODEL_DIR is set."""
        import clientcloak.detector as det

        # Reset module-level state.
        det._gliner_model = None
        det._gliner_import_failed = False

        # Point env var at a real directory (but missing model files).
        monkeypatch.setenv("CLIENTCLOAK_ONNX_MODEL_DIR", str(tmp_path))

        # The ONNX load should be attempted and fail (no model files),
        # producing a warning log.  Whether the overall result is None
        # depends on whether the full gliner package is installed in
        # the test environment (it may succeed as a fallback).
        import logging
        with caplog.at_level(logging.WARNING, logger="clientcloak.detector"):
            det._get_gliner_model()

        assert "ONNX NER model failed to load" in caplog.text

        # Clean up.
        det._gliner_model = None
        det._gliner_import_failed = False
