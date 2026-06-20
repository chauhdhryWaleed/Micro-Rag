"""
Tests for pipeline/utils.py — retry logic.

These are the most fundamental tests: if retry breaks, every external call fails.
We test the mechanics (attempt count, delay behaviour) without actually sleeping
by patching time.sleep.
"""

import pytest
from unittest.mock import patch, call


def test_retry_succeeds_on_first_attempt():
    """Happy path: function returns immediately, no sleep called."""
    from pipeline.utils import retry_with_backoff

    calls = []

    def always_ok():
        calls.append(1)
        return "success"

    with patch("pipeline.utils.time.sleep") as mock_sleep:
        result = retry_with_backoff(always_ok, max_retries=3)

    assert result == "success"
    assert len(calls) == 1
    mock_sleep.assert_not_called()


def test_retry_succeeds_on_second_attempt():
    """Transient failure: first call fails, second succeeds."""
    from pipeline.utils import retry_with_backoff

    attempt = {"n": 0}

    def flaky():
        attempt["n"] += 1
        if attempt["n"] < 2:
            raise ValueError("transient error")
        return "recovered"

    with patch("pipeline.utils.time.sleep"):
        result = retry_with_backoff(flaky, max_retries=3, base_delay=0)

    assert result == "recovered"
    assert attempt["n"] == 2


def test_retry_raises_after_max_retries():
    """Permanent failure: all retries exhausted, original exception propagates."""
    from pipeline.utils import retry_with_backoff

    def always_fail():
        raise RuntimeError("always broken")

    with patch("pipeline.utils.time.sleep"):
        with pytest.raises(RuntimeError, match="always broken"):
            retry_with_backoff(always_fail, max_retries=2)


def test_retry_attempt_count_matches_max_retries():
    """Verify total call count = 1 initial + max_retries."""
    from pipeline.utils import retry_with_backoff

    calls = []

    def always_fail():
        calls.append(1)
        raise ValueError("fail")

    with patch("pipeline.utils.time.sleep"):
        with pytest.raises(ValueError):
            retry_with_backoff(always_fail, max_retries=3)

    assert len(calls) == 4  # 1 initial + 3 retries


def test_retry_sleep_called_between_attempts():
    """Sleep must be called between retries — we don't care about the exact delay value."""
    from pipeline.utils import retry_with_backoff

    def always_fail():
        raise ValueError("fail")

    with patch("pipeline.utils.time.sleep") as mock_sleep:
        with patch("pipeline.utils.random.uniform", return_value=0):
            with pytest.raises(ValueError):
                retry_with_backoff(always_fail, max_retries=2, base_delay=1.0)

    # Sleep called once per failed attempt (not after the final one)
    assert mock_sleep.call_count == 2


def test_retry_passes_args_and_kwargs():
    """Positional and keyword args must reach the wrapped function unchanged."""
    from pipeline.utils import retry_with_backoff

    received = {}

    def capture(a, b, key=None):
        received["a"] = a
        received["b"] = b
        received["key"] = key
        return "ok"

    with patch("pipeline.utils.time.sleep"):
        retry_with_backoff(capture, 1, 2, max_retries=1, key="value")

    assert received == {"a": 1, "b": 2, "key": "value"}
