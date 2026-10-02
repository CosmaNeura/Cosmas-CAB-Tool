"""Provider Azure proof stays source-specific; existing offline/v1 shapes survive."""
from copy import deepcopy
from dataclasses import replace
from uuid import uuid4

import pytest
from cosmaneura_billing import billing_header as h
from cosmaneura_billing.persistence import MemoryEncounterStore
from cosmaneura_billing.schemas import VisitBundle
from test_billing_header import generated


def test_provider_azure_proof_contract_and_legacy_shapes():
    encounter = VisitBundle("SYN-PROVIDER-MODEL", 30, "female", "office_established", "11",
                            signed_note="Headache documented in this wholly synthetic note.")
    stored = replace(MemoryEncounterStore([encounter]).get(encounter.encounter_id),
                     source="provider_completed_note")
    offline = generated(stored)
    h.validate_generated(offline, stored)
    value = deepcopy(offline)
    value["generation"].update(inferenceMode="azure_openai", modelInputProof={
        "schemaVersion": "cab-model-input-proof.v2", "policyId": str(uuid4()),
        "policySha256": "a" * 64, "sourceSha256": h.digest(h.source_snapshot(stored)),
        "serviceSha256": "b" * 64, "expiresAt": "2027-01-01T00:00:00Z",
        "sourceOperationId": str(uuid4()), "sourceAdmissionSha256": "c" * 64,
    })
    h.validate_generated(value, stored)
    for key, invalid in [("sourceOperationId", "invalid"), ("sourceAdmissionSha256", "invalid"),
                         ("sourceSha256", "d" * 64), ("schemaVersion", "cab-model-input-proof.v1")]:
        candidate = deepcopy(value)
        candidate["generation"]["modelInputProof"][key] = invalid
        with pytest.raises(h.BillingHeaderError):
            h.validate_generated(candidate, stored)
    missing = deepcopy(value)
    del missing["generation"]["modelInputProof"]
    with pytest.raises(h.BillingHeaderError):
        h.validate_generated(missing, stored)
    athena = replace(stored, source="athena_staff_review_operational")
    legacy = deepcopy(value)
    proof = legacy["generation"]["modelInputProof"]
    proof.update(schemaVersion="cab-model-input-proof.v1", sourceSha256=h.digest(h.source_snapshot(athena)))
    del proof["sourceOperationId"], proof["sourceAdmissionSha256"]
    h.validate_generated(legacy, athena)
