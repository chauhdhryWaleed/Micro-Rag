"""
Phase 1: Discovery

Three-pronged seed strategy, updated after validating what EDGAR actually indexes.

  1. SEC EDGAR Form 13F  — quarterly equity holdings reports. Any institutional manager
                           holding >$100M in public equities must file regardless of
                           registration status — no family office exemption applies.
                           We paginate up to 600 hits to extract ~44 unique entities
                           that explicitly mention "family office" in their filing text.
                           Field names in 13F responses differ from other EDGAR forms:
                           display_names (list) and ciks (list), not entity_name/entity_id.
                           [verified: confirmed via direct API testing 2026-06-21]

  2. SEC EDGAR Form D    — Regulation D private placement filings. Family offices
                           frequently create investment vehicles (funds, SPVs) that
                           trigger a Form D. Catches a different population than 13F:
                           smaller offices that don't hit the $100M public equity threshold.
                           Higher non-FO noise than 13F (VC funds, PE funds also file D),
                           but enrichment agent filters non-FOs in Phase 2.
                           [inferred] Estimated FO fraction: 30-50%.

  3. Gemini web search   — everything EDGAR structurally cannot see: SFOs below $100M,
                           offices with generic entity names, directories, rankings, press.
                           Target raised to 60 names after the 15-name target proved
                           insufficient (combined EDGAR + web of 18 seeds → only 10 records).

Why not Form ADV?
  Form ADV is filed with the SEC's IAPD system (separate from EDGAR). The EFTS
  full-text search index does not include ADV filings — confirmed: every ADV query
  returns exactly 0 hits. A fourth leg targeting IAPD would require scraping its
  web interface, which is out of scope for v1.
  [verified: confirmed via direct API testing 2026-06-21]

No single source covers the full FO universe. All three together still miss sub-$100M
SFOs with no web presence — structural gap, not a bug.
"""

import json
import logging
import re
import time
from pipeline.config import ANTHROPIC_API_KEY, OPENAI_API_KEY, PROVIDER, MODEL, MAX_TOKENS, DISCOVERY_BUFFER
from pipeline.model_client import ModelClient
from pipeline.tools import search_web, search_sec_edgar, TOOL_DEFINITIONS, dispatch_tool
from pipeline.utils import retry_with_backoff

logger = logging.getLogger(__name__)


def discover_from_sec_13f(pages: int = 3) -> list[dict]:
    """
    13F catch: paginated across multiple pages to extract all unique filers.

    The EDGAR EFTS API returns up to 100 hits per page, but the same entity files
    13F quarterly — 100 hits often contain only ~24 unique entities. Paginating
    across 3 pages (300 hits total) extracts ~30-35 unique entities that explicitly
    mention "family office" in their filing text.

    Pages capped at 3 (from=0, 100, 200) because EDGAR's EFTS returns a 500 error
    at from=300 for this query. Confirmed 2026-06-21 via live run.
    [verified: field names and pagination cap confirmed 2026-06-21]

    Deduplication by CIK within this function ensures quarterly re-filers are
    counted once. The enrichment agent confirms whether each is a genuine FO.
    """
    logger.info("Querying SEC EDGAR Form 13F (paginated across %d pages)...", pages)
    seen_ciks = set()
    seeds = []
    for page in range(pages):
        result = search_sec_edgar("family office", limit=100, form_type="13F-HR",
                                   from_offset=page * 100)
        # Distinguish API error from genuinely empty page
        if result.get("error"):
            logger.warning("13F page %d failed with API error — stopping pagination | error=%s",
                           page, result["error"])
            break
        page_hits = result.get("results", [])
        new_this_page = 0
        for hit in page_hits:
            name = hit.get("entity_name", "").strip()
            cik = hit.get("cik", "")
            if not name or len(name) <= 3:
                continue
            # CIK dedup: same entity files quarterly, don't count it twice
            if cik and cik in seen_ciks:
                continue
            if cik:
                seen_ciks.add(cik)
            seeds.append({
                "name": name,
                "source": "SEC EDGAR Form 13F",
                "source_url": hit.get("filing_url", ""),
                "cik": cik,
                "location": hit.get("location", ""),
            })
            new_this_page += 1
        logger.debug("13F page %d: %d new unique entities (running total=%d)",
                     page, new_this_page, len(seeds))
        if not page_hits:
            logger.info("13F pagination complete at page %d (no more results)", page)
            break
    logger.info("13F discovery complete: %d unique entities", len(seeds))
    return seeds


def discover_from_sec_form_d(limit: int = 40) -> list[dict]:
    """
    Form D catch: Regulation D private placement filings.

    Family offices often create investment vehicles (fund-of-one, SPVs) that trigger
    a Form D filing. Catches offices that don't hit the 13F equity threshold but still
    run structured investment programs. Higher noise than 13F — VC funds, PE funds,
    and real estate syndicators also file D. Enrichment agent filters non-FOs at a cost
    of one API call per false positive, which we accept since missed offices can't be
    recovered downstream.
    [inferred] FO fraction in D filers mentioning "family office": ~30-50%.
    """
    logger.info("Querying SEC EDGAR Form D for family office fund vehicles...")
    result = search_sec_edgar("family office", limit=limit, form_type="D")
    seeds = []
    for hit in result.get("results", []):
        name = hit.get("entity_name", "").strip()
        if name and len(name) > 3:
            seeds.append({
                "name": name,
                "source": "SEC EDGAR Form D",
                "source_url": hit.get("filing_url", ""),
                "cik": hit.get("cik", ""),
            })
    logger.info("Form D returned %d candidates (higher non-FO noise than 13F)", len(seeds))
    return seeds


def discover_from_web(target: int = 60) -> list[dict]:
    """
    Web catch: everything EDGAR structurally cannot see.

    Target raised from 15 to 60 after the first trial run showed that EDGAR legs
    alone (when broken) produced only 18 seeds total — insufficient for 50 records.
    Even with EDGAR fixed, web discovery remains critical for:
      - SFOs below the $100M AUM threshold (no EDGAR filing obligation)
      - SFOs with generic entity names that don't self-identify in filings
      - International offices and those in directories not captured by EDGAR

    Web quality is lower than EDGAR (defunct offices, garbled names from directories).
    Enrichment validates in Phase 2; anything unverifiable falls below the minimum bar.

    The agent is given a 25-iteration budget and explicit diversity requirements to
    prevent it from returning 60 variants of the same well-known FO cluster.
    """
    logger.info("Running web-based discovery via %s agent (target=%d names)...", PROVIDER, target)

    api_key = OPENAI_API_KEY if PROVIDER == "openai" else ANTHROPIC_API_KEY
    client = ModelClient(provider=PROVIDER, api_key=api_key, model=MODEL, max_tokens=MAX_TOKENS)

    system_prompt = f"""You are a research agent building a dataset of family offices.
The current year is 2026. Your job in this phase is ONLY to discover family office names — not to enrich them.

You need to find {target} UNIQUE family office names. This requires DIVERSE searches across:
- Different geographies: US (NY, TX, CA, FL, IL, MA, CO), Europe (UK, Germany, Switzerland), Asia
- Different AUM tiers: ultra-large ($5B+), large ($1B-5B), mid ($100M-1B)
- Different types: Single Family Office (SFO), Multi Family Office (MFO)
- Different industries that created the wealth: tech, finance, real estate, industrials, energy
- Different discovery channels: rankings, directories, news, press releases, LinkedIn

Example search queries to use (use MANY different ones):
- "top family offices 2025 United States AUM"
- "single family office Texas wealth management"
- "multi family office London 2025"
- "family office investments 2025 private equity"
- "largest family offices Switzerland"
- "family office New York CIO hire 2025"
- "family office Germany ultra high net worth"
- "Rockefeller family office" / "Gates family office"
- "family office directory 500 million"
- "family office Chicago real estate"
- "Asian family office Singapore Hong Kong"
- "family office venture capital 2025 2026"

When you have gathered enough names, return a JSON array:
[
  {{"name": "Summit Capital Partners", "source": "Forbes ranking", "source_url": "https://..."}},
  ...
]

Return ONLY the JSON array. Include AT LEAST {target} unique names.
DO NOT include: hedge funds, PE firms (unless they have a FO arm), VC firms, or non-FO wealth managers.
DO include: SFOs, MFOs, family-controlled holding companies with investment functions.
"""

    messages = [{"role": "user", "content": "Begin discovery. Use diverse searches to find at least 60 family offices from different geographies, sizes, and types."}]
    all_names = []
    iterations = 0
    max_iterations = 25

    while iterations < max_iterations:
        iterations += 1
        response = retry_with_backoff(
            client.chat,
            system=system_prompt,
            messages=messages,
            tools=TOOL_DEFINITIONS,
        )

        tool_calls = response["tool_calls"]

        if tool_calls:
            tool_results = []
            for tc in tool_calls:
                logger.debug("Discovery agent tool | %s | input_preview=%s",
                             tc["name"], json.dumps(tc["input"])[:80])
                result_str = dispatch_tool(tc["name"], tc["input"])
                tool_results.append({"tool_call_id": tc["id"], "content": result_str})
            messages.extend(client.make_tool_turn(response, tool_results))
            continue

        # No tool calls — agent returned its final name list
        raw = (response["text"] or "").strip()
        json_match = re.search(r"\[[\s\S]*\]", raw)
        if json_match:
            try:
                all_names = json.loads(json_match.group())
            except json.JSONDecodeError:
                pass
        break

    logger.info("Web discovery returned %d candidates after %d iterations",
                len(all_names), iterations)
    return all_names


def deduplicate(seeds: list[dict]) -> list[dict]:
    """
    Collapse duplicates across all three discovery sources.

    Normalization strips legal suffixes (LLC, LP, Ltd, Inc, Corp) and the words
    "family office" itself before comparing — catches cases like EDGAR returning
    "Biltmore Family Office, LLC" and web returning "Biltmore Family Office."

    Source priority (first encountered wins on collision):
      1. EDGAR 13F  — highest verifiability (filing date, CIK, location known)
      2. EDGAR D    — medium verifiability (entity filed a D, CIK known)
      3. Web search — lowest verifiability (name only, must be confirmed in Phase 2)
    """
    seen = set()
    unique = []
    for s in seeds:
        name = s.get("name", "").strip()
        key = re.sub(r"\b(llc|lp|ltd|inc|corp|family office|fo|family offices)\b", "", name.lower())
        key = re.sub(r"\s+", " ", key).strip()
        if key and key not in seen:
            seen.add(key)
            unique.append(s)
    return unique


def run_discovery() -> list[dict]:
    """
    Merge all three discovery legs and return a deduplicated seed list.

    Target: DISCOVERY_BUFFER seeds (set to ~2x TARGET_RECORDS) to absorb dropout.
    Observed dropout rate from trial run: ~45% (10/18 accepted = 55% acceptance).
    At 55% acceptance, reaching 50 records requires ~91 seeds minimum.
    DISCOVERY_BUFFER is set to 120 to provide margin.

    Source priority on collision: 13F > D > web (13F has most verifiable provenance).
    """
    edgar_13f_seeds = discover_from_sec_13f(pages=6)
    edgar_d_seeds = discover_from_sec_form_d(limit=40)
    web_seeds = discover_from_web(target=60)

    # 13F first (highest verifiability), then D, then web
    combined = edgar_13f_seeds + edgar_d_seeds + [
        s for s in web_seeds
        if isinstance(s, dict) and s.get("name")
    ]
    unique = deduplicate(combined)

    returning = unique[:DISCOVERY_BUFFER]
    logger.info(
        "Discovery complete | 13f=%d | form_d=%d | web=%d | unique=%d | returning=%d",
        len(edgar_13f_seeds), len(edgar_d_seeds), len(web_seeds),
        len(unique), len(returning),
    )
    return returning
