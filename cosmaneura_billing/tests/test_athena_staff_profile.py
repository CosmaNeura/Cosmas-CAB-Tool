"""Dormant real-practice protocol tests; all values and transport are authored."""
from dataclasses import FrozenInstanceError, replace
import io
import json
import urllib.request

import pytest

from cosmaneura_billing import athena_client as api
from cosmaneura_billing.athena_staff_contract import (
    AthenaStaffProfile, AthenaStaffScope, AthenaStaffScopeError,
)


def scope(environment="production", patient="4455"):
    profile = AthenaStaffProfile("synthetic-cab-practice", environment,
                                 "195900" if environment == "preview" else "112233", 1)
    return AthenaStaffScope(profile, "synthetic-case", "opaque-patient-reference", patient, 1)


def raw(value):
    return json.dumps(value).encode()


def test_scope_is_immutable_and_every_binding_dimension_changes_its_fingerprint():
    original = scope()
    with pytest.raises(FrozenInstanceError):
        original.source_patient_id = "4456"
    with pytest.raises(FrozenInstanceError):
        original.profile.vendor_practice_id = "223344"
    alternatives = [replace(original, **{key: value}) for key, value in (
        ("case_id", "different-case"), ("patient_ref", "different-local-patient"),
        ("source_patient_id", "4456"), ("binding_revision", 2))]
    alternatives += [replace(original, profile=replace(original.profile, **{key: value}))
        for key, value in (("server_practice_id", "different-practice"),
            ("vendor_practice_id", "223344"), ("environment", "preview"),
            ("connection_revision", 2))]
    assert len({original.fingerprint, *(v.fingerprint for v in alternatives)}) == 9
    assert original.fingerprint == scope().fingerprint
    assert "4455" not in repr(original)
    assert "112233" not in repr(original.profile)
    assert "opaque-patient-reference" not in repr(original)


@pytest.mark.parametrize("value", [True, 0, -1, "1", 2**31, None])
def test_revision_must_be_positive_bounded_integer(value):
    with pytest.raises(AthenaStaffScopeError, match="^invalid Athena staff scope$"):
        replace(scope(), binding_revision=value)
    with pytest.raises(AthenaStaffScopeError):
        replace(scope().profile, connection_revision=value)


@pytest.mark.parametrize("value", [4455, True, "0", "04455", "1/2", "1?x=2", "1" * 19, "", None])
def test_source_identifiers_are_canonical_and_cannot_change_request_shape(value):
    with pytest.raises(AthenaStaffScopeError):
        replace(scope(), source_patient_id=value)
    with pytest.raises(AthenaStaffScopeError):
        replace(scope().profile, vendor_practice_id=value)


@pytest.mark.parametrize("environment", ["Production", "prod", "https://invalid.example", "", None])
def test_environment_is_a_closed_enum(environment):
    with pytest.raises(AthenaStaffScopeError):
        replace(scope().profile, environment=environment)


def test_production_contract_uses_only_official_origin_and_bound_practice_patient():
    bound = scope()
    assert api.build_athena_staff_read_url(bound, "patient") == (
        "https://api.platform.athenahealth.com/v1/112233/patients/4455?showfullssn=false")
    assert api.build_athena_staff_read_url(bound, "insurance") == (
        "https://api.platform.athenahealth.com/v1/112233/patients/4455/insurances?"
        "limit=5&offset=0&showcancelled=true&showfullssn=false")
    assert api.build_athena_staff_read_url(bound, "encounters", department="150", offset=15) == (
        "https://api.platform.athenahealth.com/v1/112233/chart/4455/encounters?"
        "departmentid=150&limit=5&offset=15")
    assert api.build_athena_staff_read_url(bound, "encounter", encounter="7788") == (
        "https://api.platform.athenahealth.com/v1/112233/chart/encounter/7788")
    assert api.build_athena_staff_read_url(bound, "summary", encounter="7788") == (
        "https://api.platform.athenahealth.com/v1/112233/chart/encounters/7788/summary")
    # A numeric ID matching the public cohort is still Production in this scope.
    assert "/v1/112233/patients/60181?" in api.build_athena_staff_read_url(
        replace(bound, source_patient_id="60181"), "patient")
    with pytest.raises(api.AthenaOperationUnavailable):
        api.build_athena_staff_read_url(bound, "claims")
    with pytest.raises(api.AthenaSchemaError):
        api.build_athena_staff_read_url(bound, "encounters", department="1&limit=1000")
    with pytest.raises(api.AthenaSchemaError):
        api._staff_url("patient", "4456", scope=bound)


def test_explicit_preview_scope_cannot_expand_existing_practice_or_patient_cohort():
    bound = scope("preview", "60181")
    assert api.build_athena_staff_read_url(bound, "patient") == api._staff_url("patient", "60181")
    with pytest.raises(api.AthenaSchemaError):
        api.build_athena_staff_read_url(replace(bound, source_patient_id="4455"), "patient")
    with pytest.raises(api.AthenaSchemaError):
        api.build_athena_staff_read_url(replace(bound,
            profile=replace(bound.profile, vendor_practice_id="112233")), "patient")
    with pytest.raises(api.AthenaSchemaError):
        api._staff_url("patient", "4455")


def test_production_pure_card_decoding_keeps_raw_observations_and_expected_binding():
    bound = scope()
    payload = [{"patientid": "4455", "practiceid": "112233", "mobilephone": "2025550142"}]
    result = api.decode_athena_card_read(raw(payload), dataset="patient",
        expected_patient_id="4455", scope=bound)
    phone = next(f for f in result.records[0] if f.name == "mobile_phone")
    assert phone.value == "2025550142" and phone.wire_type == "string"
    insurance = {"patientid": "4455", "practiceid": "112233", "totalcount": 1,
                 "insurances": [{"insuranceid": "5566"}]}
    assert len(api.decode_athena_card_read(raw(insurance), dataset="insurance",
        expected_patient_id="4455", scope=bound).records) == 1
    for key, value in (("patientid", "4456"), ("practiceid", "195900")):
        with pytest.raises(api.AthenaSchemaError):
            api.decode_athena_card_read(raw([{**payload[0], key: value}]), dataset="patient",
                expected_patient_id="4455", scope=bound)
        with pytest.raises(api.AthenaSchemaError):
            api.decode_athena_card_read(raw({**insurance, key: value}), dataset="insurance",
                expected_patient_id="4455", scope=bound)
    with pytest.raises(api.AthenaSchemaError):
        api.decode_athena_card_read(raw(payload), dataset="patient", expected_patient_id="4455")
    with pytest.raises(api.AthenaSchemaError):
        api.decode_athena_card_read(raw(payload), dataset="patient", expected_patient_id="4456", scope=bound)


def test_production_metadata_preserves_source_department_and_rejects_other_bindings():
    bound = scope()
    row = {"encounterid": 7788, "patientid": 4455, "practiceid": 112233, "departmentid": 150}
    result = api._encounter_row(row, "4455", "1", "7788", scope=bound)
    assert result["departmentid"] == {"state": "present", "value": "150"}
    for key, value in (("patientid", 4456), ("practiceid", 195900), ("encounterid", 7789)):
        with pytest.raises(api.AthenaSchemaError):
            api._encounter_row({**row, key: value}, "4455", "1", "7788", scope=bound)
    without_department = {k: v for k, v in row.items() if k != "departmentid"}
    assert api._encounter_row(without_department, "4455", "1", scope=bound)["departmentid"] == {
        "state": "absent", "value": None}


@pytest.mark.parametrize("injected", [False, True])
def test_production_transport_is_unavailable_even_with_matching_tag_or_injected_io(injected):
    attempted = []
    def forbidden(*args, **kwargs):
        attempted.append("I/O")
        raise AssertionError("must not invoke a provider or transport")
    kwargs = {"token_provider": forbidden, "urlopen": forbidden} if injected else {
        "client_id": "authored-public-id", "client_secret": "authored-placeholder"}
    with pytest.raises(api.AthenaOperationUnavailable):
        api.AthenaStaffReadClient(scope=scope(), credential_environment="production", **kwargs)
    assert attempted == []


@pytest.mark.parametrize("bound,tag", [(scope(), "preview"), (scope(), None),
    (scope("preview", "60181"), "production"), (scope("preview", "60181"), None), (None, "production")])
def test_credential_environment_mismatch_rejects_before_any_credential_or_token_use(bound, tag):
    attempted = []
    with pytest.raises(api.AthenaAuthenticationError):
        api.AthenaStaffReadClient(scope=bound, credential_environment=tag,
            token_provider=lambda: attempted.append("token"), urlopen=lambda *_: attempted.append("wire"))
    assert attempted == []


class Response(io.BytesIO):
    status = 200
    headers = {"Content-Type": "application/json"}
    def __init__(self, value, url):
        super().__init__(raw(value))
        self.url = url
    def geturl(self): return self.url


def preview_client(bound=None):
    calls = []
    def send(request, timeout):
        assert 0 < timeout <= 5
        calls.append((request.get_method(), request.full_url))
        if request.get_method() == "POST":
            payload = {"access_token": "authored-token", "token_type": "Bearer", "expires_in": 300,
                       "scope": api.ATHENA_PREVIEW_SCOPE}
        elif "/insurances?" in request.full_url:
            payload = {"insurances": [], "totalcount": 0}
        elif "/patients/" in request.full_url:
            payload = [{"patientid": "60181", "practiceid": "195900", "mobilephone": "2025550142"}]
        elif request.full_url.endswith("/summary"):
            payload = {"summaryhtml": "<p>Synthetic source note.</p>"}
        else:
            row = {"encounterid": 7788, "patientid": 60181, "practiceid": 195900,
                   "departmentid": 150, "status": "OPEN"}
            payload = {"encounters": [row], "totalcount": 1} if "/encounters?" in request.full_url else [row]
        return Response(payload, request.full_url)
    return api.AthenaStaffReadClient(client_id="authored-id", client_secret="authored-placeholder",
        urlopen=send, scope=bound, credential_environment="preview" if bound else None), calls


def test_legacy_and_explicit_preview_reads_have_identical_wire_and_decoded_results():
    legacy, old_calls = preview_client()
    scoped, scoped_calls = preview_client(scope("preview", "60181"))
    assert legacy.read_card("60181") == scoped.read_card("60181")
    assert legacy.read_billing("60181", "1", "7788") == scoped.read_billing("60181", "1", "7788")
    assert old_calls == scoped_calls
    assert all(url.startswith(api.ATHENA_PREVIEW_ORIGIN + "/") for _, url in old_calls)


def test_preview_scope_rejects_other_patient_or_changed_binding_before_token():
    client, calls = preview_client(scope("preview", "60181"))
    with pytest.raises(api.AthenaSchemaError):
        client.read_card("60178")
    assert calls == []
    client._scope = replace(client._scope, case_id="changed-case")
    with pytest.raises(api.AthenaSchemaError):
        client.read_card("60181")
    assert calls == []


def test_direct_wire_cannot_bypass_closed_production_transport():
    client, calls = preview_client()
    request = urllib.request.Request(api.ATHENA_PRODUCTION_ORIGIN + "/oauth2/v1/token", method="POST")
    with pytest.raises(api.AthenaOperationUnavailable):
        client._wire(request, client._monotonic() + 25)
    assert calls == []
