"""Имитация нестабильности: MOCK_DELAY_MS (задержка), MOCK_FAIL_RATE (доля ответов 503)."""
import os
import random
import time


def delay_and_should_fail() -> bool:
    delay = float(os.environ.get("MOCK_DELAY_MS", "0") or 0)
    if delay > 0:
        time.sleep(delay / 1000)
    rate = float(os.environ.get("MOCK_FAIL_RATE", "0") or 0)
    return rate > 0 and random.random() < rate
