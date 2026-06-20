"""
Tests for pipeline/validation.py — the independent validation layer.

This is the most important test module. The validation layer is what separates
our pipeline from a naive "trust Claude output" approach. If these checks break,
bad data ships with false confidence scores.

We mock the network calls (validate_url, validate_email_domain) because:
1. Unit tests should be deterministic — real network calls flake
2. We want to test the scoring logic, not the network stack
3. The network functions have their own (separate) integration concerns
"""

import pytest
from unittest.mock import patch
from pipeline.validation import validate_record, passes_minimum_bar


# ---------------------------------------------------------------------------
# passes_minimum_bar
# ---------------------------------------------------------------------------

def test_passes_minimum_bar_accepts_valid_record(full_record):
    full_record["data_completion_score"] = 75
    assert passes_minimum_bar(full_record) is True


def test_passes_minimum_bar_rejects_missing_name(full_record):
    full_record["fo_name"] = None
    assert passes_minimum_bar(full_record) is False


def test_passes_minimum_bar_rejects_missing_location(full_record):
    full_record["hq_city"] = None
    full_record["hq_country"] = None
    assert passes_minimum_bar(full_record) is False


def test_passes_minimum_bar_rejects_low_completion(full_record):
    full_record["data_completion_score"] = 25
    assert passes_minimum_bar(full_record) is False


def test_passes_minimum_bar_exact_threshold(full_record):
    full_record["data_completion_score"] = 30
    assert passes_minimum_bar(full_record) is True


def test_passes_minimum_bar_accepts_country_only_no_city(full_record):
    """Country alone is sufficient for location check — city is optional."""
    full_record["hq_city"] = None
    full_record["data_completion_score"] = 40
    assert passes_minimum_bar(full_record) is True


# ---------------------------------------------------------------------------
# validate_record — website validation
# ---------------------------------------------------------------------------

@patch("pipeline.validation.validate_url")
@patch("pipeline.validation.validate_email_domain")
def test_reachable_website_adds_verification(mock_email, mock_url, full_record):
    mock_url.return_value = {"valid": True, "final_url": "https://summitcapital.com", "status_code": 200}
    mock_email.return_value = {"mx_valid": True}

    result = validate_record(full_record)
    assert "website_reachable" in result["_validations_passed"]


@patch("pipeline.validation.validate_url")
@patch("pipeline.validation.validate_email_domain")
def test_dead_website_clears_url_and_adds_issue(mock_email, mock_url, full_record):
    mock_url.return_value = {"valid": False, "status_code": 404}
    mock_email.return_value = {"mx_valid": True}

    result = validate_record(full_record)
    assert result["website_url"] == ""
    assert any("website_unreachable" in i for i in result["_validation_issues"])


@patch("pipeline.validation.validate_url")
@patch("pipeline.validation.validate_email_domain")
def test_missing_website_adds_issue(mock_email, mock_url, full_record):
    full_record["website_url"] = None
    mock_url.return_value = {}
    mock_email.return_value = {"mx_valid": True}

    result = validate_record(full_record)
    assert "website_missing" in result["_validation_issues"]
    mock_url.assert_not_called()


# ---------------------------------------------------------------------------
# validate_record — LinkedIn URL validation
# ---------------------------------------------------------------------------

@patch("pipeline.validation.validate_url")
@patch("pipeline.validation.validate_email_domain")
def test_invalid_corporate_linkedin_is_cleared(mock_email, mock_url, full_record):
    full_record["corporate_linkedin"] = "https://linkedin.com/wrongformat/page"
    mock_url.return_value = {"valid": True, "final_url": full_record["website_url"], "status_code": 200}
    mock_email.return_value = {"mx_valid": True}

    result = validate_record(full_record)
    assert result["corporate_linkedin"] == ""
    assert "corporate_linkedin_invalid_format" in result["_validation_issues"]


@patch("pipeline.validation.validate_url")
@patch("pipeline.validation.validate_email_domain")
def test_valid_linkedin_url_is_kept(mock_email, mock_url, full_record):
    mock_url.return_value = {"valid": True, "final_url": full_record["website_url"], "status_code": 200}
    mock_email.return_value = {"mx_valid": True}

    result = validate_record(full_record)
    assert "linkedin.com/company/" in result["corporate_linkedin"]


# ---------------------------------------------------------------------------
# validate_record — email validation
# ---------------------------------------------------------------------------

@patch("pipeline.validation.validate_url")
@patch("pipeline.validation.validate_email_domain")
def test_bad_email_domain_sets_not_found_status(mock_email, mock_url, full_record):
    mock_url.return_value = {"valid": True, "final_url": full_record["website_url"], "status_code": 200}
    mock_email.return_value = {"mx_valid": False, "status": "domain-not-found"}

    result = validate_record(full_record)
    assert result["email_validation_status"] == "not-found"
    assert result["email_confidence"] == "low"


@patch("pipeline.validation.validate_url")
@patch("pipeline.validation.validate_email_domain")
def test_valid_email_domain_preserves_confirmed_status(mock_email, mock_url, full_record):
    full_record["email_validation_status"] = "confirmed"
    mock_url.return_value = {"valid": True, "final_url": full_record["website_url"], "status_code": 200}
    mock_email.return_value = {"mx_valid": True}

    result = validate_record(full_record)
    assert result["email_validation_status"] == "confirmed"


# ---------------------------------------------------------------------------
# validate_record — confidence score calculation
# ---------------------------------------------------------------------------

@patch("pipeline.validation.validate_url")
@patch("pipeline.validation.validate_email_domain")
def test_confidence_score_increases_with_verifications(mock_email, mock_url, full_record):
    full_record["confidence_score"] = 5
    mock_url.return_value = {"valid": True, "final_url": full_record["website_url"], "status_code": 200}
    mock_email.return_value = {"mx_valid": True}

    result = validate_record(full_record)
    # 2 verifications (+1.0 bonus), 0 issues → score should increase
    assert result["confidence_score"] > 5


@patch("pipeline.validation.validate_url")
@patch("pipeline.validation.validate_email_domain")
def test_confidence_score_decreases_with_issues(mock_email, mock_url, full_record):
    full_record["confidence_score"] = 8
    full_record["website_url"] = None  # will add website_missing issue
    full_record["principal_email"] = None  # will set not-found
    mock_url.return_value = {}
    mock_email.return_value = {}

    result = validate_record(full_record)
    assert result["confidence_score"] < 8


@patch("pipeline.validation.validate_url")
@patch("pipeline.validation.validate_email_domain")
def test_confidence_score_clamped_to_max_10(mock_email, mock_url, full_record):
    full_record["confidence_score"] = 10
    mock_url.return_value = {"valid": True, "final_url": full_record["website_url"], "status_code": 200}
    mock_email.return_value = {"mx_valid": True}

    result = validate_record(full_record)
    assert result["confidence_score"] <= 10


@patch("pipeline.validation.validate_url")
@patch("pipeline.validation.validate_email_domain")
def test_confidence_score_clamped_to_min_1(mock_email, mock_url, full_record):
    full_record["confidence_score"] = 1
    full_record["website_url"] = None
    full_record["principal_email"] = None
    full_record["hq_city"] = None
    full_record["description"] = None
    mock_url.return_value = {}
    mock_email.return_value = {}

    result = validate_record(full_record)
    assert result["confidence_score"] >= 1


# ---------------------------------------------------------------------------
# validate_record — data completion score
# ---------------------------------------------------------------------------

@patch("pipeline.validation.validate_url")
@patch("pipeline.validation.validate_email_domain")
def test_completion_score_is_percentage(mock_email, mock_url, full_record):
    mock_url.return_value = {"valid": True, "final_url": full_record["website_url"], "status_code": 200}
    mock_email.return_value = {"mx_valid": True}

    result = validate_record(full_record)
    assert 0 <= result["data_completion_score"] <= 100


@patch("pipeline.validation.validate_url")
@patch("pipeline.validation.validate_email_domain")
def test_sparse_record_has_low_completion(mock_email, mock_url, sparse_record):
    sparse_record["website_url"] = None
    sparse_record["principal_email"] = None
    mock_url.return_value = {}
    mock_email.return_value = {}

    result = validate_record(sparse_record)
    assert result["data_completion_score"] < 50
