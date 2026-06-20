"""
Tests for pipeline/discovery.py — deduplication logic.

We only unit-test the parts that don't require network calls: deduplication.
The discovery agent functions (discover_from_sec_edgar, etc.) make live API
calls and belong in integration tests, not here.

Key hypothesis being tested: normalisation is aggressive enough to catch
entity-name variants that map to the same real-world family office,
but conservative enough to keep genuinely different entities separate.
"""

import pytest
from pipeline.discovery import deduplicate


def test_dedup_removes_llc_variant():
    """'Walton Family Office LLC' and 'Walton Family Office' should collapse to one entry."""
    seeds = [
        {"name": "Walton Family Office LLC", "source": "ADV"},
        {"name": "Walton Family Office", "source": "web"},
    ]
    result = deduplicate(seeds)
    assert len(result) == 1
    # ADV wins (first in list = higher priority)
    assert result[0]["source"] == "ADV"


def test_dedup_removes_fo_suffix():
    """The phrase 'family office' is stripped before comparison."""
    seeds = [
        {"name": "Cascade Investment Family Office", "source": "ADV"},
        {"name": "Cascade Investment", "source": "web"},
    ]
    result = deduplicate(seeds)
    assert len(result) == 1


def test_dedup_handles_empty_list():
    assert deduplicate([]) == []


def test_dedup_keeps_distinct_entities():
    """Different names must not collapse into one."""
    seeds = [
        {"name": "Summit Capital Partners", "source": "ADV"},
        {"name": "Apex Wealth Management", "source": "ADV"},
        {"name": "Pinnacle Family Office", "source": "web"},
    ]
    result = deduplicate(seeds)
    assert len(result) == 3


def test_dedup_strips_lp_suffix():
    seeds = [
        {"name": "Redwood Holdings LP", "source": "13F"},
        {"name": "Redwood Holdings", "source": "web"},
    ]
    result = deduplicate(seeds)
    assert len(result) == 1


def test_dedup_is_case_insensitive():
    seeds = [
        {"name": "BLUE HARBOR FAMILY OFFICE", "source": "ADV"},
        {"name": "Blue Harbor Family Office", "source": "web"},
    ]
    result = deduplicate(seeds)
    assert len(result) == 1


def test_dedup_preserves_first_seen_order():
    """After dedup, the returned list should preserve insertion order of unique items."""
    seeds = [
        {"name": "Alpha FO LLC", "source": "ADV"},
        {"name": "Beta FO Ltd", "source": "web"},
        {"name": "Alpha FO", "source": "web"},  # duplicate of first
    ]
    result = deduplicate(seeds)
    assert len(result) == 2
    assert result[0]["name"] == "Alpha FO LLC"
    assert result[1]["name"] == "Beta FO Ltd"


def test_dedup_skips_empty_name():
    """Seeds with empty names should not be included."""
    seeds = [
        {"name": "", "source": "ADV"},
        {"name": "   ", "source": "web"},
        {"name": "Real Family Office LLC", "source": "ADV"},
    ]
    result = deduplicate(seeds)
    assert len(result) == 1
    assert result[0]["name"] == "Real Family Office LLC"
