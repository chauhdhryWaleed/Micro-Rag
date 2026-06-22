# Architecture

## What This Is

A two-system pipeline that collects and makes queryable a structured dataset of 50 family office intelligence records.

**System 1 — Data Collection Pipeline**
An AI agent pipeline that discovers family offices, enriches each one into a structured record, and independently validates the output before accepting it into the dataset.

**System 2 — RAG Query Interface**
A FastAPI web application that lets analysts query the dataset in natural language and receive AI-generated answers grounded in the data.

---

## The Dataset

**50 records** across global family offices, each containing up to 43 fields across four sections:

| Section | What it contains |
|---|---|
| Entity Attributes | Name, type, description, investment thesis, AUM estimate, founding year, website, asset classes, geographic focus |
| Principal Intelligence | Lead decision-maker name, title, LinkedIn, email |
| Entity Signals | Recent investments, fund commitments, key hires, news headlines |
| Validation & Meta | Validation status, confidence score (1–10), data completion %, source log |

Every record carries a **validation status** and **confidence score** computed independently of the AI that collected the data:

- **`validated`** — website URL and/or email domain confirmed against external sources (HTTP + DNS)
- **`unverified`** — data collected but could not be independently confirmed (often due to limited web presence)
- **Confidence score** — starts from the AI's self-assessment, then adjusted up or down by the validation checks

---

## How the Pipeline Works

```
Discovery → Enrichment → Validation → Export
```

**Discovery**
Seeds family office names from SEC EDGAR filings (13F and Form D) and web search. Deduplicates across all three sources before enrichment begins.

**Enrichment**
An LLM tool-use agent runs a multi-step research loop per seed: web searches, website scraping, principal lookups, news searches. Fills up to 43 structured fields and outputs a JSON record.

**Validation (independent)**
Runs after enrichment and does not trust the AI's output at face value:
- HTTP check on the claimed website URL
- DNS MX record lookup on the email domain
- LinkedIn URL format check
- Required field presence check

Records that don't pass a minimum bar (name + location + 30% field completion) are discarded.

**Export**
Accepted records are written to `dataset/family_offices.xlsx` and a `methodology.json` audit log.

---

## How the RAG Interface Works

```
User question → Filter extraction → Vector search → AI answer
```

1. **Filter extraction** — the AI reads the question and pulls out any structured constraints (country, type, confidence threshold)
2. **Vector search** — the semantic intent of the question is embedded and matched against the 50 records in Qdrant. Filters are applied first, then similarity search runs against the matching subset
3. **Answer generation** — the top 5 matching records are passed to the AI as context. It writes a grounded answer citing records by name, and flags low-confidence sources
4. **Source cards** — the raw records that drove the answer are shown below it so every claim can be verified

If filters produce zero results, the system automatically falls back to semantic-only search and tells the user.

---

## File Structure

```
├── main.py                  # Pipeline orchestrator — runs all 4 phases
├── pipeline/
│   ├── config.py            # API keys, model names, schema definitions
│   ├── discovery.py         # Phase 1: seed discovery from EDGAR + web search
│   ├── enrichment.py        # Phase 2: LLM tool-use agent, fills 43 fields per record
│   ├── tools.py             # Tool implementations: web search, URL scraping, validation
│   ├── validation.py        # Phase 3: independent URL + DNS + field checks
│   ├── exporter.py          # Phase 4: XLSX + methodology JSON export
│   └── utils.py             # Retry logic with exponential backoff
├── rag/
│   ├── config.py            # Embedding model, Qdrant settings, field lists
│   ├── ingest.py            # XLSX → OpenAI embeddings → Qdrant
│   ├── retrieval.py         # Filter extraction + Qdrant vector search
│   ├── generator.py         # Grounded answer generation from retrieved records
│   └── api.py               # FastAPI app — ingests dataset on startup, serves queries
├── static/
│   └── index.html           # Single-page frontend
├── tests/                   # Unit tests for validation, retrieval, ingest, discovery
├── dataset/                 # Created at runtime (not committed)
│   ├── progress.json        # Pipeline checkpoint — resumes from last record if interrupted
│   ├── family_offices.xlsx  # Final dataset (auto-ingested by RAG server on startup)
│   └── methodology.json     # Audit trail with validation chains for top records
├── Dockerfile               # Single-container deploy: uvicorn + in-memory Qdrant
├── requirements.txt
└── .env                     # API keys (not committed — see .env.example)
```

---

## Running Locally

**Prerequisites:** `OPENAI_API_KEY` in `.env`. The dataset must exist at `dataset/family_offices.xlsx` (generated by `python main.py`).

```bash
source .venv/bin/activate
uvicorn rag.api:app --reload
```

Open `http://localhost:8000`. The server ingests the dataset on startup (~5 seconds).

**To regenerate the dataset from scratch:**
```bash
python main.py
```

The pipeline checkpoints after each record to `dataset/progress.json`. If interrupted, re-running resumes from where it stopped.

---

## Deployment

The app is packaged as a single Docker container. The dataset XLSX is bundled into the image at build time and ingested on startup.

```bash
docker build -t fo-rag .
docker run -p 8000:8000 \
  -e OPENAI_API_KEY=your_key \
  fo-rag
```

For Railway or Render: connect the GitHub repo, set `OPENAI_API_KEY` in the environment variables dashboard, and deploy. The Dockerfile is detected automatically.

**Note on persistence:** By default, Qdrant runs in-memory and re-ingests the XLSX on every restart (~5 seconds). For persistent storage, set `QDRANT_URL` and `QDRANT_API_KEY` to point at a Qdrant Cloud cluster.
