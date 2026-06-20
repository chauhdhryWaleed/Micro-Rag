"""
FastAPI application — the presentation and retrieval boundary.

Separation of concerns:
  - retrieval.py owns search logic (filter extraction + Qdrant vector search)
  - generator.py owns answer generation (grounded AI response)
  - api.py owns routing, request/response shape, and startup state

The Qdrant client is created once at startup and shared across all requests
via dependency injection. A new QdrantClient(":memory:") per request would
return empty results every time — the shared client is the only correct pattern
for in-memory mode.

Startup sequence:
  1. Create Qdrant client (in-memory if QDRANT_URL unset, cloud otherwise)
  2. Check dataset/family_offices.xlsx exists; log warning and skip if not
  3. Ingest: load XLSX → embed → upsert to Qdrant
  4. Verify indexed count matches loaded record count
  5. Serve requests

A failed ingest does not crash the server — /api/health surfaces the error
and /api/ask returns HTTP 503 until the dataset is available.
"""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from qdrant_client import QdrantClient

from logging_config import setup_logging
from rag.config import QDRANT_URL, QDRANT_API_KEY, COLLECTION_NAME, XLSX_PATH, TOP_K
from rag.ingest import ingest, get_qdrant_client as make_qdrant_client
from rag.retrieval import search as run_search
from rag.generator import generate_answer

setup_logging()
logger = logging.getLogger(__name__)


# --- Shared state ---

class AppState:
    qdrant: QdrantClient = None
    indexed_count: int = 0
    ingest_error: str = ""

state = AppState()


# --- Lifespan: ingest on startup ---

@asynccontextmanager
async def lifespan(app: FastAPI):
    state.qdrant = make_qdrant_client()

    xlsx = Path(XLSX_PATH)
    if not xlsx.exists():
        state.ingest_error = (
            f"Dataset not found at {XLSX_PATH}. "
            "Run the data collection pipeline (main.py) first."
        )
        logger.warning("Startup: dataset not found | path=%s", XLSX_PATH)
    else:
        try:
            logger.info("Startup: ingesting dataset into Qdrant | path=%s", xlsx)
            state.indexed_count = ingest(xlsx_path=str(xlsx), client=state.qdrant)
            logger.info("Startup: ingest complete | indexed=%d", state.indexed_count)
        except Exception as e:
            state.ingest_error = f"Ingest failed: {e}"
            logger.error("Startup: ingest failed | error=%s", e)

    yield
    # Shutdown: nothing to clean up (in-memory Qdrant drops with the process)


# --- App ---

app = FastAPI(
    title="Family Office Intelligence — RAG API",
    version="1.0",
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory="static"), name="static")


# --- Request / Response models ---

class SearchRequest(BaseModel):
    query: str
    top_k: int = TOP_K

class AskRequest(BaseModel):
    question: str
    top_k: int = TOP_K


# --- Routes ---

@app.get("/")
async def index():
    return FileResponse("static/index.html")


@app.get("/api/health")
async def health():
    """
    System status. Returns indexed record count and any ingest errors.
    Use this to verify the system is ready before running queries.
    """
    return {
        "status": "degraded" if state.ingest_error else "ok",
        "indexed_records": state.indexed_count,
        "collection": COLLECTION_NAME,
        "storage": "qdrant-cloud" if QDRANT_URL else "in-memory",
        "error": state.ingest_error or None,
    }


@app.post("/api/search")
async def search(req: SearchRequest):
    """
    Structured + semantic search over the FO dataset.
    Returns raw records with similarity scores and applied filters.
    Intended for data consumers who want structured output, not prose.
    """
    if state.ingest_error and state.indexed_count == 0:
        raise HTTPException(status_code=503, detail=state.ingest_error)

    if not req.query.strip():
        raise HTTPException(status_code=400, detail="Query cannot be empty.")

    try:
        result = run_search(query=req.query, top_k=req.top_k, qdrant=state.qdrant)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return JSONResponse(content={
        "query": req.query,
        **result,
    })


@app.post("/api/ask")
async def ask(req: AskRequest):
    """
    Natural language question → retrieval → Claude grounded answer.
    Returns prose answer + source records + citation metadata.
    Every claim in the answer is traceable to a specific retrieved record.
    """
    if state.ingest_error and state.indexed_count == 0:
        raise HTTPException(status_code=503, detail=state.ingest_error)

    if not req.question.strip():
        raise HTTPException(status_code=400, detail="Question cannot be empty.")

    try:
        retrieval = run_search(query=req.question, top_k=req.top_k, qdrant=state.qdrant)
        generation = generate_answer(
            question=req.question,
            records=retrieval["results"],
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return JSONResponse(content={
        "question": req.question,
        "answer": generation["answer"],
        "sources": generation["sources"],
        "records": retrieval["results"],
        "filters_applied": retrieval["filters_applied"],
        "semantic_query": retrieval["semantic_query"],
        "fallback_used": retrieval["fallback_used"],
        "record_count": generation["record_count"],
    })
