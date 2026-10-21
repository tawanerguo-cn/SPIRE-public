from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
import time

from .models import SignalReference, SignalValue


@dataclass(frozen=True)
class SignalChartSample:
    timestamp_s: float
    values: list[SignalValue]


@dataclass(frozen=True)
class SignalPollingBenchmark:
    samples: int
    duration_s: float
    samples_per_s: float
    mean_interval_s: float | None
    min_interval_s: float | None
    max_interval_s: float | None


def sample_signal_chart(
    device,
    references: Sequence[SignalReference],
    *,
    wait_newest: bool = False,
    clock: Callable[[], float] = time.perf_counter,
) -> SignalChartSample:
    """Read one Signal Chart style sample through Signals.ValsGet."""

    values = device.read_resolved_signals(references, wait_newest=wait_newest)
    return SignalChartSample(timestamp_s=float(clock()), values=values)


def benchmark_signal_polling(
    device,
    references: Sequence[SignalReference],
    *,
    duration_s: float = 2.0,
    wait_newest: bool = False,
    clock: Callable[[], float] = time.perf_counter,
) -> SignalPollingBenchmark:
    """Measure read-only Signals.ValsGet polling throughput."""

    start = float(clock())
    timestamps: list[float] = []
    while True:
        now = float(clock())
        if now - start >= duration_s:
            break
        device.read_resolved_signals(references, wait_newest=wait_newest)
        timestamps.append(float(clock()))

    end = float(clock())
    elapsed = max(0.0, end - start)
    intervals = [right - left for left, right in zip(timestamps, timestamps[1:], strict=False)]
    return SignalPollingBenchmark(
        samples=len(timestamps),
        duration_s=elapsed,
        samples_per_s=(len(timestamps) / elapsed) if elapsed > 0 else 0.0,
        mean_interval_s=(sum(intervals) / len(intervals)) if intervals else None,
        min_interval_s=min(intervals) if intervals else None,
        max_interval_s=max(intervals) if intervals else None,
    )
