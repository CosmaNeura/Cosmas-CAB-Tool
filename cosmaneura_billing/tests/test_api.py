"""The FastAPI service: every endpoint, against the frontend data contracts."""
import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

import cosmaneura_billing.api as api


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("COSMANEURA_FEEDBACK_PATH", str(tmp_path / "fb.jsonl"))
    return TestClient(api.app)


def test_health_reports_knowledge_bases(client):
    h = client.get("/api/health").json()
    assert h["status"] == "ok"
    assert h["icd10Codes"] > 1000
    assert h["encounters"] >= 1


def test_list_and_get_encounter(client):
    encs = client.get("/api/encounters").json()
    ids = [e["id"] for e in encs]
    assert "ENC-100237" in ids
    one = client.get("/api/encounters/ENC-100237").json()
    assert one["submittedEm"] == "99213"
    assert "hematochezia" in one["signedNote"].lower()


def test_unknown_encounter_404(client):
    assert client.post("/api/encounters/NOPE/billing").status_code == 404


def test_stored_billing_matches_contract(client):
    j = client.post("/api/encounters/ENC-100237/billing").json()
    assert j["encounterId"] == "ENC-100237"
    assert j["em"]["code"] == "99214"
    assert j["primary"]["code"] == "K62.5"
    em = next(c for c in j["captured"] if c["kind"] == "EM")
    assert em["correctedFrom"] == "99213"
    assert em["disposition"] == "captured"
    assert em["evidence"] and {"source", "quote"} <= set(em["evidence"][0])
    assert any(c["code"].startswith("Z80") and c["disposition"] == "rejected"
               for c in j["declined"])
    assert j["requiresSignoff"] is True
    assert all({"action", "code", "detail"} <= set(a) for a in j["audit"])
    assert any(a["action"] == "CAPTURE" and a["code"] == "99214" for a in j["audit"])
    assert all("patientBenefit" in f for f in j["feedback"])
    assert j["revenueIntegrityNote"]


def test_adhoc_billing_runs_posted_documentation(client):
    j = client.post("/api/billing", json={
        "encounterId": "ADHOC-9", "submittedEm": "99212",
        "intake": "I have had a cough for two weeks.",
        "transcript": "Provider: how long? Patient: two weeks.",
        "signedNote": "Assessment: acute cough. Plan: supportive care.",
    }).json()
    assert j["encounterId"] == "ADHOC-9"
    assert isinstance(j["captured"], list) and isinstance(j["audit"], list)


def test_coder_view_contract(client):
    cv = client.get("/api/encounters/ENC-100237/coder-view").json()
    assert cv["submittedEm"] == "99213"
    assert cv["em"]["code"] == "99214" and cv["em"]["why"]
    assert [d["code"] for d in cv["dx"]] == ["K62.5", "K59.00", "R19.7"]
    statuses = {w["code"]: w["status"] for w in cv["withheld"]}
    assert statuses["Z80.0"] == "rejected"
    assert any(s == "held" for s in statuses.values())
    assert all(w["why"] for w in cv["withheld"])


def test_feedback_roundtrip_and_summary(client):
    payload = {
        "encounter_id": "ENC-100237", "coder_id": "coder_T",
        "engine_version": "test", "submitted_em": "99213",
        "engine_em": "99214", "final_em": "99214",
        "decisions": [
            {"kind": "EM", "engine_code": "99214", "action": "accept", "final_code": "99214"},
            {"kind": "ICD10", "engine_code": None, "action": "add",
             "final_code": "Z86.010", "reason_code": "dx_missed_by_engine"},
        ],
        "time_spent_seconds": 45,
    }
    r = client.post("/api/feedback", json=payload)
    assert r.json() == {"ok": True, "stored": 1}
    s = client.get("/api/feedback/summary").json()
    assert s["encounters"] == 1
    assert s["summary"]["em_exact_agreement"] == 1.0
    assert s["tuningBacklog"][0]["reason"] == "dx_missed_by_engine"
    assert s["goldRows"] == 1


def test_feedback_summary_empty(client):
    s = client.get("/api/feedback/summary").json()
    assert s["encounters"] == 0 and s["goldRows"] == 0


def test_reviewer_rejections_influence_future_runs_for_that_reviewer(client):
    """The full loop: three rejections of K59.00 by one reviewer -> the engine
    holds it on their next run (with history), while others are unaffected."""
    reject = {"kind": "ICD10", "engine_code": "K59.00", "action": "reject",
              "final_code": None, "reason_code": "dx_not_supported_by_doc"}
    for i in range(3):
        client.post("/api/feedback", json={
            "encounter_id": f"E{i}", "coder_id": "dr_smith",
            "engine_em": "99214", "final_em": "99214", "decisions": [reject]})

    j = client.post("/api/encounters/ENC-100237/billing?user_id=dr_smith").json()
    captured = {c["code"] for c in j["captured"]}
    assert "K59.00" not in captured
    held = [c for c in j["declined"] if c["code"] == "K59.00"]
    assert held and held[0]["disposition"] == "needs_review"
    assert any(a["action"] == "HOLD" and "declined K59.00 3x" in a["detail"]
               for a in j["audit"])

    # a different reviewer still gets the base behavior
    j2 = client.post("/api/encounters/ENC-100237/billing?user_id=dr_jones").json()
    assert "K59.00" in {c["code"] for c in j2["captured"]}

    # and the coder view surfaces the learned history
    cv = client.get("/api/encounters/ENC-100237/coder-view?user_id=dr_smith").json()
    row = next(w for w in cv["withheld"] if w["code"] == "K59.00")
    assert "declined K59.00 3x" in row["history"]
