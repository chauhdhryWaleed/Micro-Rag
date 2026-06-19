"""
Retrieval: filter extraction + Qdrant vector search

Pattern: filter-then-search (not search-then-filter).
Qdrant applies metadata filters first, then runs vector similarity only against
the matching subset — ensuring the most semantically relevant record within the
filtered set is returned, not the most relevant overall that happens to pass a filter.

Structured filters are extracted from the user's query by the configured LLM
(gpt-5-nano or Claude, controlled by RAG_PROVIDER in .env). This handles
natural language location/type constraints that keyword matching would miss.

If extracted filters produce zero results, the search falls back to semantic-only
and sets fallback_used=True in the response so the UI can inform the user.
"""

import json
import logging
import re
from openai import OpenAI
from qdrant_client import QdrantClient
from qdrant_client.models import Filter, FieldCondition, MatchValue, Range

from rag.config import (
    OPENAI_API_KEY, ANTHROPIC_API_KEY, QDRANT_URL, QDRANT_API_KEY,
    COLLECTION_NAME, EMBEDDING_MODEL, TOP_K, RAG_PROVIDER, RAG_MODEL,
)
from pipeline.model_client import ModelClient

logger = logging.getLogger(__name__)

_qdrant_client: QdrantClient | None = None
_oai_client: OpenAI | None = None


def get_qdrant_client(shared: QdrantClient = None) -> QdrantClient:
    """Return shared client if provided (API lifespan), else create a new one."""
    if shared:
        return shared
    global _qdrant_client
    if _qdrant_client is None:
        if QDRANT_URL:
            _qdrant_client = QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY)
        else:
            _qdrant_client = QdrantClient(":memory:")
    return _qdrant_client


def get_oai() -> OpenAI:
    global _oai_client
    if _oai_client is None:
        _oai_client = OpenAI(api_key=OPENAI_API_KEY)
    return _oai_client


FILTER_EXTRACTION_PROMPT = """You are parsing a search query against a family office database.

Extract any structured filter constraints explicitly present in the query.
Be conservative: only extract a filter if it is unambiguous. Omit it if uncertain.

Filterable fields:
- fo_type: "Single Family Office" or "Multi Family Office"
- hq_country: country name as a string (e.g. "United States", "United Kingdom")
- hq_city: city name (e.g. "New York", "London")
- hq_state: US state or region (e.g. "Texas", "California")
- validation_status: "validated", "partial", or "unverified"
- confidence_score_min: minimum confidence score, number between 1 and 10
- data_completion_score_min: minimum data completeness %, number between 0 and 100

Return JSON only, no prose. Null means do not apply this filter.
Also return the semantic_query: the remaining intent after removing location/type terms.

Query: {query}

JSON:"""


def extract_filters(query: str) -> dict:
    """
    Ask the configured LLM to separate structured intent from semantic intent.
    Provider controlled by RAG_PROVIDER in rag/config.py.
    """
    api_key = OPENAI_API_KEY if RAG_PROVIDER == "openai" else ANTHROPIC_API_KEY
    client = ModelClient(provider=RAG_PROVIDER, api_key=api_key, model=RAG_MODEL, max_tokens=4000)
    try:
        response = client.chat(
            system="You extract structured filters from search queries. Return JSON only.",
            messages=[{"role": "user", "content": FILTER_EXTRACTION_PROMPT.format(query=query)}],
        )
        raw = (response["text"] or "").strip()
        match = re.search(r"\{[\s\S]*\}", raw)
        if match:
            return json.loads(match.group())
    except Exception as e:
        logger.warning("Filter extraction failed | provider=%s | error=%s | falling back to semantic-only",
                       RAG_PROVIDER, e)

    return {"filters": {}, "semantic_query": query}


def build_qdrant_filter(filters: dict) -> Filter | None:
    """
    Convert the filter dict from Claude into a Qdrant Filter object.

    Only non-null filters are applied. Range filters (confidence_score,
    data_completion_score) use gte (greater-than-or-equal). String filters
    use exact MatchValue — partial string matching would require full-text
    search config which we haven't set up [assumed: not needed for 50 records].
    """
    conditions = []

    string_fields = ["fo_type", "hq_country", "hq_city", "hq_state", "validation_status"]
    for field in string_fields:
        val = filters.get(field)
        if val:
            conditions.append(FieldCondition(key=field, match=MatchValue(value=val)))

    if filters.get("confidence_score_min") is not None:
        conditions.append(
            FieldCondition(key="confidence_score", range=Range(gte=float(filters["confidence_score_min"])))
        )

    if filters.get("data_completion_score_min") is not None:
        conditions.append(
            FieldCondition(key="data_completion_score", range=Range(gte=float(filters["data_completion_score_min"])))
        )

    return Filter(must=conditions) if conditions else None


def search(
    query: str,
    top_k: int = TOP_K,
    qdrant: QdrantClient = None,
) -> dict:
    """
    Full retrieval pipeline for one query:
      1. Extract structured filters from query (Claude)
      2. Embed the semantic portion of the query (OpenAI)
      3. Run Qdrant filter-then-search
      4. If 0 results with filters, fall back to semantic-only

    Returns a dict with results, filters applied, and the semantic query used.
    """
    extracted = extract_filters(query)
    filters_dict  = extracted.get("filters", {})
    semantic_query = extracted.get("semantic_query", query)

    # Embed the semantic portion, not the full query (filters already handled)
    oai = get_oai()
    embedding = oai.embeddings.create(
        model=EMBEDDING_MODEL,
        input=semantic_query or query
    ).data[0].embedding

    qdrant_client = get_qdrant_client(qdrant)
    qdrant_filter = build_qdrant_filter(filters_dict)

    hits = qdrant_client.query_points(
        collection_name=COLLECTION_NAME,
        query=embedding,
        query_filter=qdrant_filter,
        limit=top_k,
        with_payload=True,
    ).points

    # Fallback: if filters produced zero results, retry without them
    # and flag this to the caller so the UI can inform the user
    fallback_used = False
    if not hits and qdrant_filter is not None:
        logger.info("Filters produced 0 results | semantic_query=%r | retrying without filters", semantic_query)
        hits = qdrant_client.query_points(
            collection_name=COLLECTION_NAME,
            query=embedding,
            limit=top_k,
            with_payload=True,
        ).points
        fallback_used = True

    results = []
    for hit in hits:
        record = dict(hit.payload)
        record["_similarity_score"] = round(hit.score, 4)
        record.pop("_embed_text", None)  # internal field, not for display
        results.append(record)

    active_filters = {k: v for k, v in filters_dict.items() if v is not None}
    logger.info(
        "Search complete | query=%r | semantic_query=%r | filters=%s | hits=%d | fallback=%s",
        query, semantic_query, active_filters, len(results), fallback_used,
    )
    return {
        "results": results,
        "filters_applied": active_filters,
        "semantic_query": semantic_query,
        "fallback_used": fallback_used,
        "total": len(results),
    }
