import os
from dotenv import load_dotenv

load_dotenv()

OPENAI_API_KEY   = os.getenv("OPENAI_API_KEY")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

# Qdrant: empty QDRANT_URL means use in-memory mode (fine for dev + single-container deploy).
# Set QDRANT_URL + QDRANT_API_KEY to point at Qdrant Cloud for persistent production storage.
QDRANT_URL      = os.getenv("QDRANT_URL", "")
QDRANT_API_KEY  = os.getenv("QDRANT_API_KEY", "")
COLLECTION_NAME = "family_offices"

# text-embedding-3-small: 1536 dims, $0.02/1M tokens — cheapest OpenAI embedding that
# still outperforms ada-002 on retrieval benchmarks. 50 records costs < $0.001 to embed.
EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIM   = 1536

TOP_K      = 5
XLSX_PATH  = "dataset/family_offices.xlsx"

# RAG provider: controls filter extraction and answer generation.
# Set RAG_PROVIDER=claude or RAG_PROVIDER=openai in .env to switch.
RAG_PROVIDER      = os.getenv("RAG_PROVIDER", "openai")
CLAUDE_MODEL      = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
OPENAI_CHAT_MODEL = os.getenv("OPENAI_CHAT_MODEL", "gpt-5-nano")
RAG_MODEL         = OPENAI_CHAT_MODEL if RAG_PROVIDER == "openai" else CLAUDE_MODEL

# Fields whose text content carries semantic meaning about *what the office does*.
# Everything else (emails, phones, LinkedIn URLs, pipeline metadata) is noise for
# an embedding — it would anchor similarity to surface formatting, not investment intent.
SEMANTIC_FIELDS = [
    "fo_name", "fo_type", "description", "investment_thesis",
    "investment_mandate", "asset_classes", "geographic_focus",
    "recent_news_headline", "recent_investment_1", "recent_investment_2",
    "recent_fund_commitment", "recent_key_hire",
]

# Fields exposed as hard filters in Qdrant. Only fields where exact/range matching
# makes sense are here — AUM is excluded because it's stored as a text range
# ("$500M–$1B"), not a number, so range filtering would silently return wrong results.
FILTERABLE_FIELDS = {
    "fo_type":            "string",
    "hq_country":         "string",
    "hq_city":            "string",
    "hq_state":           "string",
    "validation_status":  "string",
    "confidence_score":   "float",   # min threshold filter
    "data_completion_score": "int",  # min threshold filter
}
