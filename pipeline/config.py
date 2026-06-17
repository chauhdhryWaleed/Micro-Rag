import os
from dotenv import load_dotenv

load_dotenv()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
OPENAI_API_KEY    = os.getenv("OPENAI_API_KEY")
GEMINI_API_KEY    = os.getenv("GEMINI_API_KEY")

# PROVIDER controls which LLM backs the enrichment and discovery agents.
# Set to "openai" or "claude" in .env (or export before running).
# Switching providers does not require any code changes — only this setting.
PROVIDER = os.getenv("PROVIDER", "openai")

# Model names per provider. Override via .env if needed.
CLAUDE_MODEL  = os.getenv("CLAUDE_MODEL",  "claude-sonnet-4-6")
OPENAI_MODEL  = os.getenv("OPENAI_MODEL",  "gpt-5-nano")

# Active model (resolved at import time based on PROVIDER)
MODEL = OPENAI_MODEL if PROVIDER == "openai" else CLAUDE_MODEL

MAX_TOKENS = 8096
TARGET_RECORDS = 50
# 120 buffer based on observed 55% acceptance rate from trial run (10/18 accepted).
# To reach 50 records: 50 / 0.55 = 91 minimum seeds. 120 provides margin for variance.
DISCOVERY_BUFFER = 120

# Seconds to sleep between enrichment calls to avoid rate limits
ENRICHMENT_DELAY = 2
SEARCH_DELAY = 1

# Output paths
OUTPUT_DIR = "dataset"
XLSX_PATH = f"{OUTPUT_DIR}/family_offices.xlsx"
PROGRESS_PATH = f"{OUTPUT_DIR}/progress.json"
METHODOLOGY_PATH = f"{OUTPUT_DIR}/methodology.json"

# Schema: ordered list of output columns
SCHEMA_FIELDS = [
    # --- ENTITY ATTRIBUTES ---
    "fo_name",
    "fo_type",              # Single Family Office / Multi Family Office
    "description",
    "investment_thesis",
    "investment_mandate",
    "aum_estimate",         # e.g. "$500M–$1B"
    "aum_year",             # year AUM was reported
    "founding_year",
    "hq_street",
    "hq_city",
    "hq_state",
    "hq_country",
    "website_url",
    "domain",
    "corporate_linkedin",
    "asset_classes",        # comma-separated
    "geographic_focus",

    # --- PRINCIPAL / DECISION-MAKER INTELLIGENCE ---
    "principal_first_name",
    "principal_last_name",
    "principal_full_name",
    "principal_title",
    "principal_linkedin",
    "principal_email",
    "email_validation_status",  # confirmed / pattern-inferred / not-found
    "email_confidence",         # high / medium / low
    "principal_phone",

    # --- ENTITY SIGNALS / RECENT ACTIVITIES ---
    "recent_investment_1",
    "recent_investment_1_date",
    "recent_investment_2",
    "recent_investment_2_date",
    "recent_fund_commitment",
    "recent_key_hire",
    "recent_news_headline",
    "recent_news_date",
    "recent_news_source",
    "recent_news_url",

    # --- VALIDATION / META ---
    "discovery_source",
    "discovery_url",
    "enrichment_sources",         # pipe-separated source URLs
    "validation_status",          # validated / partial / unverified
    "confidence_score",           # 1–10
    "data_completion_score",      # 0–100 (% of substantive fields populated)
    "field_confidence_notes",     # JSON: per-field epistemic labels (verified/inferred/assumed/speculative)
    "pipeline_run_date",
]

# Fields that count toward data_completion_score (excludes meta fields)
SCORED_FIELDS = [
    "fo_name", "fo_type", "description", "investment_thesis", "investment_mandate",
    "aum_estimate", "founding_year", "hq_city", "hq_country", "website_url",
    "corporate_linkedin", "asset_classes", "geographic_focus",
    "principal_full_name", "principal_title", "principal_linkedin",
    "principal_email", "principal_phone",
    "recent_investment_1", "recent_fund_commitment", "recent_news_headline",
]

COLUMN_HEADERS = {
    "fo_name": "Family Office Name",
    "fo_type": "FO Type",
    "description": "Description",
    "investment_thesis": "Investment Thesis",
    "investment_mandate": "Investment Mandate",
    "aum_estimate": "AUM Estimate",
    "aum_year": "AUM Year",
    "founding_year": "Founding Year",
    "hq_street": "HQ Street",
    "hq_city": "HQ City",
    "hq_state": "HQ State / Region",
    "hq_country": "HQ Country",
    "website_url": "Website URL",
    "domain": "Domain",
    "corporate_linkedin": "Corporate LinkedIn",
    "asset_classes": "Asset Classes / Sectors",
    "geographic_focus": "Geographic Focus",
    "principal_first_name": "Principal First Name",
    "principal_last_name": "Principal Last Name",
    "principal_full_name": "Principal Full Name",
    "principal_title": "Principal Title",
    "principal_linkedin": "Principal LinkedIn",
    "principal_email": "Principal Email",
    "email_validation_status": "Email Validation Status",
    "email_confidence": "Email Confidence",
    "principal_phone": "Principal Phone",
    "recent_investment_1": "Recent Investment 1",
    "recent_investment_1_date": "Recent Investment 1 Date",
    "recent_investment_2": "Recent Investment 2",
    "recent_investment_2_date": "Recent Investment 2 Date",
    "recent_fund_commitment": "Recent Fund Commitment",
    "recent_key_hire": "Recent Key Hire",
    "recent_news_headline": "Recent News Headline",
    "recent_news_date": "Recent News Date",
    "recent_news_source": "Recent News Source",
    "recent_news_url": "Recent News URL",
    "discovery_source": "Discovery Source",
    "discovery_url": "Discovery URL",
    "enrichment_sources": "Enrichment Sources",
    "validation_status": "Validation Status",
    "confidence_score": "Confidence Score (1–10)",
    "data_completion_score": "Data Completion Score (%)",
    "field_confidence_notes": "Field Confidence Notes (JSON)",
    "pipeline_run_date": "Pipeline Run Date",
}
