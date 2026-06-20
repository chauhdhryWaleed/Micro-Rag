"""
Tests for rag/retrieval.py — filter building and fallback logic.

Filter building is pure logic — no network calls needed.
Search tests mock Qdrant and OpenAI so we test the retrieval flow
(filter → embed → search → fallback) without paying for API calls or
requiring a live Qdrant instance.
"""

import pytest
from unittest.mock import MagicMock, patch
from qdrant_client.models import Filter


# ---------------------------------------------------------------------------
# build_qdrant_filter
# ---------------------------------------------------------------------------

def test_build_qdrant_filter_returns_none_for_empty():
    from rag.retrieval import build_qdrant_filter
    result = build_qdrant_filter({})
    assert result is None


def test_build_qdrant_filter_returns_none_for_all_null():
    from rag.retrieval import build_qdrant_filter
    result = build_qdrant_filter({
        "fo_type": None,
        "hq_country": None,
        "confidence_score_min": None,
    })
    assert result is None


def test_build_qdrant_filter_single_string_condition():
    from rag.retrieval import build_qdrant_filter
    result = build_qdrant_filter({"hq_country": "United States"})
    assert result is not None
    assert isinstance(result, Filter)
    assert len(result.must) == 1
    assert result.must[0].key == "hq_country"


def test_build_qdrant_filter_multiple_conditions():
    from rag.retrieval import build_qdrant_filter
    result = build_qdrant_filter({
        "hq_country": "United States",
        "fo_type": "Single Family Office",
    })
    assert result is not None
    assert len(result.must) == 2
    keys = {c.key for c in result.must}
    assert "hq_country" in keys
    assert "fo_type" in keys


def test_build_qdrant_filter_range_confidence():
    from rag.retrieval import build_qdrant_filter
    result = build_qdrant_filter({"confidence_score_min": 7})
    assert result is not None
    assert len(result.must) == 1
    condition = result.must[0]
    assert condition.key == "confidence_score"
    assert condition.range.gte == 7.0


def test_build_qdrant_filter_range_completion():
    from rag.retrieval import build_qdrant_filter
    result = build_qdrant_filter({"data_completion_score_min": 50})
    assert result is not None
    condition = result.must[0]
    assert condition.key == "data_completion_score"
    assert condition.range.gte == 50.0


def test_build_qdrant_filter_mixed_string_and_range():
    from rag.retrieval import build_qdrant_filter
    result = build_qdrant_filter({
        "hq_country": "United Kingdom",
        "confidence_score_min": 6,
        "fo_type": None,  # null — should be excluded
    })
    assert result is not None
    assert len(result.must) == 2
    keys = {c.key for c in result.must}
    assert "hq_country" in keys
    assert "confidence_score" in keys
    assert "fo_type" not in keys


# ---------------------------------------------------------------------------
# search — fallback behaviour
# ---------------------------------------------------------------------------

@patch("rag.retrieval.extract_filters")
@patch("rag.retrieval.get_oai")
@patch("rag.retrieval.get_qdrant_client")
def test_search_uses_fallback_when_filtered_results_empty(
    mock_qdrant_fn, mock_oai_fn, mock_extract
):
    """
    When the filter-then-search returns 0 results, the function must retry
    without filters and set fallback_used=True in the response.
    """
    from rag.retrieval import search

    mock_extract.return_value = {
        "filters": {"hq_country": "Narnia"},
        "semantic_query": "tech focused offices",
    }

    # Mock OpenAI embedding
    mock_oai = MagicMock()
    mock_oai.embeddings.create.return_value = MagicMock(
        data=[MagicMock(embedding=[0.1] * 1536)]
    )
    mock_oai_fn.return_value = mock_oai

    # Mock Qdrant: first call (with filter) returns [], second call (no filter) returns hits
    fake_hit = MagicMock()
    fake_hit.payload = {"fo_name": "Fallback Result", "confidence_score": 6.0}
    fake_hit.score = 0.85

    mock_qdrant = MagicMock()
    mock_qdrant.search.side_effect = [[], [fake_hit]]
    mock_qdrant_fn.return_value = mock_qdrant

    result = search("tech family offices in Narnia", top_k=5, qdrant=mock_qdrant)

    assert result["fallback_used"] is True
    assert len(result["results"]) == 1
    assert result["results"][0]["fo_name"] == "Fallback Result"


@patch("rag.retrieval.extract_filters")
@patch("rag.retrieval.get_oai")
@patch("rag.retrieval.get_qdrant_client")
def test_search_no_fallback_when_filtered_results_exist(
    mock_qdrant_fn, mock_oai_fn, mock_extract
):
    """When filters return results, fallback should NOT be triggered."""
    from rag.retrieval import search

    mock_extract.return_value = {
        "filters": {"hq_country": "United States"},
        "semantic_query": "growth-focused offices",
    }

    mock_oai = MagicMock()
    mock_oai.embeddings.create.return_value = MagicMock(
        data=[MagicMock(embedding=[0.1] * 1536)]
    )
    mock_oai_fn.return_value = mock_oai

    fake_hit = MagicMock()
    fake_hit.payload = {"fo_name": "Summit Capital", "confidence_score": 8.0}
    fake_hit.score = 0.92

    mock_qdrant = MagicMock()
    mock_qdrant.search.return_value = [fake_hit]
    mock_qdrant_fn.return_value = mock_qdrant

    result = search("growth focused US family offices", top_k=5, qdrant=mock_qdrant)

    assert result["fallback_used"] is False
    assert mock_qdrant.search.call_count == 1  # no retry


@patch("rag.retrieval.extract_filters")
@patch("rag.retrieval.get_oai")
@patch("rag.retrieval.get_qdrant_client")
def test_search_similarity_score_attached_to_results(
    mock_qdrant_fn, mock_oai_fn, mock_extract
):
    """_similarity_score must be attached to every result record."""
    from rag.retrieval import search

    mock_extract.return_value = {"filters": {}, "semantic_query": "any query"}

    mock_oai = MagicMock()
    mock_oai.embeddings.create.return_value = MagicMock(
        data=[MagicMock(embedding=[0.0] * 1536)]
    )
    mock_oai_fn.return_value = mock_oai

    fake_hit = MagicMock()
    fake_hit.payload = {"fo_name": "Alpha FO"}
    fake_hit.score = 0.77

    mock_qdrant = MagicMock()
    mock_qdrant.search.return_value = [fake_hit]
    mock_qdrant_fn.return_value = mock_qdrant

    result = search("any query", top_k=5, qdrant=mock_qdrant)

    assert result["results"][0]["_similarity_score"] == 0.77
