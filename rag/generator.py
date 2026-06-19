"""
Generator: retrieved records → grounded AI answer

Provider is controlled by RAG_PROVIDER in .env (openai or claude).
Currently running gpt-5-nano via OpenAI.

Grounding is enforced at the prompt level:
- Records are passed as numbered context blocks so the AI cites by name
- The AI is instructed to answer only from the provided records, not training data
- validation_status and confidence_score are included per record so the AI flags
  low-confidence sources rather than presenting them as certain
- Both the prose answer AND the raw records are returned — the answer is a
  convenience layer; the source cards are the authoritative output
"""

import logging
from rag.config import OPENAI_API_KEY, ANTHROPIC_API_KEY, RAG_PROVIDER, RAG_MODEL
from pipeline.model_client import ModelClient

logger = logging.getLogger(__name__)


ANSWER_SYSTEM_PROMPT = """You are an analyst answering questions about a family office intelligence dataset.

Rules you must follow:
1. Answer ONLY from the provided records. Do not use external knowledge.
2. If the answer is not in the records, say so clearly: "The dataset does not contain that information."
3. When you cite a fact, reference the record by name (e.g., "According to [FO Name]...").
4. If a record has validation_status "unverified" or confidence_score below 5, flag it as low-confidence.
5. Be concise. Analysts read fast.

The current year is 2026."""


def format_records_for_context(records: list[dict]) -> str:
    """Build a numbered context block from retrieved records for the Claude prompt."""
    blocks = []
    for i, r in enumerate(records, 1):
        lines = [f"[Record {i}] {r.get('fo_name', 'Unknown')}"]
        lines.append(f"  Type: {r.get('fo_type', 'N/A')}")
        lines.append(f"  Location: {r.get('hq_city', '')}, {r.get('hq_country', '')}")
        lines.append(f"  Validation: {r.get('validation_status', 'N/A')} | Confidence: {r.get('confidence_score', 'N/A')}/10")

        for field in [
            "description", "investment_thesis", "investment_mandate",
            "aum_estimate", "asset_classes", "geographic_focus",
            "principal_full_name", "principal_title",
            "recent_investment_1", "recent_investment_2",
            "recent_fund_commitment", "recent_key_hire",
            "recent_news_headline", "recent_news_date",
        ]:
            val = r.get(field)
            if val and str(val).strip() not in ("", "null", "None", "n/a"):
                label = field.replace("_", " ").title()
                lines.append(f"  {label}: {val}")

        blocks.append("\n".join(lines))

    return "\n\n".join(blocks)


def generate_answer(question: str, records: list[dict]) -> dict:
    """
    Generate a grounded answer from retrieved records.

    Returns:
        {
          "answer": str,           # Claude's prose answer
          "sources": list[dict],   # which records were cited (name + confidence + status)
          "record_count": int,     # how many records were available as context
        }
    """
    if not records:
        return {
            "answer": "No matching records were found in the dataset for this query.",
            "sources": [],
            "record_count": 0,
        }

    context = format_records_for_context(records)
    user_message = f"""Dataset records retrieved for this query:

{context}

---

Question: {question}

Answer based only on the records above. Cite record names where relevant."""

    api_key = OPENAI_API_KEY if RAG_PROVIDER == "openai" else ANTHROPIC_API_KEY
    # gpt-5-nano is a reasoning model: max_completion_tokens covers reasoning + visible output.
    # At 1024, the budget is consumed by reasoning and visible text is empty. 8000 fixes this.
    client = ModelClient(provider=RAG_PROVIDER, api_key=api_key, model=RAG_MODEL, max_tokens=8000)
    try:
        response = client.chat(
            system=ANSWER_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )
        answer = (response["text"] or "").strip()
        logger.info("Answer generated | provider=%s | question=%r | answer_chars=%d | sources=%d",
                    RAG_PROVIDER, question[:60], len(answer), len(records))
    except Exception as e:
        logger.error("Answer generation failed | provider=%s | question=%r | error=%s",
                     RAG_PROVIDER, question[:60], e)
        answer = f"Answer generation failed: {e}"

    # Surface which records were available as sources, with their trust signals.
    # The UI shows these as citation cards so users can verify claims independently.
    sources = [
        {
            "fo_name": r.get("fo_name", "Unknown"),
            "validation_status": r.get("validation_status", "unknown"),
            "confidence_score": r.get("confidence_score"),
            "similarity_score": r.get("_similarity_score"),
            "website_url": r.get("website_url"),
        }
        for r in records
    ]

    return {
        "answer": answer,
        "sources": sources,
        "record_count": len(records),
    }
