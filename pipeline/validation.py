"""
Phase 3: Validation Layer

Runs after Claude enrichment to independently verify and score each record.
This is the validation layer — it does NOT trust Claude's output at face value.

Checks:
  1. Website URL: HTTP reachable (not just Claude's claim)
  2. Email domain: MX record exists (not just format)
  3. LinkedIn URL: plausible format
  4. Required fields: name, city, country, website present
  5. Confidence score: recalculate based on what's actually verified
  6. Data completion score: fraction of scored fields that are non-null/non-empty
"""

import logging
import re
from pipeline.config import SCORED_FIELDS
from pipeline.tools import validate_url, validate_email_domain

logger = logging.getLogger(__name__)


def _is_populated(val) -> bool:
    return val is not None and str(val).strip() not in ("", "null", "None", "n/a", "N/A", "unknown")


def _validate_linkedin_url(url: str) -> bool:
    if not url:
        return False
    return bool(re.match(r"https?://(www\.)?linkedin\.com/(company|in)/", url.strip()))


def validate_record(record: dict) -> dict:
    """
    Run validation checks on a single enriched record.
    Updates validation_status, confidence_score, and data_completion_score in place.
    Returns the updated record.
    """
    issues = []
    verifications = []

    # --- 1. Website URL check ---
    website = record.get("website_url", "")
    if _is_populated(website):
        result = validate_url(website)
        if result.get("valid"):
            verifications.append("website_reachable")
            record["website_url"] = result.get("final_url", website)
        else:
            issues.append(f"website_unreachable ({result.get('status_code', result.get('error', ''))})")
            record["website_url"] = ""
    else:
        issues.append("website_missing")

    # --- 2. Email domain check ---
    email = record.get("principal_email", "")
    if _is_populated(email):
        result = validate_email_domain(email)
        if result.get("mx_valid"):
            verifications.append("email_domain_valid")
            # Only upgrade to pattern-inferred — never downgrade a confirmed email
            if record.get("email_validation_status") != "confirmed":
                record["email_validation_status"] = "pattern-inferred"
        else:
            issues.append(f"email_domain_invalid ({result.get('status', '')})")
            record["email_validation_status"] = "not-found"
            record["email_confidence"] = "low"
    else:
        record["email_validation_status"] = "not-found"
        record["email_confidence"] = "low"

    # --- 3. LinkedIn URL format check ---
    corp_li = record.get("corporate_linkedin", "")
    if _is_populated(corp_li) and not _validate_linkedin_url(corp_li):
        issues.append("corporate_linkedin_invalid_format")
        record["corporate_linkedin"] = ""

    principal_li = record.get("principal_linkedin", "")
    if _is_populated(principal_li) and not _validate_linkedin_url(principal_li):
        issues.append("principal_linkedin_invalid_format")
        record["principal_linkedin"] = ""

    # --- 4. Required field presence check ---
    required = ["fo_name", "hq_city", "hq_country", "description"]
    for field in required:
        if not _is_populated(record.get(field)):
            issues.append(f"missing_required:{field}")

    # --- 5. Data completion score ---
    populated = sum(1 for f in SCORED_FIELDS if _is_populated(record.get(f)))
    completion_pct = round((populated / len(SCORED_FIELDS)) * 100)
    record["data_completion_score"] = completion_pct

    # --- 6. Validation status ---
    critical_missing = [i for i in issues if "missing_required" in i or "website_unreachable" in i]
    if not issues:
        record["validation_status"] = "validated"
    elif not critical_missing and len(verifications) >= 1:
        record["validation_status"] = "partial"
    else:
        record["validation_status"] = "unverified"

    # --- 7. Recalculate confidence score ---
    # Start from Claude's self-reported score, then adjust down for issues
    base_score = int(record.get("confidence_score") or 5)
    penalty = len(issues) * 0.5
    bonus = len(verifications) * 0.5
    adjusted = max(1, min(10, base_score - penalty + bonus))
    record["confidence_score"] = round(adjusted, 1)

    # Store validation notes in a hidden field for the methodology log
    record["_validation_issues"] = issues
    record["_validations_passed"] = verifications

    logger.info(
        "Validated | name=%s | status=%s | completion=%s%% | confidence=%s | issues=%d | verified=%d",
        record.get("fo_name", "?"), record["validation_status"],
        record["data_completion_score"], record["confidence_score"],
        len(issues), len(verifications),
    )
    return record


def passes_minimum_bar(record: dict) -> bool:
    """
    A record passes the minimum bar for inclusion in the dataset if:
    - It has a name
    - It has at least one of city or country
    - Data completion score >= 30%

    Note: validation_status is NOT checked here. An "unverified" record
    can still pass if it meets the above — the status is surfaced in the
    output so downstream users can apply their own trust threshold.
    Filtering on status here would silently discard records that are
    unverified only because the website is down, not because data is absent.
    """
    if not _is_populated(record.get("fo_name")):
        return False
    if not _is_populated(record.get("hq_city")) and not _is_populated(record.get("hq_country")):
        return False
    if record.get("data_completion_score", 0) < 30:
        return False
    return True
