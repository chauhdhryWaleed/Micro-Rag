"""
Tool implementations used by the Claude agent.
Each function is called when Claude invokes the corresponding tool.
"""

import logging
import time
import json
import re
import socket
import requests
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from urllib.parse import urlparse
from bs4 import BeautifulSoup
from google import genai
from google.genai import types
from pipeline.config import GEMINI_API_KEY, SEARCH_DELAY
from pipeline.utils import retry_with_backoff

logger = logging.getLogger(__name__)

# Hard wall-clock timeout for any single network request.
# requests timeout=15 catches clean failures but not hung TCP connections
# (e.g. when internet drops mid-connection — OS doesn't detect broken socket
# for up to 2 hours by default). This thread-based timeout guarantees a
# maximum wall time regardless of OS socket state.
_HARD_TIMEOUT_SECS = 30


def _run_with_hard_timeout(fn, *args, **kwargs):
    """Run fn(*args, **kwargs) in a thread; raise TimeoutError if it exceeds _HARD_TIMEOUT_SECS."""
    with ThreadPoolExecutor(max_workers=1) as ex:
        future = ex.submit(fn, *args, **kwargs)
        try:
            return future.result(timeout=_HARD_TIMEOUT_SECS)
        except FuturesTimeoutError:
            raise requests.exceptions.Timeout(
                f"Hard {_HARD_TIMEOUT_SECS}s timeout — internet may be down"
            )

_gemini_client = None


def _get_gemini() -> genai.Client:
    global _gemini_client
    if _gemini_client is None:
        _gemini_client = genai.Client(api_key=GEMINI_API_KEY)
    return _gemini_client


def search_web(query: str, search_depth: str = "basic", max_results: int = 5) -> dict:
    """Search the web via Gemini 2.5 Flash-Lite with Google Search grounding."""
    time.sleep(SEARCH_DELAY)
    try:
        client = _get_gemini()
        response = retry_with_backoff(
            client.models.generate_content,
            model="gemini-2.5-flash-lite",
            contents=f"Search for and summarize information about: {query}",
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],
            ),
        )
        results = []
        if response.candidates:
            candidate = response.candidates[0]
            summary = (response.text or "")[:600]
            if hasattr(candidate, "grounding_metadata") and candidate.grounding_metadata:
                chunks = candidate.grounding_metadata.grounding_chunks or []
                for chunk in chunks[:max_results]:
                    if hasattr(chunk, "web") and chunk.web:
                        results.append({
                            "title": chunk.web.title or "",
                            "url": chunk.web.uri or "",
                            "snippet": summary,
                        })
            # Grounding chunks absent but response text exists — surface it as one result
            # so the enrichment agent still gets signal rather than an empty list
            if not results and summary:
                logger.debug("No grounding chunks for query=%r — returning summary as single result", query)
                results.append({"title": query, "url": "", "snippet": summary})

        logger.debug("search_web | query=%r | results=%d", query, len(results))
        return {"query": query, "results": results, "count": len(results)}
    except Exception as e:
        logger.error("search_web failed | query=%r | error=%s", query, e)
        return {"query": query, "results": [], "error": str(e)}


def search_sec_edgar(query: str, limit: int = 100, form_type: str = "13F-HR",
                      from_offset: int = 0) -> dict:
    """
    Search SEC EDGAR EFTS full-text index for filings mentioning the query term.

    Form ADV is filed with IAPD (not EDGAR) and is NOT searchable here — confirmed
    via direct API testing that ADV returns 0 hits. We use 13F-HR and D instead.

    13F-HR response format uses `display_names` (list) and `ciks` (list), not the
    `entity_name`/`entity_id` fields that appear in other EDGAR EFTS responses.
    We parse both formats and normalize to a consistent output shape.

    from_offset: supports pagination (EDGAR returns 100 hits per page max).
    """
    time.sleep(SEARCH_DELAY)
    try:
        url = "https://efts.sec.gov/LATEST/search-index"
        # Use list-of-tuples to avoid dict key collisions and ensure correct encoding
        params = [("q", f'"{query}"'), ("forms", form_type), ("from", from_offset)]
        headers = {"User-Agent": "FamilyOfficeDataPipeline contact@example.com"}
        resp = requests.get(url, params=params, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        hits = data.get("hits", {}).get("hits", [])
        seen_ciks = set()
        results = []
        for hit in hits:
            if len(results) >= limit:
                break
            src = hit.get("_source", {})

            # 13F/D use display_names + ciks (lists); other forms use entity_name + entity_id
            display_names = src.get("display_names", [])
            cik_list = src.get("ciks", [])
            if display_names:
                import re as _re
                raw_name = display_names[0]
                entity_name = _re.sub(r"\s*\(CIK.*?\)", "", raw_name).strip()
                cik = _re.sub(r"^0+", "", cik_list[0]) if cik_list else ""
            else:
                entity_name = src.get("entity_name", "")
                cik = src.get("entity_id", "")

            # Deduplicate within this call — same entity files quarterly
            if not entity_name or cik in seen_ciks:
                continue
            if cik:
                seen_ciks.add(cik)

            location = ""
            locs = src.get("biz_locations", [])
            if locs:
                location = locs[0]

            results.append({
                "entity_name": entity_name,
                "file_date": src.get("file_date", ""),
                "cik": cik,
                "location": location,
                "filing_url": (
                    f"https://www.sec.gov/cgi-bin/browse-edgar"
                    f"?action=getcompany&CIK={cik}&type={form_type}"
                    f"&dateb=&owner=include&count=5"
                ),
            })

        logger.debug("search_sec_edgar | form=%s | query=%r | from=%d | returned=%d",
                     form_type, query, from_offset, len(results))
        return {"query": query, "results": results, "count": len(results)}
    except Exception as e:
        logger.error("search_sec_edgar failed | form=%s | query=%r | error=%s", form_type, query, e)
        return {"query": query, "results": [], "error": str(e)}


def scrape_url(url: str, max_chars: int = 3000) -> dict:
    """Scrape plain text from a URL. Returns truncated text content."""
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        resp = _run_with_hard_timeout(
            requests.get, url, headers=headers, timeout=15, allow_redirects=True
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
        for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
            tag.decompose()
        text = " ".join(soup.get_text(separator=" ").split())
        logger.debug("scrape_url | url=%s | chars=%d", url, len(text))
        return {
            "url": url,
            "final_url": resp.url,
            "status_code": resp.status_code,
            "text": text[:max_chars],
            "char_count": len(text),
        }
    except Exception as e:
        logger.warning("scrape_url failed | url=%s | error=%s", url, e)
        return {"url": url, "text": "", "error": str(e)}


def validate_url(url: str) -> dict:
    """Check if a URL is reachable and returns HTTP 200."""
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = _run_with_hard_timeout(
            requests.head, url, headers=headers, timeout=10, allow_redirects=True
        )
        valid = resp.status_code < 400
        logger.debug("validate_url | url=%s | status=%d | valid=%s", url, resp.status_code, valid)
        return {"url": url, "final_url": resp.url, "status_code": resp.status_code, "valid": valid}
    except Exception as e:
        logger.debug("validate_url | url=%s | error=%s", url, e)
        return {"url": url, "valid": False, "error": str(e)}


def validate_email_domain(email: str) -> dict:
    """
    Validate an email by:
    1. Checking format with regex
    2. Checking the domain has MX records (DNS lookup)
    Does NOT send SMTP pings — avoids spam flag risk.
    """
    email = email.strip().lower()
    fmt_ok = bool(re.match(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$", email))
    if not fmt_ok:
        return {"email": email, "format_valid": False, "mx_valid": False, "status": "invalid-format"}

    domain = email.split("@")[1]
    try:
        import dns.resolver
        mx_records = dns.resolver.resolve(domain, "MX")
        has_mx = len(mx_records) > 0
    except Exception:
        try:
            socket.gethostbyname(domain)
            has_mx = True
        except Exception:
            has_mx = False

    logger.debug("validate_email_domain | email=%s | mx_valid=%s", email, has_mx)
    return {
        "email": email,
        "domain": domain,
        "format_valid": True,
        "mx_valid": has_mx,
        "status": "domain-valid" if has_mx else "domain-not-found",
    }


TOOL_DEFINITIONS = [
    {
        "name": "search_web",
        "description": (
            "Search the web for information. Use targeted queries. "
            "Returns title, URL, and snippet for each result. "
            "search_depth='advanced' costs more but gives richer results — use for enrichment. "
            "search_depth='basic' is fine for discovery."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search query string."},
                "search_depth": {
                    "type": "string", "enum": ["basic", "advanced"],
                    "description": "Use 'advanced' for richer content during enrichment.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Number of results to return (default 5, max 10).",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "search_sec_edgar",
        "description": (
            "Search SEC EDGAR for Form ADV filings matching a query. "
            "Family offices managing >$100M must register as investment advisers. "
            "Returns entity names, CIKs, and filing dates."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search term, e.g. 'family office'."},
                "limit": {"type": "integer", "description": "Max results to return (default 20)."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "scrape_url",
        "description": (
            "Fetch and extract plain text from a URL. "
            "Use this to read a family office's website, a LinkedIn company page, "
            "or a news article for enrichment."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string", "description": "Full URL to scrape."}},
            "required": ["url"],
        },
    },
    {
        "name": "validate_url",
        "description": "Check if a URL is reachable (HTTP 200). Use to validate website URLs.",
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    },
    {
        "name": "validate_email_domain",
        "description": (
            "Validate an email address format and check that the domain has MX records. "
            "Does not send any email — purely DNS-based validation."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"email": {"type": "string"}},
            "required": ["email"],
        },
    },
]


def dispatch_tool(name: str, inputs: dict) -> str:
    """Route a tool call from Claude/OpenAI to the correct function.

    Reasoning models (gpt-5-nano, o-series) occasionally emit tool calls with
    empty or partially-formed argument objects. Guard every required key and
    return an error JSON rather than raising — the agent loop can recover.

    gpt-5-nano sometimes wraps args in a nested 'parameters' key, mirroring
    the function-definition schema structure. Unwrap one level if detected.
    """
    # Unwrap nested 'parameters' key — gpt-5-nano sometimes echoes the schema
    # structure in its output: {"parameters": {"query": "..."}} instead of {"query": "..."}
    # This happens because the OpenAI function schema has a "parameters" field and the
    # reasoning model occasionally mirrors that structure in its tool-call arguments.
    if isinstance(inputs.get("parameters"), dict):
        nested = inputs["parameters"]
        other_top_level = {k: v for k, v in inputs.items() if k != "parameters"}
        # Nested keys win over top-level (they are the intended args)
        inputs = {**other_top_level, **nested}
        logger.debug("dispatch_tool: unwrapped 'parameters' key for tool=%s | result=%s", name, inputs)

    logger.debug("dispatch_tool | tool=%s | inputs=%s", name, json.dumps(inputs)[:120])

    if name == "search_web":
        query = inputs.get("query", "").strip()
        if not query:
            logger.warning("search_web called with missing/empty query | inputs=%s", inputs)
            result = {"error": "search_web requires a non-empty 'query' argument"}
        else:
            result = search_web(
                query=query,
                search_depth=inputs.get("search_depth", "basic"),
                max_results=inputs.get("max_results", 5),
            )
    elif name == "search_sec_edgar":
        query = inputs.get("query", "").strip()
        if not query:
            logger.warning("search_sec_edgar called with missing/empty query | inputs=%s", inputs)
            result = {"error": "search_sec_edgar requires a non-empty 'query' argument"}
        else:
            result = search_sec_edgar(query=query, limit=inputs.get("limit", 20))
    elif name == "scrape_url":
        url = inputs.get("url", "").strip()
        if not url:
            logger.warning("scrape_url called with missing url | inputs=%s", inputs)
            result = {"error": "scrape_url requires a non-empty 'url' argument"}
        else:
            result = scrape_url(url)
    elif name == "validate_url":
        url = inputs.get("url", "").strip()
        if not url:
            logger.warning("validate_url called with missing url | inputs=%s", inputs)
            result = {"error": "validate_url requires a non-empty 'url' argument"}
        else:
            result = validate_url(url)
    elif name == "validate_email_domain":
        email = inputs.get("email", "").strip()
        if not email:
            logger.warning("validate_email_domain called with missing email | inputs=%s", inputs)
            result = {"error": "validate_email_domain requires a non-empty 'email' argument"}
        else:
            result = validate_email_domain(email)
    else:
        logger.warning("dispatch_tool called with unknown tool: %s", name)
        result = {"error": f"Unknown tool: {name}"}

    return json.dumps(result, ensure_ascii=False)
