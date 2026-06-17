import logging
import random
import time

logger = logging.getLogger(__name__)


def retry_with_backoff(func, *args, max_retries=3, base_delay=2.0, **kwargs):
    """
    Call func(*args, **kwargs), retrying up to max_retries times on any exception.
    Delay doubles each attempt plus a small random jitter to avoid thundering herd.
    Raises the final exception if all retries are exhausted.
    """
    for attempt in range(max_retries + 1):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            if attempt == max_retries:
                logger.error(
                    "All retries exhausted for %s | attempts=%d | error=%s",
                    getattr(func, "__name__", str(func)), max_retries + 1, e,
                )
                raise
            delay = base_delay * (2 ** attempt) + random.uniform(0, 1)
            logger.warning(
                "Attempt %d/%d failed for %s | error=%s | retrying in %.1fs",
                attempt + 1, max_retries,
                getattr(func, "__name__", str(func)), e, delay,
            )
            time.sleep(delay)
