"""
Centralised logging setup for both the data collection pipeline and the RAG API.

Called once at process startup (main.py and rag/api.py). Every module then does:
    import logging
    logger = logging.getLogger(__name__)

and gets a pre-configured logger scoped to its package path.

LOG_LEVEL env var overrides the default (INFO). Set LOG_LEVEL=DEBUG to see
per-tool-call traces and per-field validation details during development.
"""

import logging
import os
import sys


def setup_logging(level: str = None) -> None:
    level_str = (level or os.getenv("LOG_LEVEL", "INFO")).upper()
    level_int = getattr(logging, level_str, logging.INFO)

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    for pkg in ("pipeline", "rag", "__main__"):
        lg = logging.getLogger(pkg)
        lg.setLevel(level_int)
        if not lg.handlers:
            lg.addHandler(handler)
        lg.propagate = False

    # Third-party libraries log aggressively at INFO/DEBUG — suppress unless debugging them
    for noisy in ("httpx", "httpcore", "openai", "anthropic", "qdrant_client",
                  "google", "urllib3", "requests"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
