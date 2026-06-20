"""
Tests for rag/ingest.py — embedding text builder and type coercion.

We do not test the full ingest() function here because it requires an OpenAI
key and a real XLSX file — those belong in integration tests.

What we do test:
- build_embed_text: confirms only semantic fields contribute to the embedding blob
- coerce_types: confirms Excel-style strings and "N/A" values are handled safely

These two functions are the most fragile parts of the ingest path. If either
produces wrong output, Qdrant range filters silently break (wrong types) or
retrieval quality degrades (wrong fields embedded).
"""

import pytest
from rag.ingest import build_embed_text, coerce_types


# ---------------------------------------------------------------------------
# build_embed_text
# ---------------------------------------------------------------------------

def test_build_embed_text_joins_semantic_fields():
    """All populated semantic fields should appear in the output string."""
    record = {
        "fo_name": "Summit Capital",
        "fo_type": "Single Family Office",
        "description": "A growth-focused family office.",
        "investment_thesis": "Long-term value creation.",
        # Non-semantic fields should be excluded
        "principal_email": "jane@summitcapital.com",
        "hq_city": "New York",
        "pipeline_run_date": "2026-06-21",
    }
    text = build_embed_text(record)
    assert "Summit Capital" in text
    assert "growth-focused" in text
    assert "Long-term value creation" in text
    # Email and city are not semantic fields — must not appear in embed text
    assert "jane@" not in text


def test_build_embed_text_skips_null_values():
    """None and null-like values should not contribute a segment."""
    record = {
        "fo_name": "Phantom Office",
        "fo_type": None,
        "description": "null",
        "investment_thesis": "N/a",
    }
    text = build_embed_text(record)
    assert "null" not in text
    assert "N/a" not in text
    assert "Phantom Office" in text


def test_build_embed_text_handles_empty_record():
    """An empty record should produce an empty string, not raise."""
    text = build_embed_text({})
    assert text == ""


def test_build_embed_text_uses_pipe_separator():
    """Fields are joined with ' | ' for readability and boundary clarity."""
    record = {
        "fo_name": "Alpha FO",
        "fo_type": "Multi Family Office",
    }
    text = build_embed_text(record)
    assert " | " in text


def test_build_embed_text_excludes_all_non_semantic_fields():
    """Non-semantic fields like confidence_score and domain must not appear."""
    record = {
        "fo_name": "Test FO",
        "confidence_score": 8.5,
        "domain": "testfo.com",
        "principal_email": "contact@testfo.com",
        "data_completion_score": 80,
        "validation_status": "validated",
        "pipeline_run_date": "2026-06-21",
    }
    text = build_embed_text(record)
    # Only fo_name is a semantic field from the above set
    assert "testfo.com" not in text
    assert "8.5" not in text
    assert "validated" not in text


# ---------------------------------------------------------------------------
# coerce_types
# ---------------------------------------------------------------------------

def test_coerce_types_converts_string_to_int():
    record = {"founding_year": "2005", "data_completion_score": "75"}
    result = coerce_types(record)
    assert result["founding_year"] == 2005
    assert result["data_completion_score"] == 75
    assert isinstance(result["founding_year"], int)


def test_coerce_types_converts_string_to_float():
    record = {"confidence_score": "7.5"}
    result = coerce_types(record)
    assert result["confidence_score"] == 7.5
    assert isinstance(result["confidence_score"], float)


def test_coerce_types_handles_na_gracefully():
    """Excel exports "N/A" as strings — must not raise, must produce None."""
    record = {
        "confidence_score": "N/A",
        "founding_year": "n/a",
        "data_completion_score": "",
    }
    result = coerce_types(record)
    assert result["confidence_score"] is None
    assert result["founding_year"] is None
    assert result["data_completion_score"] is None


def test_coerce_types_handles_none_gracefully():
    """None values must pass through cleanly."""
    record = {
        "confidence_score": None,
        "founding_year": None,
        "aum_year": None,
    }
    result = coerce_types(record)
    assert result["confidence_score"] is None
    assert result["founding_year"] is None


def test_coerce_types_preserves_already_correct_types():
    """Integers and floats already in the right type must not change."""
    record = {"confidence_score": 8.0, "founding_year": 2010}
    result = coerce_types(record)
    assert result["confidence_score"] == 8.0
    assert result["founding_year"] == 2010


def test_coerce_types_does_not_touch_string_fields():
    """Fields not in the coercion list must be untouched."""
    record = {
        "fo_name": "Test FO",
        "confidence_score": 7.5,
        "hq_city": "London",
    }
    result = coerce_types(record)
    assert result["fo_name"] == "Test FO"
    assert result["hq_city"] == "London"
