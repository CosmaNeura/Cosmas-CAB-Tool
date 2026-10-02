"""Summary identity must agree with the authenticated, server-owned practice."""
import io
import json
import time

import pytest

from cosmaneura_billing import athena_admission as admission
from cosmaneura_billing.athena_client import (
    ATHENA_PREVIEW_SCOPE, AthenaAccessToken, AthenaSchemaError, AthenaStaffReadClient,
)
from cosmaneura_billing.athena_staff_contract import AthenaStaffProfile, AthenaStaffScope


@pytest.mark.parametrize("environment", [None, "preview", "production"])
@pytest.mark.parametrize("variant", ["absent", "string", "integer", "wrong", "boolean", "float", "zero_prefix", "null"])
def test_summary_practice_is_optional_but_must_match_when_supplied(environment, variant):
    practice = "112233" if environment == "production" else "195900"
    patient = "4455" if environment == "production" else "60178"
    row = {"patientid": patient, "practiceid": practice, "encounterid": "88001", "departmentid": "1"}
    summary = {"patientid": patient, "encounterid": "88001", "summaryhtml": "<p>Authored synthetic note.</p>"}
    values = {"string": practice, "integer": int(practice), "wrong": "999999",
              "boolean": True, "float": float(practice), "zero_prefix": "0" + practice, "null": None}
    if variant != "absent":
        summary["practiceid"] = values[variant]
    responses = [{"encounters": [row], "totalcount": 1}, row, summary, row]
    calls = []
    def send(request, timeout):
        calls.append(request)
        response = io.BytesIO(json.dumps(responses.pop(0)).encode())
        response.status, response.headers = 200, {"Content-Type": "application/json"}
        response.geturl = lambda: request.full_url
        return response
    kwargs = {}
    if environment is not None:
        bound = AthenaStaffScope(AthenaStaffProfile("synthetic-cab", environment, practice, 1),
                                 "synthetic-case", "synthetic-patient", patient, 1)
        kwargs.update(scope=bound, credential_environment=environment)
        if environment == "production":
            # Authored lease exercises the scoped protocol only: no DB resolver,
            # real admission, credential, token exchange or network is invoked.
            proof = {"authored": "synthetic-test-only"}
            kwargs["read_lease"] = admission.AthenaReadLease(admission._SEAL, bound, proof, lambda: proof)
    client = AthenaStaffReadClient(token_provider=lambda: AthenaAccessToken(
        "authored-token", ATHENA_PREVIEW_SCOPE, time.time() + 300), urlopen=send, **kwargs)
    if variant in {"absent", "string", "integer"}:
        result = client.read_billing(patient, "1", "88001")
        assert result["note"]["plainText"] == "Authored synthetic note."
        assert result["readyForHeader"] is False
        assert result["note"]["signatureInterpretation"] == "unverified"
        assert len(calls) == 4
    else:
        with pytest.raises(AthenaSchemaError):
            client.read_billing(patient, "1", "88001")
        assert len(calls) == 3
