"""
Family Office Dataset Pipeline — Orchestrator

Runs 4 phases sequentially:
  Phase 1: Discovery  — find 70 seed family offices from SEC EDGAR + web search
  Phase 2: Enrichment — Claude tool-use agent enriches each seed to a full record
  Phase 3: Validation — URL + email domain validation, scoring, minimum bar filter
  Phase 4: Export     — XLSX + methodology JSON

Progress is checkpointed to dataset/progress.json after each record so a
partial run can be inspected or resumed without re-running discovery.
"""

import json
import logging
import os
import sys
import time
from logging_config import setup_logging
from pipeline.config import (
    ANTHROPIC_API_KEY, GEMINI_API_KEY, TARGET_RECORDS,
    OUTPUT_DIR, PROGRESS_PATH
)
from pipeline.discovery import run_discovery
from pipeline.enrichment import enrich_family_office
from pipeline.validation import validate_record, passes_minimum_bar
from pipeline.exporter import export_xlsx, export_methodology

logger = logging.getLogger(__name__)


def check_env():
    missing = []
    if not ANTHROPIC_API_KEY:
        missing.append("ANTHROPIC_API_KEY")
    if not GEMINI_API_KEY:
        missing.append("GEMINI_API_KEY")
    if missing:
        # Use stderr directly here — logging isn't set up yet when this runs
        sys.stderr.write(f"[Error] Missing environment variables: {', '.join(missing)}\n")
        sys.stderr.write("Copy .env.example to .env and fill in your API keys.\n")
        sys.exit(1)


def load_progress() -> dict:
    if os.path.exists(PROGRESS_PATH):
        with open(PROGRESS_PATH) as f:
            return json.load(f)
    return {"seeds": [], "records": [], "enriched_names": []}


def save_progress(progress: dict):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(PROGRESS_PATH, "w") as f:
        json.dump(progress, f, indent=2, ensure_ascii=False)


def main():
    setup_logging()
    check_env()
    logger.info("=" * 60)
    logger.info("  Family Office Dataset Pipeline")
    logger.info("=" * 60)

    progress = load_progress()

    # =========================================================
    # PHASE 1: DISCOVERY
    # =========================================================
    if not progress.get("seeds"):
        logger.info("[Phase 1] Discovery — finding seed family offices...")
        seeds = run_discovery()
        progress["seeds"] = seeds
        save_progress(progress)
        logger.info("[Phase 1] Complete. %d seeds saved.", len(seeds))
    else:
        seeds = progress["seeds"]
        logger.info("[Phase 1] Skipped (loaded %d seeds from checkpoint).", len(seeds))

    # =========================================================
    # PHASE 2: ENRICHMENT + PHASE 3: VALIDATION (per record)
    # =========================================================
    enriched_names = set(progress.get("enriched_names", []))
    validated_records = progress.get("records", [])

    remaining = [s for s in seeds if s.get("name", "") not in enriched_names]
    logger.info(
        "[Phase 2+3] Enrichment + Validation | need=%d | have=%d | remaining_seeds=%d",
        TARGET_RECORDS, len(validated_records), len(remaining),
    )

    for seed in remaining:
        if len(validated_records) >= TARGET_RECORDS:
            logger.info("[Phase 2+3] Reached target of %d records. Stopping enrichment.", TARGET_RECORDS)
            break

        name = seed.get("name", "")
        logger.info("[%d/%d] Processing: %s", len(validated_records) + 1, TARGET_RECORDS, name)

        record = enrich_family_office(seed)
        enriched_names.add(name)

        if record is None:
            logger.warning("Enrichment returned None | name=%s | skipping", name)
            progress["enriched_names"] = list(enriched_names)
            save_progress(progress)
            continue

        record = validate_record(record)

        if passes_minimum_bar(record):
            # Domain-level entity dedup — catches the case where two differently-named
            # seeds (e.g. "Cascade Investment LLC" from EDGAR and "BGI LLC" from web)
            # both resolved to the same family office during enrichment.
            # Name dedup in Phase 1 handles formatting variants; this handles aliases.
            existing_domains = {
                r.get("domain", "").lower().strip()
                for r in validated_records
                if r.get("domain")
            }
            record_domain = (record.get("domain") or "").lower().strip()
            if record_domain and record_domain in existing_domains:
                logger.info("Skipped duplicate entity | domain=%s | name=%s", record_domain, name)
            else:
                validated_records.append(record)
                logger.info(
                    "Accepted | name=%s | completion=%s%% | confidence=%s | status=%s",
                    record.get("fo_name", name), record.get("data_completion_score"),
                    record.get("confidence_score"), record.get("validation_status"),
                )
        else:
            logger.info(
                "Rejected (below minimum bar) | name=%s | completion=%s%%",
                name, record.get("data_completion_score", 0),
            )

        progress["records"] = validated_records
        progress["enriched_names"] = list(enriched_names)
        save_progress(progress)

    logger.info("[Phase 2+3] Complete. %d records in dataset.", len(validated_records))

    if len(validated_records) < TARGET_RECORDS:
        logger.warning(
            "Only %d/%d records collected. Consider re-running — more seeds may be available.",
            len(validated_records), TARGET_RECORDS,
        )

    # =========================================================
    # PHASE 4: EXPORT
    # =========================================================
    logger.info("[Phase 4] Exporting...")

    # Remove internal validation annotation fields before export
    clean_records = []
    for r in validated_records:
        clean = {k: v for k, v in r.items() if not k.startswith("_")}
        clean_records.append(clean)

    xlsx_path = export_xlsx(clean_records)

    validation_summary = {
        "total_records": len(clean_records),
        "validated": sum(1 for r in clean_records if r.get("validation_status") == "validated"),
        "partial": sum(1 for r in clean_records if r.get("validation_status") == "partial"),
        "unverified": sum(1 for r in clean_records if r.get("validation_status") == "unverified"),
        "avg_confidence": round(
            sum(r.get("confidence_score", 0) for r in clean_records) / max(len(clean_records), 1), 1
        ),
        "avg_completion_pct": round(
            sum(r.get("data_completion_score", 0) for r in clean_records) / max(len(clean_records), 1), 1
        ),
    }

    methodology_path = export_methodology(
        records=validated_records,  # pass unclean so validation chains have issues/passes
        discovery_log={
            "total_candidates": len(seeds),
            "records_enriched": len(enriched_names),
        },
        validation_summary=validation_summary,
    )

    logger.info("=" * 60)
    logger.info("Pipeline Complete")
    logger.info("  Records collected : %d", len(clean_records))
    logger.info("  XLSX output       : %s", xlsx_path)
    logger.info("  Methodology log   : %s", methodology_path)
    logger.info("  Avg confidence    : %s/10", validation_summary["avg_confidence"])
    logger.info("  Avg completion    : %s%%", validation_summary["avg_completion_pct"])
    logger.info("  Validated         : %d", validation_summary["validated"])
    logger.info("  Partial           : %d", validation_summary["partial"])
    logger.info("  Unverified        : %d", validation_summary["unverified"])


if __name__ == "__main__":
    main()
