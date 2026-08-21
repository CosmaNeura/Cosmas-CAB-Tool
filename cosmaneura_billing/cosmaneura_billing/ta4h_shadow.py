"""Bounded, non-blocking TA4H shadow execution and PHI-safe observability.

The runner may hold at most a small, fixed number of in-memory visit tasks while
TA4H processes them. It never persists or logs those tasks. The metrics boundary
cannot retain an encounter identifier, clinical text, code value, evidence
span, model response, or exception: it reduces each comparison to bounded
counters and exposes only the closed schema returned by ``snapshot``.

The worker is a daemon thread, saturation drops shadow work immediately, and
shutdown is bounded. Therefore a slow or stuck secondary service cannot delay
reviewer output, consume an unbounded queue, or hold process termination open.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from queue import Empty, Queue
from threading import (
    BoundedSemaphore,
    Condition,
    Event,
    Lock,
    Thread,
    current_thread,
)
from time import monotonic, perf_counter
from typing import Any, Iterable, Protocol

from .schemas import CodeKind, CodeSuggestion, VisitBundle


_MAX_COUNT = 1_000_000_000
_MAX_LATENCY_MS = 86_400_000
_MODES = {"off", "shadow", "active_consensus"}
_STATUSES = {"disabled", "not_run", "healthy", "degraded", "active"}


def _bounded_add(current: int, value: int, *, maximum: int = _MAX_COUNT) -> int:
    if type(value) is not int or value < 0:
        value = 0
    return min(maximum, current + value)


def _diagnosis_codes(suggestions: Iterable[CodeSuggestion]) -> frozenset[str]:
    """Return an ephemeral, code-only projection for aggregate comparison."""

    return frozenset(
        suggestion.code
        for suggestion in suggestions
        if suggestion.kind == CodeKind.ICD10 and isinstance(suggestion.code, str)
    )


class SecondaryCoding(Protocol):
    def run(self, bundle: VisitBundle) -> tuple[dict[str, Any], list[CodeSuggestion]]:
        ...


class TA4HShadowMetrics:
    """Thread-safe, in-memory, closed-schema aggregate comparison counters."""

    def __init__(self, mode: str = "off") -> None:
        if mode not in _MODES:
            raise ValueError("invalid TA4H operating mode")
        self._mode = mode
        self._lock = Lock()
        self._runs = 0
        self._pending = 0
        self._succeeded = 0
        self._failed = 0
        self._dropped = 0
        self._primary = 0
        self._secondary = 0
        self._agreed = 0
        self._primary_only = 0
        self._secondary_only = 0
        self._secondary_vetoes = 0
        self._secondary_entities = 0
        self._total_latency_ms = 0
        self._max_latency_ms = 0

    @staticmethod
    def elapsed_ms(started_at: float) -> int:
        elapsed = int(max(0.0, (perf_counter() - started_at) * 1_000))
        return min(_MAX_LATENCY_MS, elapsed)

    def record_submission(self) -> None:
        with self._lock:
            self._runs = _bounded_add(self._runs, 1)
            self._pending = _bounded_add(self._pending, 1)

    def record_drop(self) -> None:
        """Record work refused before queueing; no input value is accepted."""

        with self._lock:
            self._runs = _bounded_add(self._runs, 1)
            self._dropped = _bounded_add(self._dropped, 1)

    def record_cancelled(self) -> None:
        """Convert one already-submitted task into a bounded drop counter."""

        with self._lock:
            self._pending = max(0, self._pending - 1)
            self._dropped = _bounded_add(self._dropped, 1)

    def record_success(
        self,
        primary: frozenset[str],
        secondary_facts: dict[str, Any],
        secondary_suggestions: Iterable[CodeSuggestion],
        *,
        latency_ms: int,
    ) -> None:
        """Reduce one successful comparison without retaining its values."""

        secondary = _diagnosis_codes(secondary_suggestions)
        contradictions = secondary_facts.get("ta4h_contradictions", {})
        vetoes = len(contradictions) if isinstance(contradictions, dict) else 0
        entity_count = secondary_facts.get("ta4h_entity_count", 0)
        if type(entity_count) is not int or entity_count < 0:
            entity_count = 0
        latency = (
            latency_ms
            if type(latency_ms) is int and 0 <= latency_ms <= _MAX_LATENCY_MS
            else 0
        )
        with self._lock:
            self._pending = max(0, self._pending - 1)
            self._succeeded = _bounded_add(self._succeeded, 1)
            self._primary = _bounded_add(self._primary, len(primary))
            self._secondary = _bounded_add(self._secondary, len(secondary))
            self._agreed = _bounded_add(self._agreed, len(primary & secondary))
            self._primary_only = _bounded_add(
                self._primary_only, len(primary - secondary),
            )
            self._secondary_only = _bounded_add(
                self._secondary_only, len(secondary - primary),
            )
            self._secondary_vetoes = _bounded_add(self._secondary_vetoes, vetoes)
            self._secondary_entities = _bounded_add(
                self._secondary_entities, entity_count,
            )
            self._total_latency_ms = _bounded_add(
                self._total_latency_ms, latency, maximum=_MAX_LATENCY_MS,
            )
            self._max_latency_ms = max(self._max_latency_ms, latency)

    def record_failure(self, *, latency_ms: int) -> None:
        """Record only that TA4H failed; exception text is never accepted."""

        latency = (
            latency_ms
            if type(latency_ms) is int and 0 <= latency_ms <= _MAX_LATENCY_MS
            else 0
        )
        with self._lock:
            self._pending = max(0, self._pending - 1)
            self._failed = _bounded_add(self._failed, 1)
            self._total_latency_ms = _bounded_add(
                self._total_latency_ms, latency, maximum=_MAX_LATENCY_MS,
            )
            self._max_latency_ms = max(self._max_latency_ms, latency)

    def snapshot(self) -> dict[str, str | int]:
        """Return the only supported observability schema for this lane."""

        with self._lock:
            if self._mode == "off":
                status = "disabled"
            elif self._mode == "active_consensus":
                status = "active"
            elif self._runs == 0:
                status = "not_run"
            elif self._failed or self._dropped:
                status = "degraded"
            else:
                status = "healthy"
            payload: dict[str, str | int] = {
                "mode": self._mode,
                "status": status,
                "runs": self._runs,
                "pending": self._pending,
                "succeeded": self._succeeded,
                "failed": self._failed,
                "dropped": self._dropped,
                "primaryDiagnosisCandidates": self._primary,
                "secondaryDiagnosisCandidates": self._secondary,
                "agreedDiagnosisCandidates": self._agreed,
                "primaryOnlyDiagnosisCandidates": self._primary_only,
                "secondaryOnlyDiagnosisCandidates": self._secondary_only,
                "secondaryVetoCandidates": self._secondary_vetoes,
                "secondaryEntityCount": self._secondary_entities,
                "totalLatencyMs": self._total_latency_ms,
                "maxLatencyMs": self._max_latency_ms,
            }
        if not is_safe_ta4h_shadow_snapshot(payload):
            raise RuntimeError("invalid TA4H shadow metric snapshot")
        return payload


_SNAPSHOT_FIELDS = {
    "mode",
    "status",
    "runs",
    "pending",
    "succeeded",
    "failed",
    "dropped",
    "primaryDiagnosisCandidates",
    "secondaryDiagnosisCandidates",
    "agreedDiagnosisCandidates",
    "primaryOnlyDiagnosisCandidates",
    "secondaryOnlyDiagnosisCandidates",
    "secondaryVetoCandidates",
    "secondaryEntityCount",
    "totalLatencyMs",
    "maxLatencyMs",
}


def is_safe_ta4h_shadow_snapshot(payload: object) -> bool:
    """Reject field expansion and all free-form values."""

    if not isinstance(payload, dict) or set(payload) != _SNAPSHOT_FIELDS:
        return False
    if payload.get("mode") not in _MODES or payload.get("status") not in _STATUSES:
        return False
    for field in _SNAPSHOT_FIELDS - {"mode", "status"}:
        maximum = _MAX_LATENCY_MS if field.endswith("LatencyMs") else _MAX_COUNT
        value = payload.get(field)
        if type(value) is not int or not 0 <= value <= maximum:
            return False
    return (
        min(
            _MAX_COUNT,
            payload["pending"]
            + payload["succeeded"]
            + payload["failed"]
            + payload["dropped"],
        ) == payload["runs"]
        and payload["agreedDiagnosisCandidates"]
        <= min(
            payload["primaryDiagnosisCandidates"],
            payload["secondaryDiagnosisCandidates"],
        )
    )


@dataclass(frozen=True)
class _ShadowTask:
    bundle: VisitBundle
    primary_codes: frozenset[str]
    started_at: float


class TA4HShadowRunner:
    """Single-worker, bounded, non-blocking observation lane."""

    def __init__(
        self,
        secondary: SecondaryCoding,
        metrics: TA4HShadowMetrics,
        *,
        max_inflight: int = 2,
    ) -> None:
        if type(max_inflight) is not int or not 1 <= max_inflight <= 100:
            raise ValueError("TA4H shadow max_inflight must be between 1 and 100")
        self._secondary = secondary
        self._metrics = metrics
        self._queue: Queue[_ShadowTask] = Queue(maxsize=max_inflight)
        self._slots = BoundedSemaphore(max_inflight)
        self._stop = Event()
        self._lock = Lock()
        self._idle = Condition(self._lock)
        self._thread: Thread | None = None
        self._inflight = 0
        self._closed = False

    def observe(
        self,
        bundle: VisitBundle,
        primary_suggestions: Iterable[CodeSuggestion],
    ) -> bool:
        """Queue one observation without waiting; return false when dropped."""

        try:
            primary_codes = _diagnosis_codes(primary_suggestions)
            # The primary pipeline continues to read ``bundle`` after this
            # dispatch. Give the untrusted secondary a recursively detached
            # copy so mutation of submitted_icd10 (or any future nested field)
            # cannot race or influence reviewer output.
            shadow_bundle = deepcopy(bundle)
        except Exception:
            self._safe_drop()
            return False
        if not self._slots.acquire(blocking=False):
            self._safe_drop()
            return False

        task = _ShadowTask(shadow_bundle, primary_codes, perf_counter())
        with self._lock:
            if self._closed:
                self._slots.release()
                self._safe_drop()
                return False
            recorded = False
            try:
                if self._thread is None:
                    self._thread = Thread(
                        target=self._work,
                        name="cosmaneura-ta4h-shadow",
                        daemon=True,
                    )
                    self._thread.start()
                self._metrics.record_submission()
                recorded = True
                self._inflight += 1
                self._queue.put_nowait(task)
            except Exception:
                self._inflight = max(0, self._inflight - 1)
                self._slots.release()
                if recorded:
                    try:
                        self._metrics.record_cancelled()
                    except Exception:
                        pass
                else:
                    if self._thread is not None and not self._thread.is_alive():
                        self._thread = None
                    self._safe_drop()
                return False
        return True

    def _safe_drop(self) -> None:
        try:
            self._metrics.record_drop()
        except Exception:
            pass

    def _work(self) -> None:
        while not self._stop.is_set():
            try:
                task = self._queue.get(timeout=0.05)
            except Empty:
                continue
            try:
                secondary_facts, secondary_suggestions = self._secondary.run(
                    task.bundle,
                )
                if not isinstance(secondary_facts, dict):
                    raise TypeError("invalid shadow facts")
                self._metrics.record_success(
                    task.primary_codes,
                    secondary_facts,
                    secondary_suggestions,
                    latency_ms=self._metrics.elapsed_ms(task.started_at),
                )
            except Exception:
                try:
                    self._metrics.record_failure(
                        latency_ms=self._metrics.elapsed_ms(task.started_at),
                    )
                except Exception:
                    pass
            finally:
                self._queue.task_done()
                self._slots.release()
                with self._idle:
                    self._inflight = max(0, self._inflight - 1)
                    self._idle.notify_all()

    def wait_for_idle(self, timeout: float) -> bool:
        """Acceptance/test synchronization; never used on reviewer requests."""

        if not isinstance(timeout, (int, float)) or timeout < 0:
            raise ValueError("timeout must be non-negative")
        deadline = monotonic() + float(timeout)
        with self._idle:
            while self._inflight:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    return False
                self._idle.wait(remaining)
            return True

    def shutdown(self, timeout: float = 0.05) -> None:
        """Stop accepting work, discard queued tasks, and return on a bound."""

        with self._lock:
            self._closed = True
            self._stop.set()
        while True:
            try:
                self._queue.get_nowait()
            except Empty:
                break
            else:
                self._queue.task_done()
                self._slots.release()
                try:
                    self._metrics.record_cancelled()
                except Exception:
                    pass
                with self._idle:
                    self._inflight = max(0, self._inflight - 1)
                    self._idle.notify_all()
        thread = self._thread
        if thread is not None and thread is not current_thread():
            thread.join(max(0.0, min(float(timeout), 1.0)))


__all__ = [
    "TA4HShadowMetrics",
    "TA4HShadowRunner",
    "is_safe_ta4h_shadow_snapshot",
]
