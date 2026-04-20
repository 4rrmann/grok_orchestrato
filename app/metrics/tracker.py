"""
Metrics Tracker — an in-process, lightweight observability layer.

Why have metrics separate from the database?
  The database stores *durable* state (fail counts, latency averages) that
  survives restarts. The metrics tracker stores *operational snapshots* that
  are useful right now but don't need to outlive the process:
    - Request rate over the last 60 seconds
    - p50/p95/p99 latency distribution
    - Per-key request breakdown since startup

In production you would replace this with Prometheus counters/histograms
(exported via /metrics for scraping by Prometheus) or push metrics to
Datadog/CloudWatch. The interface here is intentionally simple so that
swapping the backend is a one-file change.

Design notes:
  - All in-memory: no I/O, no DB, no network calls
  - Thread-safe writes via asyncio.Lock (since FastAPI is single-threaded
    but coroutines may interleave)
  - A rolling window deque keeps memory bounded — we don't accumulate
    unbounded history
"""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from statistics import mean, median, quantiles
from typing import Optional

from app.core.logging_config import get_logger

log = get_logger(__name__)

# How many recent request latencies to keep in the rolling window.
# At 100 RPS this is 10 seconds of history — enough for meaningful percentiles.
ROLLING_WINDOW_SIZE = 1000


@dataclass
class RequestRecord:
    """One record per completed request."""
    timestamp: datetime
    key_id: int
    key_alias: str
    latency_ms: float
    success: bool
    attempts: int
    error_type: Optional[str] = None


@dataclass
class KeyMetrics:
    """Accumulated metrics for a single API key since process start."""
    key_id: int
    alias: str
    total_requests: int = 0
    total_failures: int = 0
    total_latency_ms: float = 0.0

    @property
    def success_rate(self) -> float:
        if self.total_requests == 0:
            return 0.0
        return (self.total_requests - self.total_failures) / self.total_requests

    @property
    def avg_latency_ms(self) -> float:
        if self.total_requests == 0:
            return 0.0
        return self.total_latency_ms / self.total_requests


class MetricsTracker:
    """
    Singleton metrics collector.

    Usage from anywhere in the application:
        from app.metrics.tracker import metrics_tracker
        await metrics_tracker.record_request(...)
        stats = metrics_tracker.get_summary()
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        # Rolling window of recent requests — bounded memory
        self._recent: deque[RequestRecord] = deque(maxlen=ROLLING_WINDOW_SIZE)
        # Per-key breakdown
        self._key_metrics: dict[int, KeyMetrics] = defaultdict(
            lambda: KeyMetrics(key_id=0, alias="unknown")
        )
        self._started_at = datetime.now(timezone.utc)
        self._total_requests = 0
        self._total_failures = 0

    async def record_request(
        self,
        key_id: int,
        key_alias: str,
        latency_ms: float,
        success: bool,
        attempts: int,
        error_type: Optional[str] = None,
    ) -> None:
        """Record metrics for a completed request. Call from the Orchestrator."""
        record = RequestRecord(
            timestamp=datetime.now(timezone.utc),
            key_id=key_id,
            key_alias=key_alias,
            latency_ms=latency_ms,
            success=success,
            attempts=attempts,
            error_type=error_type,
        )

        async with self._lock:
            self._recent.append(record)
            self._total_requests += 1
            if not success:
                self._total_failures += 1

            km = self._key_metrics[key_id]
            km.key_id = key_id
            km.alias = key_alias
            km.total_requests += 1
            km.total_latency_ms += latency_ms
            if not success:
                km.total_failures += 1

    def get_summary(self) -> dict:
        """
        Return a snapshot of system health metrics.

        This is what the /metrics endpoint exposes. All calculations
        are done on a copy of the deque to avoid holding the lock
        during computation.
        """
        recent = list(self._recent)  # snapshot — safe to read without lock

        latencies = [r.latency_ms for r in recent]
        success_count = sum(1 for r in recent if r.success)
        failure_count = len(recent) - success_count

        # Latency percentiles — requires at least 2 data points
        p50 = p95 = p99 = 0.0
        if len(latencies) >= 2:
            qs = quantiles(latencies, n=100)
            p50 = qs[49]
            p95 = qs[94]
            p99 = qs[98]
        elif latencies:
            p50 = p95 = p99 = latencies[0]

        uptime_seconds = (
            datetime.now(timezone.utc) - self._started_at
        ).total_seconds()

        return {
            "uptime_seconds": round(uptime_seconds),
            "total_requests_lifetime": self._total_requests,
            "total_failures_lifetime": self._total_failures,
            "rolling_window": {
                "size": len(recent),
                "success": success_count,
                "failure": failure_count,
                "avg_latency_ms": round(mean(latencies), 2) if latencies else 0.0,
                "p50_latency_ms": round(p50, 2),
                "p95_latency_ms": round(p95, 2),
                "p99_latency_ms": round(p99, 2),
            },
            "per_key": [
                {
                    "key_id": km.key_id,
                    "alias": km.alias,
                    "total_requests": km.total_requests,
                    "success_rate": round(km.success_rate, 4),
                    "avg_latency_ms": round(km.avg_latency_ms, 2),
                }
                for km in self._key_metrics.values()
            ],
        }


# Module-level singleton — imported by Orchestrator and health routes
metrics_tracker = MetricsTracker()
