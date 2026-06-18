"""
Phase 2: Enrichment

For each seed family office, runs an LLM tool-use agent (gpt-5-nano via OpenAI,
or Claude if PROVIDER=claude) that:
  1. Searches for entity attributes (website, description, AUM, thesis, sectors)
  2. Searches for principal / decision-maker intelligence
  3. Searches for recent signals (investments, hires, news)
  4. Scrapes the website for additional detail

Returns a structured dict matching SCHEMA_FIELDS.
"""

import json
import logging
import time
import re
from datetime import date
from pipeline.config import (
    ANTHROPIC_API_KEY, OPENAI_API_KEY, PROVIDER,
    MODEL, MAX_TOKENS, SCHEMA_FIELDS, ENRICHMENT_DELAY
)
from pipeline.model_client import ModelClient
from pipeline.tools import TOOL_DEFINITIONS, dispatch_tool
from pipeline.utils import retry_with_backoff

logger = logging.getLogger(__name__)


def _extract_json(text: str) -> dict | None:
    """
    Extract the first valid JSON object from model output.

    gpt-5-nano sometimes returns multiple JSON objects or JSON followed by
    trailing text. json.loads() raises 'Extra data' in those cases.
    This function tries progressively narrower extractions until one parses.
    """
    if not text:
        return None

    # Strategy 1: greedy match — works when model outputs exactly one object
    match = re.search(r"\{[\s\S]*\}", text)
    if match:
        candidate = match.group()
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

    # Strategy 2: find first '{' and scan forward to find the matching '}'
    # Handles cases where the model output extra text after the JSON object
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape_next = False
    for i, ch in enumerate(text[start:], start):
        if escape_next:
            escape_next = False
            continue
        if ch == "\\" and in_string:
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                candidate = text[start : i + 1]
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    return None
    return None

ENRICHMENT_SYSTEM_PROMPT = """You are a research agent building a high-quality family office intelligence dataset.
The current year is 2026. When searching for recent signals and news, focus on 2024–2026.


For each family office you are given, use the available tools to gather and extract
the following information. You MUST call tools to find real data — do not hallucinate
or guess values.

=== FIELDS TO POPULATE ===

ENTITY ATTRIBUTES:
- fo_name: Official name of the family office
- fo_type: "Single Family Office" or "Multi Family Office" (infer from what you find)
- description: 2-4 sentence factual description of the family office
- investment_thesis: Their stated or inferred investment philosophy and approach
- investment_mandate: Specific mandate constraints (e.g., "min $5M check size, B2B SaaS focus")
- aum_estimate: Approximate AUM with range (e.g., "$500M–$1B") — note the source
- aum_year: Year the AUM figure was reported
- founding_year: Year established
- hq_street, hq_city, hq_state, hq_country: Full headquarters address
- website_url: Official website (verify it exists)
- domain: Just the domain name (e.g., "cascadeinvestment.com")
- corporate_linkedin: LinkedIn company page URL
- asset_classes: Comma-separated (e.g., "Private Equity, Real Estate, Public Equities")
- geographic_focus: Where they invest (e.g., "North America, Europe")

PRINCIPAL INTELLIGENCE:
- principal_first_name, principal_last_name, principal_full_name: Key decision-maker
- principal_title: Their title (e.g., "Chief Investment Officer", "Managing Director")
- principal_linkedin: LinkedIn profile URL
- principal_email: Best available email. Try the pattern first@last.domain or first@domain,
  then check if it's findable. Mark the status accurately.
- email_validation_status: "confirmed" (found on website/directory) | "pattern-inferred" | "not-found"
- email_confidence: "high" | "medium" | "low"
- principal_phone: Direct phone if findable, otherwise leave empty

ENTITY SIGNALS (last 12–24 months):
- recent_investment_1, recent_investment_1_date: Most recent known investment
- recent_investment_2, recent_investment_2_date: Second most recent investment
- recent_fund_commitment: Recent commitment to a fund or LP position
- recent_key_hire: Notable hire or leadership change
- recent_news_headline: Most significant recent news headline
- recent_news_date: Date of that headline (YYYY-MM-DD)
- recent_news_source: Publication name
- recent_news_url: URL to the article

=== SOURCING DISCIPLINE ===
- Only populate fields you found evidence for. Leave unknown fields as null.
- Mark inferred values clearly in email_validation_status.
- The enrichment_sources field should list all URLs you actually retrieved data from.
- discovery_source and discovery_url come from the seed record provided to you.
- confidence_score: Your overall confidence in the record (1=mostly guessed, 10=fully verified).

=== EPISTEMIC LABELING — REQUIRED ===
Populate field_confidence_notes as a JSON object labeling the provenance of key inferred fields.
Use these four labels exactly:
  - "verified"    — you saw it directly on an official source (website, SEC filing, news article)
  - "inferred"    — derived from evidence but not directly stated (e.g., AUM range from fund size mentions)
  - "assumed"     — working assumption not confirmed by a source (e.g., email pattern constructed)
  - "speculative" — low confidence, likely but not supported by specific evidence

Include notes for: aum_estimate, investment_thesis, investment_mandate, principal_email,
fo_type, and any other field where you had to reason beyond what sources directly stated.

Example field_confidence_notes:
{
  "aum_estimate": "[inferred] from 2025 Bloomberg article citing $800M in managed assets",
  "investment_thesis": "[inferred] from website language — no explicit mandate document found",
  "fo_type": "[verified] stated as Single Family Office on their About page",
  "principal_email": "[assumed] pattern-constructed as j.smith@domain.com — not found on website"
}

=== OUTPUT FORMAT ===
When you have gathered all available information, output a single JSON object with
exactly these keys (use null for unknown fields):

{
  "fo_name": "...",
  "fo_type": "...",
  "description": "...",
  "investment_thesis": "...",
  "investment_mandate": "...",
  "aum_estimate": "...",
  "aum_year": null,
  "founding_year": null,
  "hq_street": "...",
  "hq_city": "...",
  "hq_state": "...",
  "hq_country": "...",
  "website_url": "...",
  "domain": "...",
  "corporate_linkedin": "...",
  "asset_classes": "...",
  "geographic_focus": "...",
  "principal_first_name": "...",
  "principal_last_name": "...",
  "principal_full_name": "...",
  "principal_title": "...",
  "principal_linkedin": "...",
  "principal_email": "...",
  "email_validation_status": "...",
  "email_confidence": "...",
  "principal_phone": "...",
  "recent_investment_1": "...",
  "recent_investment_1_date": "...",
  "recent_investment_2": "...",
  "recent_investment_2_date": "...",
  "recent_fund_commitment": "...",
  "recent_key_hire": "...",
  "recent_news_headline": "...",
  "recent_news_date": "...",
  "recent_news_source": "...",
  "recent_news_url": "...",
  "discovery_source": "...",
  "discovery_url": "...",
  "enrichment_sources": "...",
  "validation_status": "partial",
  "confidence_score": 7,
  "data_completion_score": 0,
  "field_confidence_notes": {
    "aum_estimate": "[inferred/verified/assumed/speculative] ...",
    "investment_thesis": "[inferred/verified/assumed/speculative] ..."
  },
  "pipeline_run_date": "..."
}

Output ONLY the JSON object. No other text.
"""


def enrich_family_office(seed: dict) -> dict | None:
    """
    Run a tool-use agent to enrich a single family office seed record.
    Provider (Claude or OpenAI) is controlled by PROVIDER in config.
    Returns the enriched record dict, or None if enrichment fails.
    """
    time.sleep(ENRICHMENT_DELAY)
    name = seed.get("name", "Unknown")
    logger.info("Enriching [%s]: %s", PROVIDER, name)

    api_key = OPENAI_API_KEY if PROVIDER == "openai" else ANTHROPIC_API_KEY
    client = ModelClient(provider=PROVIDER, api_key=api_key, model=MODEL, max_tokens=MAX_TOKENS)

    user_message = f"""Enrich this family office:

Name: {name}
Discovery Source: {seed.get('source', '')}
Discovery URL: {seed.get('source_url', '')}
CIK (if SEC): {seed.get('cik', '')}

Search for this family office and populate all available fields.
Start with a broad search, then narrow down for principal and signals."""

    messages = [{"role": "user", "content": user_message}]
    # Research phase: capped at 6 iterations to prevent context overflow on long pages.
    # gpt-5-nano reasoning tokens grow with context; at 8 iterations the forced output
    # phase sometimes returns empty content because 8096 tokens are consumed by reasoning.
    # Output phase: tools disabled + 20k token budget to guarantee space for reasoning + JSON.
    max_research_iterations = 6
    iterations = 0

    while iterations < max_research_iterations:
        iterations += 1

        try:
            response = retry_with_backoff(
                client.chat,
                system=ENRICHMENT_SYSTEM_PROMPT,
                messages=messages,
                tools=TOOL_DEFINITIONS,
            )
        except Exception as e:
            logger.error("Enrichment API error | provider=%s | name=%s | error=%s", PROVIDER, name, e)
            return None

        tool_calls = response["tool_calls"]

        if not tool_calls:
            # Model voluntarily stopped using tools — extract JSON
            raw = (response["text"] or "").strip()
            record = _extract_json(raw)
            if record is not None:
                record["pipeline_run_date"] = str(date.today())
                record["fo_name"] = record.get("fo_name") or name
                notes = record.get("field_confidence_notes")
                if isinstance(notes, dict):
                    record["field_confidence_notes"] = json.dumps(notes, ensure_ascii=False)
                return record
            # No JSON in the text response — fall through to forced output phase
            break

        # Execute tool calls and continue research loop
        tool_results = []
        for tc in tool_calls:
            logger.debug("Tool call | name=%s | tool=%s | inputs=%s",
                         name, tc["name"], list(tc["input"].keys()))
            result_str = dispatch_tool(tc["name"], tc["input"])
            tool_results.append({"tool_call_id": tc["id"], "content": result_str})
        messages.extend(client.make_tool_turn(response, tool_results))

    # Forced output phase: tools disabled so gpt-5-nano MUST produce JSON text
    # gpt-5-nano ignores user-message nudges but will output text when it cannot call tools
    logger.debug("Entering forced output phase | name=%s | iterations_used=%d", name, iterations)

    # Two-attempt forced output strategy:
    # Attempt 1: explicit instruction to output JSON
    # Attempt 2 (if attempt 1 returns tool-use-formatted text): stronger "no tools" instruction
    for attempt in range(2):
        nudge = (
            "Research phase complete. You must now output ONLY the final JSON object with all "
            "fields you have gathered. Do not explain or add any text outside the JSON."
            if attempt == 0 else
            "OUTPUT JSON NOW. Do NOT output any tool_use, tool_calls, or function calls. "
            f"Your FINAL answer for {name} must be a single JSON object starting with {{ and ending with }}. "
            "No other text. No tool use. Pure JSON only."
        )
        messages.append({"role": "user", "content": nudge})

        try:
            response = retry_with_backoff(
                client.chat,
                system=ENRICHMENT_SYSTEM_PROMPT,
                messages=messages,
                tools=None,          # disabled — forces text output, no tool calls possible
                max_tokens=20000,    # large budget so reasoning + JSON both fit
            )
            raw = (response["text"] or "").strip()

            # Detect tool-use-as-text — model is stuck in tool mode, extend context and retry
            if raw.startswith('{"tool_use') or '"tool_uses"' in raw[:50]:
                logger.debug("Forced output returned tool-use text | name=%s | attempt=%d | retrying", name, attempt)
                # Add the tool-use response as assistant turn so next request has full context
                messages.append({"role": "assistant", "content": raw})
                continue

            record = _extract_json(raw)
            if record is not None:
                record["pipeline_run_date"] = str(date.today())
                record["fo_name"] = record.get("fo_name") or name
                notes = record.get("field_confidence_notes")
                if isinstance(notes, dict):
                    record["field_confidence_notes"] = json.dumps(notes, ensure_ascii=False)
                logger.info("JSON extracted in forced output phase | name=%s | attempt=%d", name, attempt)
                return record
            logger.warning("No JSON in forced output response | name=%s | attempt=%d | raw_preview=%s",
                           name, attempt, raw[:100])
        except Exception as e:
            logger.error("Forced output phase API error | name=%s | attempt=%d | error=%s", name, attempt, e)

    logger.warning("No JSON produced after forced output | name=%s | iterations=%d", name, iterations)
    return None
