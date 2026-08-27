"""The FastAPI service: every endpoint, against the frontend data contracts."""
import base64
import json
from dataclasses import replace

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

import cosmaneura_billing.api as api
from cosmaneura_billing.auth import AuthPrincipal
from cosmaneura_billing.persistence import (
    ATHENA_PREVIEW_SYNTHETIC_SOURCE,
    StoredEncounter,
)
from cosmaneura_billing.settings import Settings


TENANT_ID = "11111111-1111-4111-8111-111111111111"
SMITH_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
JONES_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def _principal_header(user_id: str, *roles: str) -> dict[str, str]:
    payload = {
        "auth_typ": "aad",
        "claims": [
            {"typ": "oid", "val": user_id},
            {"typ": "tid", "val": TENANT_ID},
            *({"typ": "roles", "val": role} for role in roles),
        ],
    }
    encoded = base64.b64encode(json.dumps(payload).encode()).decode()
    return {"X-MS-CLIENT-PRINCIPAL": encoded}


def _review_context(client, headers: dict[str, str] | None = None) -> str:
    response = client.post(
        "/api/encounters/coder-view", json={"encounterId": "ENC-100237"},
        headers=headers or {},
    )
    assert response.status_code == 200
    return response.json()["reviewContext"]


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("COSMANEURA_FEEDBACK_PATH", str(tmp_path / "fb.jsonl"))
    return TestClient(api.create_app(
        settings=api.get_settings(), frontend_dist=tmp_path / "missing-frontend",
    ))


def test_health_reports_knowledge_bases(client):
    h = client.get("/api/health").json()
    assert h["status"] == "ok"
    assert h["icd10Codes"] > 1000
    assert h["encounters"] >= 1
    # cptStatus always explains the descriptor state, loaded or not
    assert h["cptStatus"]
    if h["cptDescriptors"] == 0:
        assert "descriptors loaded" not in h["cptStatus"]
    else:
        assert "descriptors loaded" in h["cptStatus"]


def test_cpt_status_lines(monkeypatch):
    for v in ("AMA_TOKEN_URL", "AMA_BASE_URL", "AMA_CONSUMER_KEY", "AMA_CONSUMER_SECRET"):
        monkeypatch.delenv(v, raising=False)

    class FakeCPT:
        loaded, size, source, fetch_error = False, 0, None, None

    unlicensed = FakeCPT()
    assert "no CPT license configured" in api._cpt_status(unlicensed)

    failed = FakeCPT()
    failed.fetch_error = "HTTPError: patient-Jane-Doe bearer-token-secret"
    status = api._cpt_status(failed)
    assert status == "CPT auto-fetch failed; generic labels in use"
    assert "Jane" not in status
    assert "bearer-token-secret" not in status

    loaded = FakeCPT()
    loaded.loaded, loaded.size, loaded.source = True, 10988, "/cache/SHORTU.txt"
    assert api._cpt_status(loaded) == \
        "10988 official descriptors loaded from /cache/SHORTU.txt"


def test_health_does_not_expose_cpt_fetch_exception_text(client):
    secret = "patient-Jane-Doe bearer-token-secret"
    # TestClient calls outside its lifespan initialize the app-local
    # orchestrator on the first request.
    assert client.get("/api/health").status_code == 200
    cpt = client.app.state.orchestrator.cpt
    cpt.fetch_error = f"HTTPError: {secret}"

    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json()["cptStatus"] == \
        "CPT auto-fetch failed; generic labels in use"
    assert secret not in response.text


def test_list_and_get_encounter(client):
    encs = client.get("/api/encounters").json()
    ids = [e["id"] for e in encs]
    assert "ENC-100237" in ids
    assert all("preview" not in encounter for encounter in encs)
    one = client.post(
        "/api/encounters/detail", json={"encounterId": "ENC-100237"},
    ).json()
    assert one["submittedEm"] == "99213"
    assert "hematochezia" in one["signedNote"].lower()
    assert one["encounterVersion"] == 1


def test_provider_detail_and_billing_tab_propagate_server_encounter_version(client):
    store = client.app.state.encounter_store
    original = store.get("ENC-100237")
    current = store.upsert(replace(
        original.bundle,
        patient_age=original.bundle.patient_age + 1,
    ))
    assert current.version == 2

    detail = client.post(
        "/api/encounters/detail", json={"encounterId": "ENC-100237"},
    )
    billing_tab = client.post(
        "/api/encounters/billing-tab", json={"encounterId": "ENC-100237"},
    )

    assert detail.status_code == 200
    assert billing_tab.status_code == 200
    assert detail.json()["encounterVersion"] == current.version
    assert billing_tab.json()["encounterVersion"] == current.version


def test_read_only_data_class_comes_only_from_persisted_source(client):
    store = client.app.state.encounter_store
    stored = store.get("ENC-100237")
    store.upsert(stored.bundle, source="synthetic_demo_release_candidate")
    misleading_id = replace(stored.bundle, encounter_id="SYN-RC-NOT-PROVENANCE")
    store.upsert(misleading_id, source="explicit_import")

    listed = {row["id"]: row for row in client.get("/api/encounters").json()}
    assert listed["ENC-100237"]["dataClass"] == \
        "synthetic_demo_release_candidate"
    assert listed["ENC-100237"]["readOnly"] is True
    assert listed["SYN-RC-NOT-PROVENANCE"]["dataClass"] == "operational"
    assert listed["SYN-RC-NOT-PROVENANCE"]["readOnly"] is False

    detail = client.post(
        "/api/encounters/detail", json={"encounterId": "ENC-100237"},
    ).json()
    billing = client.post(
        "/api/encounters/billing", json={"encounterId": "ENC-100237"},
    ).json()
    coder = client.post(
        "/api/encounters/coder-view", json={"encounterId": "ENC-100237"},
    ).json()
    assert all(
        response["dataClass"] == "synthetic_demo_release_candidate"
        and response["readOnly"] is True
        for response in (detail, billing, coder)
    )

    athena_policy = api._encounter_data_policy(StoredEncounter(
        resource_id="synthetic-resource-id",
        bundle=stored.bundle,
        source=ATHENA_PREVIEW_SYNTHETIC_SOURCE,
    ))
    unknown_policy = api._encounter_data_policy(StoredEncounter(
        resource_id="operational-resource-id",
        bundle=stored.bundle,
        source="future_unknown_source",
    ))
    assert athena_policy == {
        "dataClass": ATHENA_PREVIEW_SYNTHETIC_SOURCE,
        "readOnly": True,
    }
    assert unknown_policy == {"dataClass": "operational", "readOnly": False}


def test_unknown_encounter_404(client):
    assert client.post(
        "/api/encounters/billing", json={"encounterId": "NOPE"},
    ).status_code == 404


def test_stored_billing_matches_contract(client):
    j = client.post(
        "/api/encounters/billing", json={"encounterId": "ENC-100237"},
    ).json()
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
    cv = client.post(
        "/api/encounters/coder-view", json={"encounterId": "ENC-100237"},
    ).json()
    assert cv["encounterVersion"] == 1
    assert cv["reviewContext"]
    assert cv["submittedEm"] == "99213"
    assert cv["em"]["code"] == "99214" and cv["em"]["why"]
    assert [d["code"] for d in cv["dx"]] == ["K62.5", "K59.00", "R19.7"]
    statuses = {w["code"]: w["status"] for w in cv["withheld"]}
    assert statuses["Z80.0"] == "rejected"
    assert any(s == "held" for s in statuses.values())
    assert all(w["why"] for w in cv["withheld"])


def test_feedback_roundtrip_and_summary(client):
    payload = {
        "encounter_id": "ENC-100237",
        "review_id": "10000000-0000-4000-8000-000000000001",
        "review_context": _review_context(client),
        "encounter_version": 1,
        "submitted_em": "99213",
        "engine_em": "99214", "final_em": "99214",
        "decisions": [
            {"kind": "EM", "engine_code": "99214", "action": "accept", "final_code": "99214"},
            {"kind": "ICD10", "engine_code": None, "action": "add",
             "final_code": "Z86.010", "reason_code": "dx_missed_by_engine"},
        ],
        "time_spent_seconds": 45,
    }
    r = client.post("/api/feedback", json=payload)
    assert r.json() == {
        "ok": True,
        "reviewId": "10000000-0000-4000-8000-000000000001",
        "replayed": False,
    }
    replay = client.post("/api/feedback", json=payload)
    assert replay.json()["replayed"] is True
    stored = client.app.state.encounter_store.get("ENC-100237")
    client.app.state.encounter_store.upsert(replace(
        stored.bundle,
        signed_note=stored.bundle.signed_note + "\nSynthetic amendment.",
    ))
    replay_after_update = client.post("/api/feedback", json=payload)
    assert replay_after_update.json()["replayed"] is True
    conflict = client.post(
        "/api/feedback", json={**payload, "time_spent_seconds": 46},
    )
    assert conflict.status_code == 409
    s = client.get("/api/feedback/summary").json()
    assert s["encounters"] == 1
    assert s["summary"]["em_exact_agreement"] == 1.0
    assert s["tuningBacklog"][0]["reason"] == "dx_missed_by_engine"
    assert s["goldRows"] == 1


def test_feedback_rejects_a_stale_encounter_version(client):
    stored = client.app.state.encounter_store.get("ENC-100237")
    review_context = _review_context(client)
    unchanged = client.app.state.encounter_store.upsert(stored.bundle)
    assert unchanged.version == stored.version
    client.app.state.encounter_store.upsert(replace(
        stored.bundle,
        signed_note=stored.bundle.signed_note + "\nSynthetic amendment.",
    ))

    response = client.post("/api/feedback", json={
        "review_id": "10000000-0000-4000-8000-000000000099",
        "review_context": review_context,
        "encounter_id": "ENC-100237",
        "encounter_version": stored.version,
        "engine_em": "99214",
        "final_em": "99214",
        "decisions": [],
    })

    assert response.status_code == 409
    assert response.json()["detail"] == "encounter changed; reload the review"


def test_feedback_rejects_empty_optional_em_code(client):
    response = client.post("/api/feedback", json={
        "review_id": "10000000-0000-4000-8000-000000000098",
        "review_context": _review_context(client),
        "encounter_id": "ENC-100237",
        "encounter_version": 1,
        "engine_em": "",
        "decisions": [],
    })

    assert response.status_code == 422


def test_feedback_keeps_the_engine_version_that_issued_the_review(client):
    stored = client.app.state.encounter_store.get("ENC-100237")
    principal = AuthPrincipal(
        user_id="local-developer",
        tenant_id="development",
        practice_id="development",
        roles=frozenset({"Admin"}),
    )
    issuing_version = "synthetic-previous-revision"
    payload = {
        "review_id": "10000000-0000-4000-8000-000000000097",
        "review_context": api._issue_review_context(
            stored, principal, engine_version=issuing_version,
        ),
        "encounter_id": "ENC-100237",
        "encounter_version": stored.version,
        "engine_em": "99214",
        "final_em": "99214",
        "decisions": [],
    }

    first = client.post("/api/feedback", json=payload)
    retry = client.post("/api/feedback", json=payload)

    assert first.status_code == retry.status_code == 200
    assert retry.json()["replayed"] is True
    assert client.app.state.feedback_store.load()[0].engine_version == issuing_version


def test_feedback_summary_empty(client):
    s = client.get("/api/feedback/summary").json()
    assert s["encounters"] == 0 and s["goldRows"] == 0


def test_billing_carries_denial_prior(client):
    j = client.post(
        "/api/encounters/billing", json={"encounterId": "ENC-100237"},
    ).json()
    em = next(c for c in j["captured"] if c["kind"] == "EM")
    assert 0.0 < em["denialPrior"] < 0.10


def test_em_benchmarks_endpoint(client):
    b = client.post(
        "/api/benchmarks/em", json={"specialty": "Family Practice"},
    ).json()
    assert b["state"] == "NH"
    assert abs(sum(b["stateMix"].values()) - 1.0) < 0.01
    assert b["stateAllowed"]["99214"] > b["stateAllowed"]["99213"]


def test_payer_denials_endpoint(client):
    p = client.post(
        "/api/benchmarks/payer-denials", json={"state": "NH"},
    ).json()
    assert p["state"] == "NH" and len(p["issuers"]) >= 2
    assert p["issuers"][0]["denialRate"] >= p["issuers"][-1]["denialRate"]


def test_reviewer_rejections_influence_future_runs_for_that_reviewer(tmp_path):
    """The full loop: three rejections of K59.00 by one reviewer -> the engine
    holds it on their next run (with history), while others are unaffected."""
    settings = Settings(
        environment="test",
        auth_mode="container_apps",
        entra_tenant_id=TENANT_ID,
        practice_id="practice-test",
        feedback_path=str(tmp_path / "feedback.jsonl"),
    )
    client = TestClient(api.create_app(
        settings=settings, frontend_dist=tmp_path / "missing-frontend",
    ))
    smith = _principal_header(SMITH_ID, "Coder")
    jones = _principal_header(JONES_ID, "Coder")
    smith_review_context = _review_context(client, smith)
    reject = {"kind": "ICD10", "engine_code": "K59.00", "action": "reject",
              "final_code": None, "reason_code": "dx_not_supported_by_doc"}
    for i in range(3):
        response = client.post("/api/feedback", headers=smith, json={
            "review_id": f"10000000-0000-4000-8000-{i:012d}",
            "review_context": smith_review_context,
            "encounter_id": "ENC-100237", "encounter_version": 1,
            "engine_em": "99214",
            "final_em": "99214", "decisions": [reject]})
        assert response.status_code == 200

    j = client.post(
        "/api/encounters/billing", json={"encounterId": "ENC-100237"},
        headers=smith,
    ).json()
    captured = {c["code"] for c in j["captured"]}
    assert "K59.00" not in captured
    held = [c for c in j["declined"] if c["code"] == "K59.00"]
    assert held and held[0]["disposition"] == "needs_review"
    assert any(a["action"] == "HOLD" and "declined K59.00 3x" in a["detail"]
               for a in j["audit"])

    # a different reviewer still gets the base behavior
    j2 = client.post(
        "/api/encounters/billing", json={"encounterId": "ENC-100237"},
        headers=jones,
    ).json()
    assert "K59.00" in {c["code"] for c in j2["captured"]}

    # and the coder view surfaces the learned history
    cv = client.post(
        "/api/encounters/coder-view", json={"encounterId": "ENC-100237"},
        headers=smith,
    ).json()
    assert cv["coderId"] == SMITH_ID
    row = next(w for w in cv["withheld"] if w["code"] == "K59.00")
    assert "declined K59.00 3x" in row["history"]
