from __future__ import annotations

import random


def backoff_delay(attempt: int, base: float = 0.5, cap: float = 30.0) -> float:
    """Returns a full-jitter exponential backoff delay in seconds for a 0-based attempt."""
    return random.uniform(0, min(cap, base * (2**attempt)))
