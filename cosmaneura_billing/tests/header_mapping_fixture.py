"""Authored offline mapping source; no demo host, data admission or gold codes."""
from uuid import uuid4

from cosmaneura_billing.llm import MockLLM
from cosmaneura_billing.schemas import VisitBundle

ENCOUNTER = "SYN-HUMAN-MAPPING"
QUOTE = "Migraine without aura, not intractable, without status migrainosus."
VISIT = VisitBundle(ENCOUNTER, 42, "F", "office_established", "11", signed_note=QUOTE
    + " I reviewed headache frequency and discussed migraine treatment options.")


class MappingExtraction(MockLLM):
    def complete_json(self, **kwargs):
        assert QUOTE in kwargs["user"]
        if "extract_clinical_facts" in kwargs["user"]:
            # The documented problem marks this authored mock case as supported;
            # no E/M-supporting quote is supplied, so E/M remains held.
            return {"chief_complaint": "Synthetic migraine review",
                    "problems": [{"name": "Migraine"}],
                    "data_reviewed": [], "risk": {}, "total_time_minutes": None,
                    "total_time_quote": ""}
        return {"diagnoses": [{"concept": "episodic migraine disorder",
                               "quote": QUOTE, "primary": True}]}


def generate(client):
    detail = client.post("/api/encounters/detail", json={"encounterId": ENCOUNTER})
    assert detail.status_code == 200, detail.text
    response = client.post("/api/encounters/header/generate", json={
        "encounterId": ENCOUNTER, "expectedSource": detail.json()["headerSource"],
        "operationId": str(uuid4())})
    assert response.status_code == 200, response.text
    return response.json()["view"]


def edit_intent(view, **extra):
    from cosmaneura_billing.billing_header import create_draft
    return {"encounterId": ENCOUNTER, "expectedSource": view["header"]["expectedSource"],
        "headerId": view["header"]["headerId"], "expectedRevision": view["revisionNumber"],
        "operationId": str(uuid4()), "draft": create_draft(view["header"], view["revision"]),
        "reason": "Source and catalog reviewed", **extra}


def request_for(client, view):
    object.__setattr__(client.app.state.settings, "development_roles", ("Coder",))
    response = client.post("/api/encounters/coder-view", json={"encounterId": ENCOUNTER})
    assert response.status_code == 200, response.text
    engine = response.json()
    assert not engine["em"] and not engine["dx"]
    return {"encounterId": ENCOUNTER, "expectedSource": view["header"]["expectedSource"],
        "headerId": view["header"]["headerId"], "expectedRevision": view["revisionNumber"],
        "acknowledged": True, "humanProposalDecisions": [], "feedback": {
            "encounter_id": ENCOUNTER, "encounter_version": engine["encounterVersion"],
            "review_id": str(uuid4()), "review_context": engine["reviewContext"],
            "final_em": None, "decisions": []}}
