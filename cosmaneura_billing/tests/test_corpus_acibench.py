"""ACI-Bench / MTS-Dialog corpus loaders and the multi-encounter harness."""
from cosmaneura_billing.corpus.acibench import load_acibench, load_mts_dialog
from cosmaneura_billing.orchestrator import BillingOrchestrator


def test_acibench_train_loads_full_visits():
    bundles = load_acibench("train")
    assert len(bundles) >= 60
    for b in bundles[:5]:
        assert b.transcript.strip() and b.signed_note.strip()
        assert b.encounter_id.startswith("aci-")


def test_acibench_valid_split():
    assert len(load_acibench("valid")) >= 15


def test_mts_dialog_sections():
    rows = load_mts_dialog()
    assert len(rows) >= 1000
    assert rows[0]["section_header"]
    assert rows[0]["dialogue"].strip()


def test_pipeline_runs_on_corpus_encounters_offline():
    """Plumbing check: the full pipeline must not crash on arbitrary real-shaped
    encounters, even offline where extraction yields nothing (MockLLM only
    knows the demo visit -> conservative floor, no diagnoses)."""
    orch = BillingOrchestrator()
    for b in load_acibench("train")[:3]:
        result = orch.process(b)
        ws = result.biller_worksheet
        # offline extraction is empty -> nothing may be CAPTURED above the floor
        if ws.em_code is not None:
            assert ws.em_code.code in ("99212", "99202")
        assert ws.audit_trail  # every run must still produce an audit trail
