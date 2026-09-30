"""Workload probes: measure user-visible damage (errors, latency) while chaos is running."""
from __future__ import annotations

import math
import threading
import time
import urllib.error
import urllib.request
from typing import Dict, List

from .target import Probes


def percentile(values: List[float], q: float) -> float:
    """Nearest-rank percentile (q in 0..100)."""
    if not values:
        return math.nan
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q / 100 * len(ordered)) - 1)]


class LatencyRecorder:
    def __init__(self) -> None:
        self.ok: List[float] = []
        self.failed: List[float] = []
        self._lock = threading.Lock()

    def record(self, ok: bool, seconds: float) -> None:
        with self._lock:
            (self.ok if ok else self.failed).append(seconds)

    def summary(self, prefix: str = "") -> Dict[str, float]:
        with self._lock:
            total = len(self.ok) + len(self.failed)
            if total == 0:
                return {}
            # failed requests count with the time they took to fail: a timeout IS a slow response
            return {f"{prefix}error_rate": len(self.failed) / total,
                    f"{prefix}p99_latency_s": percentile(self.ok + self.failed, 99),
                    f"{prefix}requests": float(total)}


class HttpProbe(Probes):
    """GETs a URL every `interval` seconds from a background thread."""

    def __init__(self, url: str, interval: float = 0.5, timeout: float = 2.0) -> None:
        self.url, self.interval, self.timeout = url, interval, timeout
        self.rec = LatencyRecorder()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            began = time.monotonic()
            try:
                with urllib.request.urlopen(self.url, timeout=self.timeout) as resp:
                    ok = 200 <= resp.status < 400
            except (urllib.error.HTTPError, urllib.error.URLError, OSError):
                ok = False
            self.rec.record(ok, time.monotonic() - began)
            self._stop.wait(max(0.0, self.interval - (time.monotonic() - began)))

    def stop(self) -> Dict[str, float]:
        self._stop.set()
        self._thread.join(timeout=self.timeout + 1)
        return self.rec.summary()
