# Micro RAG — Family Office Intelligence

A small, production-shaped system that **builds a validated dataset of 50 family offices from scratch** and makes it **queryable in natural language** through a retrieval-augmented generation (RAG) interface.

Two independent systems:

1. **Data collection pipeline** — an AI agent discovers family offices, enriches each into a structured record, and an **independent validation layer** checks the output before it is accepted into the dataset.
2. **RAG query interface** — a FastAPI app that retrieves the most relevant records for a question and generates an answer grounded strictly in the data.

> The guiding principle of this build: **never trust the enrichment AI's output at face value.** Every record is checked by a separate layer that the enrichment agent has no control over. "Validated" means independently confirmed — not self-reported.

> **Trying it yourself:** there's no public hosted demo — clone the repo and add your own `OPENAI_API_KEY` (see [Running locally](#running-locally)). The pre-built dataset is included, so a key is all you need to run the RAG interface.

---

## Why this is interesting

Family offices are deliberately private. There is no clean registry to pull from. The first assumption — that SEC EDGAR's Form ADV would be the primary source — turned out to be **wrong** (ADV filings live in a separate SEC system, IAPD, not EDGAR's search index). The pipeline was rebuilt around Form 13F, Form D, and web search after that discovery.

That kind of decision — assume, test, find the assumption wrong, rebuild — is documented throughout [`METHODOLOGY.md`](METHODOLOGY.md), including what is verified vs inferred, where the system breaks, and what would change the conclusions.

---

## Architecture

```
DATA COLLECTION PIPELINE                    RAG QUERY INTERFACE
─────────────────────────                   ──────────────────────
Discovery   (EDGAR 13F/D + web search)      Question
   ↓                                            ↓
Enrichment  (LLM tool-use agent)            Filter extraction (LLM)
   ↓                                            ↓
Validation  (HTTP + DNS + field checks)     Vector search (filter-then-search)
   ↓                                            ↓
Export      (XLSX + methodology JSON)       Grounded answer + source cards
```

The two layers are intentionally separate: data, retrieval, and presentation are decoupled, and the validation layer runs independently of the agent that produced the data.

---

## Key design decisions

| Decision | Why |
|---|---|
| **Independent validation layer** | The same system can't validate its own output. HTTP HEAD, DNS MX, and field-presence checks run with no knowledge of what the agent claimed. |
| **Filter-then-search retrieval** | Filtering *after* a vector search discards the most relevant results. Qdrant applies metadata filters first, then runs similarity only on the matching subset. |
| **No chunking** | Each record is a dense, self-contained unit. Chunking is right for long documents, wrong for structured records — it would fragment context. |
| **Embed only semantic fields** | Emails, phone numbers, and dates anchor similarity to surface formatting. Only the 12 fields describing *what an office does* are embedded. |
| **Two-stage confidence score** | The agent self-scores, then the validation layer adjusts ±0.5 per independent check passed or failed. The score is never purely self-reported. |
| **Epistemic labels** | Fields are tagged `[verified]`, `[inferred]`, `[assumed]`, or `[not-found]` — uncertainty is named, not hidden. |

---

## Tech stack

- **Enrichment LLM:** gpt-5-nano (OpenAI) — tool-use research agent (Claude supported via `PROVIDER=claude`)
- **Discovery search:** Gemini 2.5 Flash-Lite with Google Search grounding
- **Embeddings:** OpenAI `text-embedding-3-small` (1536-dim)
- **Vector store:** Qdrant (in-memory by default; Qdrant Cloud via env vars)
- **API:** FastAPI + Uvicorn
- **Frontend:** single-page HTML + Tailwind (CDN)
- **Deployment:** Docker, single container — runs on any container host

---

## The dataset

50 records, up to 43 fields each, across four sections:

| Section | Contents |
|---|---|
| Entity Attributes | Name, type, description, investment thesis, mandate, AUM, founding year, website, asset classes, geographic focus |
| Principal Intelligence | Decision-maker name, title, LinkedIn, email |
| Entity Signals | Recent investments, fund commitments, key hires, news |
| Validation & Meta | Validation status, confidence score (1–10), data completion %, source log |

Validation results across the 50 records: **68% validated** (≥1 independent check passed), avg confidence **7.9/10**, avg completion **64%**.

---

## Running locally

**Prerequisites:** Python 3.12+, an `OPENAI_API_KEY`.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # add your API keys
```

**Serve the RAG interface** (dataset is already in `dataset/family_offices.xlsx`):

```bash
uvicorn rag.api:app --reload
# open http://localhost:8000  — ingests the dataset on startup (~5s)
```

**Regenerate the dataset from scratch** (needs `ANTHROPIC_API_KEY` + `GEMINI_API_KEY`):

```bash
python main.py              # checkpoints after each record; resumable if interrupted
```

**Run the tests:**

```bash
pytest
```

---

## Deployment

Packaged as a single Docker container. The dataset is bundled into the image and ingested on startup.

```bash
docker build -t micro-rag .
docker run -p 8000:8000 -e OPENAI_API_KEY=your_key micro-rag
```

On any container host (Render, Fly.io, Railway, etc.): connect the repo, set `OPENAI_API_KEY`, and the Dockerfile is detected automatically. Set `QDRANT_URL` + `QDRANT_API_KEY` for persistent storage instead of in-memory.

---

## What works, what doesn't

**Works:** independent validation catches hallucinated/dead URLs and unresolvable email domains; filter-then-search returns the right records for named-entity and geographic queries; answers are grounded and decline out-of-scope questions; the pipeline checkpoints and resumes.

**Doesn't / limitations:** emails are pattern-inferred and validated only at the domain (MX) level — not the mailbox; LinkedIn URLs are format-checked only; AUM figures come from press estimates and are directional; the dataset is biased toward offices with a web presence (low-profile offices are structurally absent). Full detail and planned improvements are in [`METHODOLOGY.md`](METHODOLOGY.md).

---

## Documentation

- [`METHODOLOGY.md`](METHODOLOGY.md) — discovery (including the wrong assumptions), enrichment, validation logic, failure modes, what would change the conclusions
- [`VALIDATION_CHAINS.md`](VALIDATION_CHAINS.md) — full discovery → validation trail for the 3 highest-confidence records
- [`ARCHITECTURE.md`](ARCHITECTURE.md) — end-user overview of how the system works

---

## Project layout

```
main.py                  # pipeline orchestrator (4 phases)
pipeline/                # discovery, enrichment, validation, export
rag/                     # ingest, retrieval, generation, FastAPI app
static/index.html        # single-page frontend
tests/                   # unit tests
dataset/                 # the generated dataset
Dockerfile               # single-container deploy
```
