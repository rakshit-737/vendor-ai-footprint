"""Per-host token-bucket rate limiter (bucket size 1: strict spacing)."""

from __future__ import annotations

import threading
import time
from typing import Callable


class RateLimiter:
    def __init__(self, clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        self._clock = clock
        self._sleep = sleep
        self._next: dict[str, float] = {}
        self._lock = threading.Lock()

    @staticmethod
    def interval(max_rps: float, crawl_delay: float | None = None) -> float:
        base = 1.0 / max_rps if max_rps and max_rps > 0 else 0.0
        return max(base, float(crawl_delay or 0.0))

    def wait(self, host: str, max_rps: float, crawl_delay: float | None = None) -> float:
        """Block until a request to ``host`` is permitted; returns seconds slept."""
        gap = self.interval(max_rps, crawl_delay)
        key = host.lower()
        with self._lock:
            now = self._clock()
            ready = self._next.get(key, now)
            delay = max(0.0, ready - now)
            self._next[key] = max(now, ready) + gap
        if delay > 0:
            self._sleep(delay)
        return delay
