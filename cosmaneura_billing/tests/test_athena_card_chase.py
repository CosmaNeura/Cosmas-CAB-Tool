"""Invented Athena-shaped observations; no credential, provider or patient I/O."""
from __future__ import annotations

import hashlib
import json
import socket
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace

import pytest

import cosmaneura_billing.athena_client as client
import cosmaneura_billing.athena_card_chase as mapper
from cosmaneura_billing.athena_staff_contract import AthenaStaffProfile, AthenaStaffScope
from cosmaneura_billing.athena_card_chase import (
    MAPPING_VERSION,
    SCHEMA_VERSION,
    AthenaCardChaseError,
    CardChaseReadContext,
    map_athena_card_chase,
)

CONTEXT = CardChaseReadContext("synthetic-practice", "60178", "invented-batch-1",
                              "2026-09-29T09:30:00-04:00")
PATIENT = [{
    "patientid": "60178", "firstname": "CASEY", "lastname": "EXAMPLE",
    "dob": "01/02/2001", "departmentid": "001", "mobilephone": "+12025550123",
    "email": "casey@example.test", "consenttotext": "true", "consenttocall": "Y",
    "contactpreference_billing_sms": "true",
    "contactpreference_billing_phone": "true", "contactpreference_billing_email": "true",
}]
POLICY = {
    "insuranceid": "00077", "insurancepackageid": "00123", "sequencenumber": 1,
    "insurancepackagepayerid": "00123", "ircid": "00123",
    "insuranceidnumber": "00012345", "insuredidnumber": "00012345", "policynumber": "000987",
    "ircname": "INVENTED CATEGORY", "insuranceplanname": "SYNTHETIC PLAN",
    "insuranceplandisplayname": "SYNTHETIC DISPLAY", "insurancepayername": "SYNTHETIC PAYER",
    "insurancepolicyholder": "TAYLOR EXAMPLE", "insurancepolicyholderfirstname": "TAYLOR",
    "insurancepolicyholderlastname": "EXAMPLE", "relationshiptoinsured": "Child",
    "relationshiptoinsuredid": "019", "issuedate": "09/01/2026",
    "expirationdate": "12/31/2026", "packageexpirationdate": "12/31/2027",
    "cancelled": False, "lastupdated": "09/20/2026 10:00:00",
    "eligibilitystatus": "Active", "eligibilitylastchecked": "09/15/2026",
    "eligibilitymessage": "INVENTED OLD SNAPSHOT", "eligibilityreason": "SYNTHETIC",
}


def encoded(value):
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


def map_result(patient=PATIENT, policies=None, *, envelope=None, context=CONTEXT):
    if envelope is None:
        policies = [deepcopy(POLICY)] if policies is None else policies
        envelope = {"insurances": policies, "totalcount": len(policies)}
    return map_athena_card_chase(context=context, patient_response=encoded(patient),
                                 insurance_response=encoded(envelope))


def fields(container):
    return {field["name"]: field for field in container["fields"]}


def test_golden_review_contract_preserves_patient_subscriber_policy_and_source():
    result = map_result().to_review_dict()
    assert set(result) == {"schemaVersion", "mappingVersion", "candidateId", "status", "source",
                           "reads", "patient", "contact", "insurance", "reviewIssues", "applied"}
    assert result["schemaVersion"] == SCHEMA_VERSION
    assert result["mappingVersion"] == MAPPING_VERSION
    assert result["status"] == "review_required"
    assert result["applied"] is False
    assert result["source"] == {
        "system": "athena_preview", "vendorPracticeId": "195900", "patientId": "60178",
        "serverPracticeId": "synthetic-practice", "batchId": "invented-batch-1",
        "observedAt": "2026-09-29T13:30:00Z", "syntheticOnly": True,
    }
    assert result["reads"]["patient"] == {
        "state": "complete", "sha256": hashlib.sha256(encoded(PATIENT)).hexdigest(),
        "recordCount": 1, "totalCount": None, "truncated": False,
    }
    assert fields(result["patient"])["birth_date"]["parsedValue"] == "2001-01-02"
    policy = result["insurance"]["policies"][0]
    pf = fields(policy)
    assert pf["member_id"] == {
        "name": "member_id", "value": "00012345", "parsedValue": None,
        "state": "present", "sourceType": "string",
        "source": {"dataset": "insurance", "pointer": "/insurances/0/insuranceidnumber",
                   "sha256": result["reads"]["insurance"]["sha256"]},
    }
    assert pf["group_number"]["value"] == "000987"
    assert pf["insured_id"]["value"] == "00012345"
    assert {pf[n]["value"] for n in ("package_id", "package_payer_id", "category_id")} == {"00123"}
    assert [pf[n]["source"]["pointer"] for n in ("package_id", "package_payer_id", "category_id")] == [
        "/insurances/0/insurancepackageid", "/insurances/0/insurancepackagepayerid", "/insurances/0/ircid",
    ]
    assert pf["subscriber_display_name"]["value"] == "TAYLOR EXAMPLE"
    assert fields(result["patient"])["first_name"]["value"] == "CASEY"
    assert policy["subscriberPatientMatch"] == "unverified"
    assert policy["subscriberState"] == "review_required"
    assert policy["sequence"] == 1 and policy["sourceIndex"] == 0
    assert policy["issues"] == []
    assert result["reviewIssues"] == []
    assert policy["accepted"] is False


def test_contact_and_consent_observations_never_become_outreach_authority():
    result = map_result().to_review_dict()["contact"]
    assert result["outreachAuthorized"] is False
    assert result["effectiveConsent"] == {"sms": "unknown", "phone": "unknown", "email": "unknown"}
    assert [v["value"] for v in result["consentObservations"]] == ["true", "Y", "true", "true", "true"]
    assert all(v["parsedValue"] is None for v in result["consentObservations"])
    assert result["candidates"][0]["formatStatus"] == "plausible"
    for contact in result["candidates"]:
        assert contact["ownership"] == "unverified"
        assert contact["selected"] is False
        assert contact["usableForOutreach"] is False
    assert result["requiredReview"] == ["recipient_identity", "channel_purpose_consent",
                                        "suppression", "direct_contract", "staff_approval"]


@pytest.mark.parametrize("flag", [False, True])
def test_numeric_insurance_ids_and_boolean_flags_preserve_observations_without_authority(flag):
    patient = deepcopy(PATIENT)
    consent_names = ("consenttotext", "consenttocall", "contactpreference_billing_sms",
                     "contactpreference_billing_phone", "contactpreference_billing_email")
    patient[0].update(dict.fromkeys(consent_names, flag))
    numeric = {"insurancepackageid": 123, "ircid": 8, "relationshiptoinsuredid": 19}
    envelope = {"insurances": [{**POLICY, **numeric}], "totalcount": 1}
    result = map_result(patient, envelope=envelope).to_review_dict()
    policy = result["insurance"]["policies"][0]
    for name, source_name in (("package_id", "insurancepackageid"), ("category_id", "ircid"),
                              ("relationship_id", "relationshiptoinsuredid")):
        field = fields(policy)[name]
        assert field == {"name": name, "state": "present", "value": numeric[source_name],
            "parsedValue": str(numeric[source_name]), "sourceType": "number",
            "source": {"dataset": "insurance", "pointer": "/insurances/0/" + source_name,
                       "sha256": hashlib.sha256(encoded(envelope)).hexdigest()}}
        assert type(field["value"]) is int
    assert policy["issues"] == []
    assert policy["currentCoverage"] == "unknown" and policy["eligibilityVerifiedByCab"] is False
    assert policy["accepted"] is False and policy["subscriberPatientMatch"] == "unverified"
    for observation, source_name in zip(result["contact"]["consentObservations"], consent_names):
        assert observation["state"] == "present" and observation["sourceType"] == "boolean"
        assert observation["value"] is flag and observation["parsedValue"] is flag
        assert observation["source"] == {"dataset": "patient", "pointer": "/0/" + source_name,
                                         "sha256": hashlib.sha256(encoded(patient)).hexdigest()}
    assert result["contact"]["effectiveConsent"] == {"sms": "unknown", "phone": "unknown", "email": "unknown"}
    assert result["contact"]["outreachAuthorized"] is False
    assert all(c["usableForOutreach"] is False for c in result["contact"]["candidates"])


@pytest.mark.parametrize("name", ["insurancepackageid", "ircid", "relationshiptoinsuredid"])
@pytest.mark.parametrize("value,expected", [(0, "0"), (2**53 - 1, "9007199254740991"),
                                            ("00123", None)])
def test_numeric_id_bounds_and_string_leading_zeros_remain_exact(name, value, expected):
    policy = map_result(policies=[{**POLICY, name: value}]).to_review_dict()["insurance"]["policies"][0]
    field = next(f for f in policy["fields"] if f["source"]["pointer"].endswith("/" + name))
    assert field["state"] == "present" and field["value"] == value
    assert type(field["value"]) is type(value) and field["parsedValue"] == expected


@pytest.mark.parametrize("name", ["insurancepackageid", "ircid", "relationshiptoinsuredid"])
@pytest.mark.parametrize("value", [True, False, -1, 2**53, 1.0, 1.5])
def test_numeric_id_parser_rejects_boolean_fractional_negative_and_inexact_values(name, value):
    policy = map_result(policies=[{**POLICY, name: value}]).to_review_dict()["insurance"]["policies"][0]
    field = next(f for f in policy["fields"] if f["source"]["pointer"].endswith("/" + name))
    assert field["state"] == "invalid" and field["parsedValue"] is None
    assert field["sourceType"] == ("boolean" if type(value) is bool else "number")


@pytest.mark.parametrize("phone", ["+12025550123", "+12025550124"])
def test_two_phone_fields_remain_ambiguous_even_when_their_values_match(phone):
    patient = deepcopy(PATIENT)
    patient[0]["homephone"] = phone
    contact = map_result(patient).to_review_dict()["contact"]
    assert contact["phoneSelection"] == "ambiguous"
    assert len(contact["candidates"]) == 3
    assert contact["candidates"][0]["selected"] is False


@pytest.mark.parametrize("value,status", [
    ("DECLINED", "unrecognized_or_declined"), ("none", "unrecognized_or_declined"),
    ("202-555-0123", "country_confirmation_required"), ("+12025550123 ext 8", "unrecognized"),
    ("2025550123", "country_confirmation_required"), (True, "invalid"), (None, "null"), ("  ", "empty"),
    ("(202) 555-0123", "country_confirmation_required"), ("2025550123 ext 8", "unrecognized"),
    ("12025550123", "unrecognized"), ("0000000000", "unrecognized"),
    ("2021550123", "unrecognized"), ("２０２５５５０１２３", "unrecognized"),
])
def test_phone_parser_does_not_guess_country_or_sentinel(value, status):
    patient = deepcopy(PATIENT)
    patient[0]["mobilephone"] = value
    assert map_result(patient).to_review_dict()["contact"]["candidates"][0]["formatStatus"] == status


def test_national_phone_retains_exact_source_without_country_selection_or_consent():
    patient = deepcopy(PATIENT)
    patient[0]["mobilephone"] = "(202) 555-0123"
    patient[0]["homephone"] = "2025550124"
    contact = map_result(patient).to_review_dict()["contact"]
    assert contact["phoneSelection"] == "ambiguous"
    mobile = contact["candidates"][0]
    assert mobile["field"]["value"] == "(202) 555-0123" and mobile["field"]["parsedValue"] is None
    assert mobile["formatStatus"] == "country_confirmation_required"
    assert mobile["selected"] is False and mobile["usableForOutreach"] is False
    assert mobile["ownership"] == "unverified" and contact["effectiveConsent"]["sms"] == "unknown"


def test_non_patient_phone_and_sensitive_unknown_fields_are_excluded():
    patient = deepcopy(PATIENT)
    del patient[0]["mobilephone"]
    patient[0].update({"contactmobilephone": "+12025550999", "contacthomephone": "+12025550888",
                       "workphone": "+12025550777", "ssn": "SYNTHETIC-NEVER-PROJECT",
                       "address1": "INVENTED-NEVER-PROJECT", "sms_authorized": True})
    result = map_result(patient).to_review_dict()
    assert result["contact"]["candidates"][0]["formatStatus"] == "absent"
    dump = json.dumps(result)
    assert "NEVER-PROJECT" not in dump and "+12025550999" not in dump
    assert "sms_authorized" not in dump and "contactmobilephone" not in dump


def test_absent_null_empty_and_invalid_remain_distinct_without_defaults():
    patient = deepcopy(PATIENT)
    patient[0].update({"consenttotext": None, "consenttocall": "", "contactpreference_billing_sms": 1})
    del patient[0]["contactpreference_billing_phone"]
    pf = fields(map_result(patient).to_review_dict()["patient"])
    assert [pf[n]["state"] for n in ("text_consent", "call_consent", "billing_sms_preference",
                                     "billing_phone_preference")] == ["null", "empty", "invalid", "absent"]
    policy = map_result(policies=[{"insurancepackageid": "00"}]).to_review_dict()["insurance"]["policies"][0]
    assert fields(policy)["relationship"]["state"] == "absent"
    assert fields(policy)["relationship"]["value"] is None
    assert policy["subscriberPatientMatch"] == "unverified"
    assert policy["sequence"] is None
    assert "policy_record_id_unknown" in policy["issues"]


def test_membership_conflict_and_group_only_never_choose_a_member_id():
    conflicting = {**POLICY, "insuredidnumber": "DIFFERENT"}
    group_only = {"insurancepackageid": "009", "policynumber": "000777", "insuredidnumber": "000888"}
    policies = map_result(policies=[conflicting, group_only]).to_review_dict()["insurance"]["policies"]
    assert policies[0]["memberIdState"] == "conflicting"
    assert "member_identifiers_conflict" in policies[0]["issues"]
    assert fields(policies[0])["insured_id"]["value"] == "DIFFERENT"
    assert policies[1]["memberIdState"] == "unknown"
    assert fields(policies[1])["member_id"]["value"] is None


@pytest.mark.parametrize("update,issue", [
    ({"insurancepolicyholder": "OTHER NAME"}, "subscriber_names_conflict"),
    ({"insurancepolicyholderlastname": ""}, "subscriber_name_incomplete"),
])
def test_subscriber_ambiguity_is_preserved_and_never_resolved(update, issue):
    policy = map_result(policies=[{**POLICY, **update}]).to_review_dict()["insurance"]["policies"][0]
    assert policy["subscriberState"] == "ambiguous"
    assert issue in policy["issues"]
    assert policy["subscriberPatientMatch"] == "unverified"


def test_policy_sequence_collisions_and_multiple_records_are_not_collapsed_or_sorted():
    policies = [{**POLICY, "sequencenumber": 2}, {**POLICY, "sequencenumber": "1"},
                {**POLICY, "insuranceid": "00088", "sequencenumber": 1}]
    result = map_result(policies=policies).to_review_dict()["insurance"]["policies"]
    assert [p["sequence"] for p in result] == [2, 1, 1]
    assert [p["sourceIndex"] for p in result] == [0, 1, 2]
    assert len({p["candidateId"] for p in result}) == 3
    assert "duplicate_policy_id" in result[0]["issues"]
    assert "duplicate_policy_id" in result[1]["issues"]
    assert "duplicate_sequence" in result[1]["issues"]
    assert "duplicate_sequence" in result[2]["issues"]


def test_old_eligibility_cancelled_policy_and_dates_do_not_establish_current_coverage():
    policy = map_result(policies=[{**POLICY, "cancelled": True, "expirationdate": "12/31/2020"}]).to_review_dict()["insurance"]["policies"][0]
    pf = fields(policy)
    assert pf["eligibility_status"]["value"] == "Active"
    assert pf["eligibility_checked_at"]["value"] == "09/15/2026"
    assert pf["policy_expiration_date"]["parsedValue"] == "2020-12-31"
    assert pf["package_expiration_date"]["parsedValue"] == "2027-12-31"
    assert pf["issue_date"]["parsedValue"] == "2026-09-01"
    assert policy["coverageEffectiveDate"] is None
    assert policy["currentCoverage"] == "unknown"
    assert policy["cardImageAvailability"] == "unknown"
    assert policy["eligibilityVerifiedByCab"] is False
    assert policy["sourceVersion"] is None


@pytest.mark.parametrize("envelope,state,count", [
    ({"insurances": [], "totalcount": 0}, "empty", 0),
    ({"insurances": []}, "partial", None),
    ({"insurances": [], "totalcount": 9}, "partial", 9),
    ({"insurances": [POLICY], "totalcount": 2}, "partial", 2),
    ({"insurances": [POLICY], "totalcount": 1, "next": "https://untrusted.example/do-not-follow"}, "partial", 1),
    ({"insurances": [POLICY], "totalcount": 1, "previous": "prior-page"}, "partial", 1),
])
def test_bounded_page_completeness_never_implies_missing_card(envelope, state, count):
    result = map_result(envelope=envelope).to_review_dict()
    assert result["reads"]["insurance"]["state"] == state
    assert result["reads"]["insurance"]["totalCount"] == count
    assert result["insurance"]["cardImageAvailability"] == "unknown"
    assert "untrusted.example" not in json.dumps(result)


def test_unavailable_reads_differ_from_successfully_empty_reads():
    unavailable = map_athena_card_chase(context=CONTEXT, patient_response=None, insurance_response=None).to_review_dict()
    empty = map_result(patient=[], policies=[]).to_review_dict()
    assert unavailable["reads"]["patient"]["recordCount"] is None
    assert unavailable["reads"]["insurance"]["state"] == "unavailable"
    assert empty["reads"]["patient"]["recordCount"] == 0
    assert empty["reads"]["insurance"]["state"] == "empty"
    assert unavailable["contact"]["outreachAuthorized"] is False
    assert empty["contact"]["outreachAuthorized"] is False


@pytest.mark.parametrize("patch", [
    {"source_patient_id": "other"}, {"source_patient_id": 60178}, {"vendor_practice_id": "other"},
    {"synthetic_only": False}, {"synthetic_only": 1}, {"server_practice_id": ""},
    {"batch_id": " x"}, {"batch_id": "x" * 129}, {"batch_id": "X\ud800"},
    {"observed_at": "2026-09-29T13:30:00"}, {"observed_at": "2026-09-29T13:30:00-04:60"},
    {"observed_at": "2026-09-29T13:30:00+00:99"}, {"observed_at": "2026-09-29T13:30:00-00:00"},
    {"observed_at": "2026-02-30T13:30:00Z"}, {"observed_at": "2026-09-29T25:30:00Z"},
])
def test_invalid_context_fails_before_source_parsing(patch):
    with pytest.raises(AthenaCardChaseError, match="^invalid_context$"):
        map_athena_card_chase(context=replace(CONTEXT, **patch), patient_response=b"not-json", insurance_response=b"not-json")


@pytest.mark.parametrize("patient", [None, {}, [{"patientid": "wrong"}], [{}],
                                      [{"patientid": 60178}], PATIENT * 2,
                                      [{**PATIENT[0], "practiceid": "wrong"}]])
def test_invalid_patient_shape_or_identity_rejects_without_source_values(patient):
    with pytest.raises(AthenaCardChaseError, match="^invalid_source$"):
        map_result(patient=patient)


@pytest.mark.parametrize("envelope", [
    [], {}, {"insurances": None}, {"insurances": [None]}, {"insurances": [POLICY] * 6},
    {"insurances": [POLICY], "totalcount": 0}, {"insurances": [], "totalcount": True},
    {"insurances": [], "totalcount": None}, {"insurances": [], "totalcount": "0"},
    {"insurances": [], "next": []}, {"insurances": [], "previous": True},
    {"insurances": [{**POLICY, "patientid": "other"}], "totalcount": 1},
    {"insurances": [{**POLICY, "practiceid": "other"}], "totalcount": 1},
    {"insurances": [POLICY], "patientid": "other"}, {"insurances": [], "practiceid": "other"},
])
def test_invalid_insurance_envelope_or_identity_rejects(envelope):
    with pytest.raises(AthenaCardChaseError, match="^invalid_source$"):
        map_result(envelope=envelope)


def test_empty_patient_cannot_be_paired_with_nonempty_insurance():
    with pytest.raises(AthenaCardChaseError, match="^binding_failed$"):
        map_result(patient=[])


@pytest.mark.parametrize("raw", [
    b"", b"not-json", b"\xff", b'{"insurances":[],"insurances":[]}',
    b'{"insurances":[],"ignored":NaN}', b'{"insurances":[],"ignored":1e999}',
    b'{"insurances":[],"ignored":"\\ud800"}', b'{"insurances":[],"ignored":"\\u007f"}',
    b'{"insurances":[],"ignored":"\\n"}', b"[" * 1000 + b"]" * 1000,
    b" " * 262145, b'{"insurances":[],"ignored":' + b"[0," * 20 + b"0" + b"]" * 20 + b"}",
], ids=["empty", "not-json", "invalid-utf8", "duplicate-key", "nan", "infinity",
        "surrogate", "del", "control", "nested", "oversized", "over-depth"])
def test_strict_json_boundaries_and_static_failure(raw):
    with pytest.raises(AthenaCardChaseError, match="^invalid_source$"):
        map_athena_card_chase(context=CONTEXT, patient_response=encoded(PATIENT), insurance_response=raw)


@pytest.mark.parametrize("name,value", [
    ("sequencenumber", "01"), ("sequencenumber", 1.0), ("sequencenumber", True),
    ("sequencenumber", 0), ("sequencenumber", 100), ("expirationdate", "02/30/2026"),
    ("issuedate", "2026-09-01"), ("insuranceidnumber", 12345),
    ("insuredidnumber", 12345), ("policynumber", 12345),
    ("insuranceid", 12345), ("insurancepackagepayerid", 12345),
    ("insuranceidnumber", " 001"), ("insuranceidnumber", "0" * 129),
    ("insurancepolicyholder", {"nested": "DO-NOT-REFLECT"}),
])
def test_invalid_field_is_marked_without_coercion_or_nested_reflection(name, value):
    policy = map_result(policies=[{**POLICY, name: value}]).to_review_dict()["insurance"]["policies"][0]
    field = next(f for f in policy["fields"] if f["source"]["pointer"].endswith("/" + name))
    assert field["state"] == "invalid"
    assert field["parsedValue"] is None
    assert "DO-NOT-REFLECT" not in json.dumps(policy)


def test_snapshot_is_immutable_defensive_and_preserves_input_bytes_and_whitespace():
    patient, policy = deepcopy(PATIENT), deepcopy(POLICY)
    patient[0]["firstname"] = " CASEY "
    before = deepcopy((patient, policy))
    result = map_result(patient, [policy])
    assert (patient, policy) == before
    first = result.to_review_dict()
    assert fields(first["patient"])["first_name"]["value"] == " CASEY "
    first["insurance"]["policies"][0]["accepted"] = True
    assert result.to_review_dict()["insurance"]["policies"][0]["accepted"] is False
    with pytest.raises(FrozenInstanceError):
        result.candidate_id = "other"
    assert "CASEY" not in repr(result) and "60178" not in repr(CONTEXT)


def test_replay_identity_binds_context_exact_source_bytes_and_mapping_version():
    result = map_result()
    assert result == map_result()
    assert result.candidate_id != map_result(context=replace(CONTEXT, batch_id="next-batch")).candidate_id
    assert result.candidate_id != map_result(context=replace(CONTEXT, server_practice_id="other-practice")).candidate_id
    modified = deepcopy(POLICY)
    modified["insuranceidnumber"] = "different"
    assert result.candidate_id != map_result(policies=[modified]).candidate_id


def test_mapper_has_no_network_env_or_import_activation(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Unexpected external action")
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(client.AthenaHTTPTransport, "request_json", forbidden)
    monkeypatch.setenv("COSMANEURA_ATHENA_CLIENT_ID", "DO-NOT-READ")
    monkeypatch.setenv("COSMANEURA_ATHENA_CLIENT_SECRET", "DO-NOT-READ")
    assert not client._ATHENA_PREVIEW_OPERATION_ALLOWLIST
    result = map_result(envelope={"insurances": [POLICY], "totalcount": 2,
                                  "next": "https://example.test/never-follow"})
    assert result.to_review_dict()["insurance"]["state"] == "partial"
    assert "DO-NOT-READ" not in result._json
    assert not client._ATHENA_PREVIEW_OPERATION_ALLOWLIST


def test_every_present_field_points_to_its_exact_source_observation():
    patients = deepcopy(PATIENT)
    policies = [deepcopy(POLICY), {**POLICY, "insuranceid": "0088", "sequencenumber": "2"}]
    envelope = {"insurances": policies, "totalcount": 2}
    result = map_result(patients, envelope=envelope).to_review_dict()
    originals = {"patient": patients, "insurance": envelope}
    containers = [result["patient"], *result["insurance"]["policies"]]
    for container in containers:
        for field in container["fields"]:
            if field["state"] != "present":
                continue
            source = field["source"]
            original = originals[source["dataset"]]
            assert source["sha256"] == hashlib.sha256(encoded(original)).hexdigest()
            for part in source["pointer"].split("/")[1:]:
                original = original[int(part)] if isinstance(original, list) else original[part]
            assert type(field["value"]) is type(original)
            assert field["value"] == original



def scoped_context(environment):
    vendor_practice = "195900" if environment == "preview" else "777001"
    scope = AthenaStaffScope(
        AthenaStaffProfile(CONTEXT.server_practice_id, environment, vendor_practice, 1),
        "invented-case", "invented-patient-ref", CONTEXT.source_patient_id, 1,
    )
    return replace(CONTEXT, scope=scope, vendor_practice_id=vendor_practice,
                   synthetic_only=environment == "preview")


def scoped_responses(context, available):
    if not available:
        return {"patient_response": None, "insurance_response": None}
    identity = {"patientid": context.scope.source_patient_id,
                "practiceid": context.scope.profile.vendor_practice_id}
    return {
        "patient_response": encoded([{**PATIENT[0], **identity}]),
        "insurance_response": encoded({**identity, "insurances": [POLICY], "totalcount": 1}),
    }


@pytest.mark.parametrize("environment", ["preview", "production"])
@pytest.mark.parametrize("available", [False, True], ids=["unavailable", "populated"])
@pytest.mark.parametrize("field,value", [
    ("server_practice_id", "other-practice"),
    ("vendor_practice_id", "195901"),
    ("source_patient_id", "60179"),
])
def test_supplied_scope_must_match_every_context_identity_before_reads(
        environment, available, field, value, monkeypatch):
    context = scoped_context(environment)
    responses = scoped_responses(context, available)

    def unexpected_decode(*args, **kwargs):
        pytest.fail("Contradictory context reached optional response parsing")

    monkeypatch.setattr(mapper, "decode_athena_card_read", unexpected_decode)
    with pytest.raises(AthenaCardChaseError, match="^invalid_context$"):
        map_athena_card_chase(context=replace(context, **{field: value}), **responses)


@pytest.mark.parametrize("available", [False, True], ids=["unavailable", "populated"])
@pytest.mark.parametrize("invalid_part", ["preview_vendor", "preview_patient", "profile"])
def test_supplied_preview_scope_is_validated_even_without_responses(available, invalid_part):
    context = scoped_context("preview")
    responses = scoped_responses(context, available)
    if invalid_part == "preview_vendor":
        scope = replace(context.scope, profile=replace(context.scope.profile, vendor_practice_id="195901"))
    elif invalid_part == "preview_patient":
        scope = replace(context.scope, source_patient_id="60185")
    else:
        scope = replace(context.scope)
        object.__setattr__(scope, "profile", None)
    with pytest.raises(AthenaCardChaseError, match="^invalid_context$"):
        map_athena_card_chase(context=replace(context, scope=scope), **responses)


@pytest.mark.parametrize("environment", ["preview", "production"])
@pytest.mark.parametrize("available", [False, True], ids=["unavailable", "populated"])
def test_matching_scope_preserves_environment_and_optional_read_semantics(environment, available):
    context = scoped_context(environment)
    responses = scoped_responses(context, available)
    result = map_athena_card_chase(context=context, **responses).to_review_dict()
    production = environment == "production"
    assert result["source"]["system"] == "athena_" + environment
    assert result["source"]["syntheticOnly"] is not production
    assert result["source"].get("dataClass") == ("operational" if production else None)
    assert result["source"]["serverPracticeId"] == context.server_practice_id
    assert result["source"]["vendorPracticeId"] == context.vendor_practice_id
    assert result["source"]["patientId"] == context.source_patient_id
    assert {read["state"] for read in result["reads"].values()} == {
        "complete" if available else "unavailable"}
    assert result["applied"] is False
    assert result["contact"]["outreachAuthorized"] is False
    if not production:
        legacy = map_athena_card_chase(context=replace(context, scope=None), **responses)
        assert result == legacy.to_review_dict()


@pytest.mark.parametrize("environment", ["preview", "production"])
@pytest.mark.parametrize("available", [False, True], ids=["unavailable", "populated"])
def test_supplied_scope_cannot_reverse_environment_data_class(environment, available):
    context = scoped_context(environment)
    with pytest.raises(AthenaCardChaseError, match="^invalid_context$"):
        map_athena_card_chase(
            context=replace(context, synthetic_only=not context.synthetic_only),
            **scoped_responses(context, available),
        )
