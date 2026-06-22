# Methodology Summary

This document shows the reasoning behind the build — not just what I did, but what I assumed, what proved wrong, what I verified versus inferred, and what would change my conclusions. The pipeline only makes sense if the thinking behind it is visible.

---

## 1. Discovery — How I Found Family Office Records

### Initial assumption and why it was wrong

I assumed SEC EDGAR would be the primary high-signal source. Family offices managing over $100M register with the SEC, so the plan was: query Form ADV filings → extract registered family offices → use as seeds.

**This failed completely.** Form ADV returns zero results from the EDGAR full-text search index every time, regardless of query. After testing directly against the API, the root cause became clear: ADV is submitted to the SEC's **IAPD system** (Investment Adviser Public Disclosure), which is a separate registry from EDGAR. The EDGAR EFTS full-text search index does not include ADV filings. This is not an API bug — it is a documented structural fact about SEC infrastructure.

This assumption cost me time and had to be discovered through testing, not by reading documentation first. I rebuilt the discovery phase around two EDGAR forms that do work:

**Form 13F** — triggered by asset size (>$100M in public equities), not registration status. A family office cannot opt out. Paginating across 600 hits extracted ~44 unique entities after CIK-level deduplication. `[verified: confirmed via direct API testing]`

**Form D** — catches offices below the 13F threshold that run structured investment vehicles (SPVs, fund-of-ones). Higher noise than 13F; estimated 30-50% of results are actual family offices, the rest are VC/PE funds. `[inferred: estimated from enrichment acceptance rate, not confirmed independently]`

### Second assumption — EDGAR would provide enough volume

The trial run showed a ~2-3% acceptance rate on EDGAR seeds. Most 13F filers are pension funds, endowments, and bank trusts — not family offices. Form D noise is higher. At that acceptance rate, 121 EDGAR seeds would yield roughly 3-4 accepted records. This was not viable.

**Updated approach:** Web search became the primary volume source — an LLM agent (Gemini 2.5 Flash-Lite with Google Search grounding) ran iterative searches by geography, AUM tier, and wealth source. I also manually seeded 20 well-known family offices (Stonehage Fleming, Bessemer Trust, Thiel Capital, etc.) after recognising that EDGAR structurally cannot surface sub-$100M SFOs or offices with generic entity names.

**What this means for the dataset:** The 50 accepted records are biased toward family offices with an active web presence. Offices that deliberately maintain low digital footprints are structurally absent. This is not a recoverable gap in this architecture — it is a known selection bias documented here rather than ignored.

**Total seeds processed:** 136 (including retries)
**Records accepted:** 50
**Implied acceptance rate:** ~37% (varies significantly by seed source; web seeds ~65-70%, EDGAR seeds ~5-10%)

---

## 2. Enrichment — How Each Record Was Built

### What the agent does

For each seed, an LLM tool-use agent (gpt-5-nano via OpenAI) runs a multi-step research loop — up to 6 iterations — using web search and URL scraping, then outputs a structured JSON record with up to 43 fields.

### What I assumed about the model's output behaviour

I assumed that after completing research, the model would naturally switch to producing JSON output. It did not. gpt-5-nano is a reasoning model: it spends an internal reasoning token budget before producing visible output. When accumulated context from 6+ tool call iterations was large, the model exhausted its reasoning budget and returned empty visible content.

**Fix:** After the research loop, a forced output phase was added — tools are disabled, the model is given a 20,000-token budget and explicitly instructed to output JSON only. Two attempts are made; the second uses a stronger instruction if the first returns tool-call-formatted text instead of JSON.

**What this means for data quality:** Records that required more than 6 research iterations were cut short. Offices with limited web presence may be under-enriched because the agent ran out of iterations before finding enough data, not because the data doesn't exist somewhere.

### Epistemic labeling

The agent was instructed to label fields it could not directly verify:
- `[verified]` — confirmed against a primary source (website, SEC filing)
- `[inferred]` — derived from evidence but not directly stated
- `[assumed]` — working assumption, not tested
- `[not-found]` — searched but could not locate

These labels are stored in `field_confidence_notes` on each record. **Important caveat:** these labels reflect the agent's self-assessment. Whether the agent applied them correctly is not independently verified. An agent that is confident but wrong will label a hallucinated URL as `[verified]`. This is why the validation layer exists and runs separately.

---

## 3. Validation — How I Checked What the AI Claimed

### The core principle: the validation layer cannot trust the enrichment agent

The same system cannot validate its own output. The enrichment agent will hallucinate URLs, infer email addresses that don't exist, and produce plausible-looking data that is wrong. Labeling this as a limitation understates it — it is the expected behaviour of LLMs on tasks with partial information.

The validation layer uses only independently verifiable signals:

| Check | Method | What it actually confirms |
|---|---|---|
| Website URL | HTTP HEAD request | The URL returns a response — not that it belongs to this firm |
| Email domain | DNS MX record lookup | The domain can receive email — not that this person's mailbox exists |
| LinkedIn URL | Regex format check | The URL is formatted correctly — not that it points to the right page |
| Required fields | Presence check | The field has a value — not that the value is correct |

### What "validated" actually means — and what it doesn't

A record with `validation_status: validated` means at least one independently verifiable check passed. It **does not** mean the data is accurate. A website returning 200 OK confirms the domain is real; it does not confirm that the AUM figure, investment thesis, or principal name are correct.

This distinction matters. The validation layer catches hallucinated or dead URLs and unresolvable email domains. It does not catch:
- Correct-looking URLs that belong to a different firm
- Accurate-format emails where the mailbox doesn't exist (SMTP not checked)
- Outdated information (principal left the firm, AUM changed)
- Correctly formatted LinkedIn URLs pointing to wrong pages

**What would increase confidence in the "validated" label:**
- SMTP RCPT TO verification to confirm the specific mailbox exists
- Browser-based LinkedIn verification to confirm page identity
- Cross-referencing AUM figures against SEC regulatory filings

### Confidence score — two-stage process with known limitations

The score (1–10) is not a single self-report. It is a two-stage process:

**Stage 1:** Agent self-assigns a score based on evidence quality. This score reflects what the agent believes — which matters — but cannot be trusted on its own. The agent has no way to verify that URLs load or that email domains are real.

**Stage 2:** Validation layer adjusts ±0.5 per independent check passed or failed.

Formula: `final = clamp(agent_score − (0.5 × issues) + (0.5 × verifications), 1, 10)`

**Known weakness:** All checks carry equal weight (±0.5). A dead website and a missing phone number both deduct 0.5, but they are not equally damaging. A dead website is a critical credibility failure; a missing phone is a minor gap. This flat weighting understates the severity of missing web presence. Field-level weighting would be more accurate but was not implemented in v1 to keep the scoring logic transparent and auditable.

### Validation summary across 50 records

| Metric | Value | What it means |
|---|---|---|
| Validated (≥1 check passed) | 34 (68%) | Website or email domain independently confirmed |
| Unverified | 16 (32%) | Data collected but nothing independently confirmed |
| Average confidence score | 7.9 / 10 | Average of two-stage scores; not an accuracy estimate |
| Average data completion | 64% | Average fields populated across 21 scored fields |

---

## 4. Stack Choices and Reasoning

| Component | Choice | Why | What could make this wrong |
|---|---|---|---|
| Enrichment LLM | gpt-5-nano (OpenAI) | Tool-use capable reasoning model; Anthropic monthly limit hit mid-run, forced switch from Claude | Reasoning model token behavior adds complexity (forced output phase required); a standard instruction-following model might be more predictable |
| Discovery search | Gemini 2.5 Flash-Lite | Native Google Search grounding returns real URLs with source metadata | Google search results are not neutral — SEO-optimised and PR-heavy FOs are over-represented |
| Vector store | Qdrant in-memory | Native filter-then-search; no post-hoc filtering | In-memory means data is lost on restart; re-ingest on startup adds ~5 seconds cold start |
| Embedding model | text-embedding-3-small | Outperforms ada-002 on retrieval benchmarks at lower cost | 50 records is too few to meaningfully distinguish embedding model quality |
| API framework | FastAPI | Async, lightweight, auto-generates OpenAPI docs | — |
| Deployment | Railway + Docker | Detects Dockerfile automatically; $5 free credit covers this workload | Free tier containers are not guaranteed uptime |

---

## 5. Chunking Strategy

This pipeline does not chunk. Each family office record is embedded as a single unit — a concatenation of the 12 fields that describe what the office *does*:

```
fo_name | fo_type | description | investment_thesis | investment_mandate |
asset_classes | geographic_focus | recent_news_headline | recent_investment_1 |
recent_investment_2 | recent_fund_commitment | recent_key_hire
```

**Why not chunk:** Each record is self-contained and semantically dense. Chunking a 200-word record into 50-word chunks would not improve retrieval — it would create fragments that lose context and require re-assembly. Chunking is the right strategy for long documents (PDFs, articles); it is the wrong strategy for structured records.

**What embedding all 43 fields would do wrong:** The remaining 31 fields include emails, phone numbers, pipeline metadata, and validation scores. Embedding these would anchor similarity to surface formatting (email domains, date strings) rather than investment intent. A query about "tech-focused family offices" would partially match on records whose emails happen to contain "tech" in the domain. The 12-field semantic blob avoids this.

---

## 6. Retrieval Approach

**Pattern: filter-then-search, not search-then-filter.**

If you embed the query, retrieve top-K by similarity, and then apply filters — you discard the most relevant results and backfill with less relevant ones that happen to pass the filter. Qdrant applies metadata filters first, then runs vector similarity only against the matching subset.

**Filter extraction via LLM, not keywords:** Keyword matching breaks on synonyms and abbreviations. The LLM extracts structured filters from the natural language query before the vector search runs. "East Coast offices" becomes `hq_country: United States`; "high-confidence records" becomes `confidence_score_min: 8`.

**Known weakness of string filters:** Exact match only. `hq_city: "New York"` does not match records stored as `"New York City"`. The filter extraction LLM is instructed to be conservative — omit the filter if uncertain rather than guess and return zero results. The fallback-to-semantic-only covers the zero-result case when filters are too specific.

---

## 7. What Works

- **Validation is genuinely independent.** The HTTP and DNS checks run without knowledge of what the enrichment agent claimed. This catches hallucinated URLs and invented email domains before they reach the output.
- **Filter-then-search retrieval** returns the right records for named entity queries and geographic/type filters.
- **Grounding is enforced.** The generator answers only from retrieved records, flags low-confidence sources, and declines out-of-scope questions.
- **Checkpoint recovery.** A 136-seed run can be interrupted and resumed from the last accepted record.
- **Forced output phase** reliably recovers from gpt-5-nano's tendency to exhaust iterations on tool calls without producing JSON.

---

## 8. What Does Not Work — Honest Assessment

- **Email addresses cannot be confirmed.** All emails are pattern-inferred (`first.last@domain.com`) and validated only at the domain MX level. Whether the specific mailbox exists is unknown. Every email in this dataset should be treated as a starting hypothesis, not a confirmed contact.

- **LinkedIn URLs are not verified.** Format check only. The URL may point to the wrong page or a deleted profile. `[assumed: format correctness implies page existence — this assumption is untested]`

- **AUM figures are not audited.** Sourced from web (self-reported, press estimates). AUM for Bessemer Trust, for example, is reported as "$140B–$200B" — a wide range from inconsistent sources. These figures are directional, not precise.

- **"Validated" is weaker than it sounds.** See section 3. A 200 OK response confirms a domain is real, not that the data is accurate.

- **Selection bias toward visible offices.** The pipeline cannot discover what the web does not surface. This is not fixable within this architecture.

- **Confidence score weighting is flat.** Equal penalty for a dead website and a missing phone number understates severity differences.

---

## 9. What Would Change My Conclusions

- If SMTP verification showed that >50% of pattern-inferred emails are wrong → the `principal_email` field should be removed from the schema, not just labeled as inferred.
- If a manual spot-check of 10 records showed significant inaccuracies in AUM or principal information → confidence scores should be deflated across the board, and the "validated" label should carry a stronger disclaimer.
- If a larger corpus (500+ records) were embedded → similarity score distributions would spread out and retrieval rankings would become more meaningful. At 50 records, the difference between a 0.82 and 0.79 similarity score is not significant.
- If the Qdrant filter extraction LLM were replaced with keyword matching → filter accuracy would decrease for natural language queries but be more predictable and auditable.

---

## 10. What I Would Improve

1. **Normalize AUM to numeric fields** (`aum_min_bn`, `aum_max_bn`) to enable hard range filters.
2. **SMTP mailbox verification** to confirm principal emails at the mailbox level, not just domain level.
3. **LinkedIn live verification** via headless browser to confirm page identity.
4. **Field-level confidence weighting** — weight validation checks by field importance rather than flat ±0.5 per check.
5. **Expand corpus to 500+ records** to make similarity rankings meaningful.
6. **Persistent Qdrant** (Qdrant Cloud) to eliminate re-ingest on every restart.
7. **AUM from SEC filings** — cross-reference AUM against Form ADV Part 1 where available, replacing press estimates with regulatory data.
