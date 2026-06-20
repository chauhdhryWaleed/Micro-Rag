"""
Shared fixtures for the test suite.

Principles:
- No live API calls in unit tests — patch at the call site
- Fixtures represent realistic data shapes so tests catch schema drift
- Use pytest fixtures rather than setUp/tearDown for composability
"""

import pytest


@pytest.fixture
def minimal_seed():
    """Smallest valid seed that the enrichment agent would accept."""
    return {
        "name": "Acme Family Office",
        "source": "SEC EDGAR Form ADV",
        "source_url": "https://www.sec.gov/cgi-bin/browse-edgar?CIK=0001234567",
        "cik": "0001234567",
    }


@pytest.fixture
def full_record():
    """A fully-populated record that should pass all validation checks."""
    return {
        "fo_name": "Summit Capital Partners",
        "fo_type": "Single Family Office",
        "description": "A single family office managing wealth for a technology entrepreneur family.",
        "investment_thesis": "Long-term growth via private equity and venture capital.",
        "investment_mandate": "Min $5M check. B2B SaaS, healthtech. North America focus.",
        "aum_estimate": "$500M–$1B",
        "aum_year": 2024,
        "founding_year": 2010,
        "hq_street": "100 Park Ave",
        "hq_city": "New York",
        "hq_state": "New York",
        "hq_country": "United States",
        "website_url": "https://summitcapital.com",
        "domain": "summitcapital.com",
        "corporate_linkedin": "https://www.linkedin.com/company/summit-capital-partners",
        "asset_classes": "Private Equity, Venture Capital",
        "geographic_focus": "North America",
        "principal_first_name": "Jane",
        "principal_last_name": "Doe",
        "principal_full_name": "Jane Doe",
        "principal_title": "Chief Investment Officer",
        "principal_linkedin": "https://www.linkedin.com/in/janedoe",
        "principal_email": "jane.doe@summitcapital.com",
        "email_validation_status": "confirmed",
        "email_confidence": "high",
        "principal_phone": "",
        "recent_investment_1": "Acme Corp (Series B)",
        "recent_investment_1_date": "2025-03-01",
        "recent_investment_2": "BetaTech (Seed)",
        "recent_investment_2_date": "2025-01-15",
        "recent_fund_commitment": "$50M commitment to XYZ Fund III",
        "recent_key_hire": "Hired Sarah Lee as Head of Real Estate",
        "recent_news_headline": "Summit Capital leads $30M round in Acme Corp",
        "recent_news_date": "2025-03-01",
        "recent_news_source": "TechCrunch",
        "recent_news_url": "https://techcrunch.com/2025/03/01/acme-round",
        "discovery_source": "SEC EDGAR Form ADV",
        "discovery_url": "https://www.sec.gov",
        "enrichment_sources": "https://summitcapital.com, https://techcrunch.com",
        "validation_status": "partial",
        "confidence_score": 7.0,
        "data_completion_score": 75,
        "pipeline_run_date": "2026-06-21",
    }


@pytest.fixture
def sparse_record():
    """Record with only the minimum fields — should fail minimum bar if completion too low."""
    return {
        "fo_name": "Ghost Capital LLC",
        "fo_type": None,
        "description": None,
        "hq_city": "Chicago",
        "hq_state": None,
        "hq_country": "United States",
        "website_url": None,
        "domain": None,
        "confidence_score": 3.0,
        "data_completion_score": 10,
        "validation_status": "unverified",
    }
