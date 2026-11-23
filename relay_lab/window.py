"""A rolling TPM window over completed requests.

Why this exists: a per-stage TPM average hides the thing operators actually care about —
whether the site is *currently* sustaining throughput, or whether it front-loaded a burst
and then stalled. A mean cannot show a stall; a sliding window can.

The window keeps only timestamps inside the retention period, so memory is bounded by
``retention / interval`` samples and never grows with request count. Samples are taken on
demand from the completion log rather than by a timer, so a stopped run has no thread
left ticking.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class Completion:
    """One finished request: when it landed and how many tokens it moved."""

    at: float
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class TokenWindow:
    """Token throughput over a trailing time window, plus the peak seen."""

    def __init__(self, seconds: float = 60.0, retention: float = 300.0) -> None:
        if seconds <= 0:
            raise ValueError('window must be positive')
        self.seconds = float(seconds)
        self.retention = float(max(retention, self.seconds))
        self._events: deque[Completion] = deque()
        self._lock = threading.Lock()
        self._peak_total = 0.0
        self._peak_input = 0.0
        self._peak_output = 0.0

    def record(self, prompt_tokens: int = 0, completion_tokens: int = 0, *, now: float | None = None) -> None:
        """Add one completed request; token counts are clamped to non-negative."""
        moment = time.time() if now is None else now
        event = Completion(
            at=moment,
            prompt_tokens=max(0, int(prompt_tokens or 0)),
            completion_tokens=max(0, int(completion_tokens or 0)),
        )
        with self._lock:
            self._events.append(event)
            self._trim(moment)
            totals = self._sums(moment)
            self._peak_total = max(self._peak_total, totals[0])
            self._peak_input = max(self._peak_input, totals[1])
            self._peak_output = max(self._peak_output, totals[2])

    def _trim(self, now: float) -> None:
        cutoff = now - self.retention
        while self._events and self._events[0].at < cutoff:
            self._events.popleft()

    def _sums(self, now: float, seconds: float | None = None) -> tuple[float, float, float]:
        """Per-minute rates over the trailing window, scaled to 60 seconds."""
        span = self.seconds if seconds is None else seconds
        cutoff = now - span
        total = prompt = completion = 0
        for event in reversed(self._events):
            if event.at < cutoff:
                break
            total += event.total_tokens
            prompt += event.prompt_tokens
            completion += event.completion_tokens
        scale = 60.0 / span
        return total * scale, prompt * scale, completion * scale

    def snapshot(self, *, now: float | None = None) -> dict[str, float]:
        """Current window values and the peak observed since the run started."""
        moment = time.time() if now is None else now
        with self._lock:
            self._trim(moment)
            total, prompt, completion = self._sums(moment)
            return {
                'window_seconds': self.seconds,
                'total_tpm': round(total, 1),
                'input_tpm': round(prompt, 1),
                'output_tpm': round(completion, 1),
                'peak_total_tpm': round(self._peak_total, 1),
                'peak_input_tpm': round(self._peak_input, 1),
                'peak_output_tpm': round(self._peak_output, 1),
                'samples': len(self._events),
            }

    def clear(self) -> None:
        with self._lock:
            self._events.clear()
            self._peak_total = self._peak_input = self._peak_output = 0.0


def series(events: list[Completion], seconds: float = 60.0, step: float = 5.0) -> list[dict[str, float]]:
    """Reconstruct the window at fixed offsets, for charting a finished run.

    Pure function so a stored run can be re-plotted without replaying it. Offsets are
    relative to the first completion; an empty list yields no points rather than zeros,
    because a flat zero line reads as "the site stalled" when nothing was measured.
    """
    if not events:
        return []
    ordered = sorted(events, key=lambda event: event.at)
    start, end = ordered[0].at, ordered[-1].at
    points: list[dict[str, float]] = []
    moment = start
    index = 0
    window: deque[Completion] = deque()
    while moment <= end + step:
        while index < len(ordered) and ordered[index].at <= moment:
            window.append(ordered[index])
            index += 1
        cutoff = moment - seconds
        while window and window[0].at < cutoff:
            window.popleft()
        total = sum(event.total_tokens for event in window)
        points.append({'offset_seconds': round(moment - start, 1), 'total_tpm': round(total * 60 / seconds, 1)})
        moment += step
    return points


def required_tokens(rate: float, seconds: float) -> float:
    """Tokens a site must move to sustain *rate* TPM across *seconds*."""
    return max(0.0, rate) * max(0.0, seconds) / 60.0


def is_plateau(values: list[float], *, tolerance: float = 0.05) -> bool:
    """Whether the tail of a series stopped growing.

    Used to describe, not to conclude: a plateau is consistent with a real saturation
    point and also with a client-side bottleneck, so callers must word the result as an
    observation. ``math.isfinite`` is enforced because a NaN would otherwise make every
    comparison False and read as "no plateau" regardless of the data.
    """
    clean = [value for value in values if isinstance(value, (int, float)) and math.isfinite(value)]
    if len(clean) < 3:
        return False
    peak = max(clean)
    if peak <= 0:
        return False
    tail = clean[-3:]
    return all(value >= peak * (1 - tolerance) for value in tail)
