"""Pluggable roster sources for the direct-contract gate (F3).

The gate in :mod:`cosmaneura_billing.direct_contract` reports one fact — is this
patient on the practice's direct-contract roster — and fails closed on any
unknown answer. Its docstring anticipates the open question of *where* that
roster lives (synthesis §8 Q5): *"when the real source appears, only
``DirectContractList`` changes — the gate semantics do not."*

This module is that seam. A source produces a :class:`DirectContractSnapshot`;
``DirectContractList(source=...)`` consumes it while keeping every gate
semantic — fail-closed unknowns, the HMAC-keyed release fingerprint, and the
two caller policies — unchanged. The built-in versioned-local-file path is
untouched and remains the default.

Fail-closed is the contract for a source too: ``refs is None`` means unavailable
and the gate blocks. A loader that raises, returns ``None``, or returns anything
malformed is treated as unavailable — never as an empty allow-through roster.

When the practice's real source is decided (an Athena read, a practice API, or a
maintained form export), it is implemented as a ``DirectContractSource`` — or
simply wrapped with :class:`CallableDirectContractSource` — and injected. No gate
code changes.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Callable, Iterable, Optional, Protocol, runtime_checkable


def _normalize(ref: str) -> str:
    """Match ``DirectContractList._normalize`` exactly (strip + casefold)."""
    return (ref or "").strip().casefold()


@dataclass(frozen=True)
class DirectContractSnapshot:
    """One roster read.

    ``refs is None`` is the fail-closed signal: the roster is unavailable and the
    gate must block. An empty (but not ``None``) frozenset is a *known* empty
    roster — every patient is "not direct-contract, list available".
    """

    refs: Optional[frozenset[str]]
    source_name: str = ""
    version: str = ""
    content_signature: str = ""
    load_error: str = ""


@runtime_checkable
class DirectContractSource(Protocol):
    """A roster provider. ``load`` must never raise; it fails closed instead."""

    def load(self) -> DirectContractSnapshot:  # pragma: no cover - protocol
        ...


def _content_signature(refs: frozenset[str], version: str) -> str:
    payload = json.dumps(
        {"refs": sorted(refs), "version": version},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _normalize_refs(raw: Iterable[str]) -> Optional[frozenset[str]]:
    """Return a normalized frozenset, or ``None`` if any entry is not a string.

    A malformed roster fails closed rather than silently dropping bad rows: a
    partially-parsed allow-list is exactly the kind of quiet wrong answer the
    gate refuses to produce.
    """
    collected: set[str] = set()
    for entry in raw:
        if not isinstance(entry, str):
            return None
        normalized = _normalize(entry)
        if normalized:
            collected.add(normalized)
    return frozenset(collected)


class InMemoryDirectContractSource:
    """A fixed in-memory roster, for tests, demos, and seeded pilots."""

    def __init__(
        self,
        refs: Iterable[str],
        *,
        source_name: str = "in_memory",
        version: str = "test",
    ) -> None:
        normalized = _normalize_refs(refs)
        if normalized is None:
            raise TypeError("direct-contract refs must be strings")
        self._refs = normalized
        self._source_name = source_name
        self._version = version
        self._signature = _content_signature(self._refs, version)

    def load(self) -> DirectContractSnapshot:
        return DirectContractSnapshot(
            refs=self._refs,
            source_name=self._source_name,
            version=self._version,
            content_signature=self._signature,
        )


class CallableDirectContractSource:
    """Receiver-first hook for a future real roster source.

    ``loader`` returns an iterable of patient references, or ``None`` to signal
    the roster is unavailable. Any exception, a ``None`` return, or a
    non-string entry fails closed (the gate then blocks). Successful reads are
    cached in-process by default so a per-lookup gate check does not re-hit a
    remote source; unavailable/failed reads are never cached, so a transient
    outage can recover on the next call.
    """

    def __init__(
        self,
        loader: Callable[[], Optional[Iterable[str]]],
        *,
        source_name: str,
        version: str = "",
        cache: bool = True,
    ) -> None:
        if not callable(loader):
            raise TypeError("loader must be callable")
        self._loader = loader
        self._source_name = source_name
        self._version = version
        self._cache = cache
        self._cached: Optional[DirectContractSnapshot] = None

    def _unavailable(self, error: str) -> DirectContractSnapshot:
        return DirectContractSnapshot(
            refs=None,
            source_name=self._source_name,
            version=self._version,
            load_error=error,
        )

    def load(self) -> DirectContractSnapshot:
        if self._cache and self._cached is not None:
            return self._cached
        try:
            raw = self._loader()
        except Exception:
            return self._unavailable("loader_error")
        if raw is None:
            return self._unavailable("unavailable")
        try:
            refs = _normalize_refs(raw)
        except TypeError:
            return self._unavailable("loader_malformed")
        if refs is None:
            return self._unavailable("loader_malformed")
        snapshot = DirectContractSnapshot(
            refs=refs,
            source_name=self._source_name,
            version=self._version,
            content_signature=_content_signature(refs, self._version),
        )
        if self._cache:
            self._cached = snapshot
        return snapshot


__all__ = [
    "CallableDirectContractSource",
    "DirectContractSnapshot",
    "DirectContractSource",
    "InMemoryDirectContractSource",
    "_normalize_refs",
]
