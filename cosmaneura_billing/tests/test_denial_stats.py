"""Denial priors (CMS PSPS 2024) and payer denial rates (TC-PUF PY2026),
plus the NH state E/M benchmark — real bundled data."""
import pytest

from cosmaneura_billing.agents import DenialRiskAgent
from cosmaneura_billing.benchmarks.em_benchmark import StateEMBenchmark
from cosmaneura_billing.knowledge.denial_stats import DenialPriors, PayerDenialRates
from cosmaneura_billing.orchestrator import BillingOrchestrator
from cosmaneura_billing.schemas import CodeKind, CodeSuggestion, Disposition, Evidence


# ---- per-code priors (PSPS) -------------------------------------------------- #
def test_priors_load_with_plausible_values():
    dp = DenialPriors()
    assert dp.loaded and dp.size > 5000
    em = dp.get("99214")
    assert em is not None and em.submitted > 10_000_000
    assert 0.01 < em.rate < 0.10          # office E/M denies at a few percent
    assert dp.rate("G0438") >= 0.15       # AWV frequency-limit trap
    assert dp.rate("XXXXX") is None


def test_state_overlay_loads_nh_carrier_slice():
    dp = DenialPriors()
    nh = dp.get_state("99214")
    assert nh is not None and nh.submitted > 100_000
    assert nh.rate < dp.rate("99214")     # NGS-NH denies office E/M below national
    assert dp.get_state("G0438").rate >= 0.15
    assert dp.state == "NH"


def _captured(kind, code, label="x"):
    return CodeSuggestion(kind=kind, code=code, label=label,
                          disposition=Disposition.CAPTURED,
                          evidence=[Evidence("signed_note", "q", "r")])


def test_agent_attaches_prior_and_flags_high_rate_codes():
    dp = DenialPriors()
    em = _captured(CodeKind.EM, "99214")
    awv = _captured(CodeKind.PROCEDURE, "G0438")
    dx = _captured(CodeKind.ICD10, "K62.5")
    out, flags = DenialRiskAgent().run([em, awv, dx], denial_priors=dp)
    assert any(n.startswith("denial_prior:") for n in em.notes)
    assert not any(n.startswith("denial_prior:") for n in dx.notes)  # dx not in PSPS scope
    assert awv.denial_risk in ("medium", "high")
    assert any("G0438" in f and "denied" in f for f in flags)
    assert not any("99214: Medicare denied" in f for f in flags)     # low-rate: note only


def test_pipeline_carries_prior_into_worksheet():
    from cosmaneura_billing.corpus.demo import SAMPLE_VISIT
    result = BillingOrchestrator().process(SAMPLE_VISIT)
    em = result.biller_worksheet.em_code
    assert any(n.startswith("denial_prior:") for n in em.notes)


# ---- payer rates (TC-PUF) ----------------------------------------------------- #
def test_payer_rates_load_and_nh_present():
    pr = PayerDenialRates()
    assert pr.loaded and pr.size > 100
    nh = pr.for_state("NH")
    assert len(nh) >= 2
    assert nh[0].denial_rate >= nh[-1].denial_rate    # sorted toughest-first
    assert all(0 <= r.denial_rate <= 1 for r in nh)


def test_payer_lookup_by_name():
    pr = PayerDenialRates()
    hit = pr.lookup("Harvard Pilgrim", state="NH")
    assert hit is not None and hit.denial_rate > 0.2


# ---- NH E/M benchmark ---------------------------------------------------------- #
def test_nh_benchmark_mix_and_allowed():
    nh = StateEMBenchmark()
    assert nh.loaded and len(nh.specialties()) > 30
    mix = nh.mix()
    assert abs(sum(mix.values()) - 1.0) < 0.01
    assert mix["99214"] > mix["99213"]          # NH billed more 99214 than 99213 in 2024
    fp = nh.mix("Family Practice")
    assert fp["99214"] > 0.5
    assert nh.allowed("99214") > nh.allowed("99213")
