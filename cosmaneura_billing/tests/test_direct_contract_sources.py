"""Tests for pluggable direct-contract roster sources (B2).

The gate semantics must not change when a source is injected: unknown answers
still fail closed, matches still block charging and route chase to review, and a
keyed roster fingerprint still binds a release to the exact answer. The default
(no-source) file/explicit paths are untouched and covered by
``test_direct_contract.py``.
"""
from __future__ import annotations

import pytest

from cosmaneura_billing.direct_contract import (
    DirectContractBlocked,
    DirectContractList,
    chase_routing,
    check_direct_contract,
    guard_patient_charge,
)
from cosmaneura_billing.direct_contract_sources import (
    CallableDirectContractSource,
    DirectContractSnapshot,
    InMemoryDirectContractSource,
)

FIXED_NOW = lambda: "2026-09-03T00:00:00+00:00"  # noqa: E731


def _roster(source, **kw):
    return DirectContractList(source=source, **kw)


# --------------------------------------------------------------------------- #
# In-memory source
# --------------------------------------------------------------------------- #
def test_in_memory_source_match_and_nonmatch():
    roster = _roster(InMemoryDirectContractSource(["PAT-1", "PAT-2"]))
    assert roster.available is True

    hit = check_direct_contract("pat-1", roster=roster, now=FIXED_NOW)
    assert hit.known is True and hit.is_direct_contract is True and hit.blocked is True

    miss = check_direct_contract("pat-9", roster=roster, now=FIXED_NOW)
    assert miss.known is True and miss.is_direct_contract is False and miss.blocked is False


def test_in_memory_source_normalizes_refs():
    roster = _roster(InMemoryDirectContractSource(["  MixedCase  "]))
    check = check_direct_contract("mixedcase", roster=roster, now=FIXED_NOW)
    assert check.is_direct_contract is True


def test_in_memory_source_rejects_non_string_refs():
    with pytest.raises(TypeError):
        InMemoryDirectContractSource(["ok", 5])


# --------------------------------------------------------------------------- #
# Callable source — the receiver-first hook
# --------------------------------------------------------------------------- #
def test_callable_source_routes_and_charges():
    source = CallableDirectContractSource(
        lambda: ["PAT-1"], source_name="practice_api", version="2026.09",
    )
    roster = _roster(source)

    check, decision = chase_routing("pat-1", roster=roster, now=FIXED_NOW)
    assert decision == "review" and check.is_direct_contract is True

    check2, decision2 = chase_routing("pat-2", roster=roster, now=FIXED_NOW)
    assert decision2 == "auto" and check2.blocked is False

    with pytest.raises(DirectContractBlocked):
        guard_patient_charge("pat-1", roster=roster, now=FIXED_NOW)
    # A non-match on an available roster is allowed to proceed.
    ok = guard_patient_charge("pat-2", roster=roster, now=FIXED_NOW)
    assert ok.blocked is False


def test_callable_source_none_fails_closed():
    roster = _roster(CallableDirectContractSource(lambda: None, source_name="down"))
    assert roster.available is False
    check = check_direct_contract("anyone", roster=roster, now=FIXED_NOW)
    assert check.known is False and check.blocked is True
    _, decision = chase_routing("anyone", roster=roster, now=FIXED_NOW)
    assert decision == "review"
    with pytest.raises(DirectContractBlocked):
        guard_patient_charge("anyone", roster=roster, now=FIXED_NOW)


def test_callable_source_exception_fails_closed():
    def boom():
        raise RuntimeError("upstream down")

    roster = _roster(CallableDirectContractSource(boom, source_name="flaky"))
    check = check_direct_contract("anyone", roster=roster, now=FIXED_NOW)
    assert check.known is False and check.blocked is True


def test_callable_source_malformed_fails_closed():
    roster = _roster(
        CallableDirectContractSource(lambda: ["ok", 5], source_name="bad")
    )
    check = check_direct_contract("ok", roster=roster, now=FIXED_NOW)
    # A malformed roster is unavailable, not a partial allow-list.
    assert check.known is False and check.blocked is True


def test_callable_source_known_empty_roster_is_available():
    roster = _roster(CallableDirectContractSource(lambda: [], source_name="empty"))
    assert roster.available is True
    check = check_direct_contract("anyone", roster=roster, now=FIXED_NOW)
    assert check.known is True and check.is_direct_contract is False and check.blocked is False


def test_callable_source_caches_success_but_not_failure():
    calls = {"n": 0}

    def loader():
        calls["n"] += 1
        return None if calls["n"] == 1 else ["PAT-1"]

    source = CallableDirectContractSource(loader, source_name="recover")
    roster = _roster(source)
    # First read is unavailable and must not be cached...
    assert roster.available is False
    # ...so a subsequent read recovers.
    check = check_direct_contract("pat-1", roster=roster, now=FIXED_NOW)
    assert check.is_direct_contract is True
    # And the recovered snapshot is cached: no further loader calls.
    before = calls["n"]
    check_direct_contract("pat-1", roster=roster, now=FIXED_NOW)
    assert calls["n"] == before


# --------------------------------------------------------------------------- #
# Fingerprint / release binding
# --------------------------------------------------------------------------- #
def test_gate_fingerprint_stable_and_content_sensitive():
    a = check_direct_contract(
        "pat-1", roster=_roster(InMemoryDirectContractSource(["PAT-1"])), now=FIXED_NOW)
    b = check_direct_contract(
        "pat-1", roster=_roster(InMemoryDirectContractSource(["PAT-1"])), now=FIXED_NOW)
    assert a.gate_fingerprint == b.gate_fingerprint

    c = check_direct_contract(
        "pat-1",
        roster=_roster(InMemoryDirectContractSource(["PAT-1", "PAT-2"])),
        now=FIXED_NOW,
    )
    # Same answer for pat-1, but a different roster content → different source
    # fingerprint → different gate fingerprint, so a stale release cannot carry.
    assert c.gate_fingerprint != a.gate_fingerprint


def test_keyed_source_fingerprint_binds_refs():
    source = InMemoryDirectContractSource(["PAT-1"])
    unkeyed = _roster(source)
    keyed_a = _roster(InMemoryDirectContractSource(["PAT-1"]), fingerprint_key="k" * 32)
    keyed_b = _roster(InMemoryDirectContractSource(["PAT-1"]), fingerprint_key="k" * 32)
    keyed_c = _roster(InMemoryDirectContractSource(["PAT-1"]), fingerprint_key="j" * 32)

    fp_unkeyed = check_direct_contract("pat-1", roster=unkeyed, now=FIXED_NOW).gate_fingerprint
    fp_a = check_direct_contract("pat-1", roster=keyed_a, now=FIXED_NOW).gate_fingerprint
    fp_b = check_direct_contract("pat-1", roster=keyed_b, now=FIXED_NOW).gate_fingerprint
    fp_c = check_direct_contract("pat-1", roster=keyed_c, now=FIXED_NOW).gate_fingerprint

    assert fp_a == fp_b            # same key, same content -> stable
    assert fp_a != fp_unkeyed      # keying changes the digest
    assert fp_a != fp_c            # different key -> different digest


# --------------------------------------------------------------------------- #
# Construction guards
# --------------------------------------------------------------------------- #
def test_refs_and_source_are_mutually_exclusive():
    with pytest.raises(ValueError):
        DirectContractList(refs=["PAT-1"], source=InMemoryDirectContractSource(["PAT-1"]))


def test_snapshot_none_refs_is_fail_closed_signal():
    class _Src:
        def load(self):
            return DirectContractSnapshot(refs=None, source_name="x", load_error="boom")

    roster = _roster(_Src())
    check = check_direct_contract("anyone", roster=roster, now=FIXED_NOW)
    assert check.known is False and check.blocked is True
