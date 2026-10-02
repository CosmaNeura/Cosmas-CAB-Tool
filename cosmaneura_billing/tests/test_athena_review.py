"""Reviewed source -> local case evidence -> CAB encounter golden and races."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from threading import Barrier
from uuid import uuid4

import pytest

from cosmaneura_billing.athena_client import MockAthenaStaffReadClient
from cosmaneura_billing.athena_review import AthenaReviewError, ReviewActor
from cosmaneura_billing.athena_review_routes import make_athena_service
from cosmaneura_billing.chase.models import ChaseItem, Trigger
from cosmaneura_billing.chase.worklist import Worklist


@pytest.fixture
def service():
    worklist = Worklist()
    for name in ("one", "two"):
        worklist.upsert(ChaseItem(name, "synthetic-"+name, Trigger.MANUAL, "2026-09-29"))
    return make_athena_service(mode="mock", practice_id="local-test", chase_store=worklist,
                               authorize=lambda actor: actor.role in ("BillingStaff", "Admin"))


@pytest.fixture
def actor():
    return ReviewActor(str(uuid4()), str(uuid4()), "BillingStaff")


def refresh_command(revision=0):
    return {"sourcePatientId":"60178", "operationId":str(uuid4()), "expectedRevision":revision}


def apply_command(view):
    return {"reviewId":view["reviewId"], "operationId":str(uuid4()), "expectedRevision":view["revision"],
            "confirmedPatientMatch":True, "selectedPhoneField":"mobile_phone", "selectedEmail":True,
            "selectedPolicyIds":[p["candidateId"] for p in view["candidate"]["insurance"]["policies"]],
            "confirmedPolicyReview":True, "reviewReason":"Compared invented patient and insurance fields."}


def national_phone_source(client, value="2025550123"):
    original = client.read_card
    def read(patient):
        data = original(patient)
        rows = json.loads(data["patient"])
        rows[0]["mobilephone"] = value
        data["patient"] = json.dumps(rows).encode()
        return data
    client.read_card = read


def billing_command(view, note):
    return {"operationId":str(uuid4()), "expectedRevision":view["revision"], "sourceEncounterId":"88001",
            "departmentId":"1", "expectedNoteSha256":note["responseSha256"],
            "expectedMetadataSha256":note["metadataSha256"], "confirmedPatientMatch":True,
            "confirmedFinishedNote":True, "confirmedClinicalContext":True, "patientAge":26,
            "patientSex":"M", "encounterType":"office_established", "placeOfService":"11",
            "providerSpecialty":"Family Medicine", "reviewReason":"Reviewed complete synthetic note and context."}


def test_golden_review_applies_only_selected_evidence_then_imports_exact_plaintext(service, actor):
    before = service.store.chase_store.get("one").to_dict()
    source = service.refresh("one", actor, refresh_command())
    assert source["applied"] is None and source["candidate"]["contact"]["outreachAuthorized"] is False
    command = apply_command(source)
    applied = service.apply("one", actor, command)
    assert applied["revision"] == 2 and applied["applied"]["consent"] == "unknown"
    fields = {f["name"]:f["value"] for f in applied["applied"]["policies"][0]["fields"]}
    assert fields["member_id"] == "DEMO-MEMBER-42" and fields["group_number"] == "DEMO-GROUP-7"
    assert applied["applied"]["policies"][0]["subscriberPatientMatch"] == "unverified"
    assert service.store.chase_store.get("one").to_dict() == before
    assert service.apply("one", actor, command) == applied
    note = service.billing_read("one", actor, department_id="1", encounter_id="88001")["note"]
    imported = service.billing_apply("one", actor, billing_command(applied, note))
    stored = service.store.encounters.get(imported["billingImport"]["encounterId"])
    assert stored.bundle.signed_note == note["plainText"] and stored.source == "athena_staff_review_mock"
    assert stored.bundle.submitted_em_code is None and stored.bundle.submitted_icd10 == []
    assert len(service.store.encounters.all()) == 1
    imported["candidate"]["patient"]["fields"].clear()
    assert service.current("one", actor)["candidate"]["patient"]["fields"]


@pytest.mark.parametrize("edit", [
    {"confirmedPatientMatch":False}, {"selectedPhoneField":"work_phone"}, {"selectedEmail":"true"},
    {"selectedPolicyIds":["f"*64]}, {"confirmedPolicyReview":False}, {"reviewReason":"\ud800"},
    {"selectedPolicyIds":[[]]}, {"extra":"secret"},
])
def test_human_choices_are_closed_and_must_refer_to_exact_source(service, actor, edit):
    source = service.refresh("one", actor, refresh_command())
    with pytest.raises(AthenaReviewError):
        service.apply("one", actor, {**apply_command(source), **edit})
    assert service.current("one", actor)["revision"] == 1 and not service.store._data["contacts"]


def test_stale_refresh_operation_conflict_case_binding_and_actor_binding(service, actor):
    command = refresh_command()
    source = service.refresh("one", actor, command)
    assert service.refresh("one", actor, command) == source
    with pytest.raises(AthenaReviewError, match="operation_conflict"):
        service.refresh("two", actor, command)
    with pytest.raises(AthenaReviewError, match="operation_conflict"):
        service.refresh("one", replace(actor,user_id=str(uuid4())), command)
    service.refresh("one", actor, refresh_command(1))
    with pytest.raises(AthenaReviewError, match="stale_review"):
        service.apply("one", actor, apply_command(source))
    service.store.chase_store.get("one").patient_ref = "different"
    with pytest.raises(AthenaReviewError, match="stale_review"):
        service.current("one", actor)


def test_actual_source_change_during_confirmation_requires_new_review(service, actor):
    source = service.refresh("one", actor, refresh_command())
    original = service.client.read_card
    def changed(patient):
        data = original(patient)
        data["patient"] = data["patient"].replace(b"Casey", b"Changed")
        return data
    service.client.read_card = changed
    with pytest.raises(AthenaReviewError, match="stale_review"):
        service.apply("one", actor, apply_command(source))
    assert service.current("one", actor)["applied"] is None


def test_mapper_version_change_requires_refresh_before_applying_pending_review(service, actor, monkeypatch):
    import cosmaneura_billing.athena_card_chase as mapper
    with monkeypatch.context() as previous:
        previous.setattr(mapper, "MAPPING_VERSION", "athena-card-chase-map.2026-09-30")
        source = service.refresh("one", actor, refresh_command())
    with pytest.raises(AthenaReviewError, match="stale_review"):
        service.apply("one", actor, apply_command(source))
    assert service.current("one", actor)["applied"] is None
    assert not service.store._data["contacts"]
    refreshed = service.refresh("one", actor, refresh_command(1))
    assert refreshed["candidate"]["mappingVersion"] == mapper.MAPPING_VERSION
    assert service.apply("one", actor, apply_command(refreshed))["applied"]["consent"] == "unknown"


@pytest.mark.parametrize("delta", [901, -1])
def test_expired_or_future_review_rejects_before_reread(service, actor, delta):
    source = service.refresh("one", actor, refresh_command())
    when = datetime.fromisoformat(source["candidate"]["source"]["observedAt"].replace("Z","+00:00"))
    service.clock = lambda: (when + timedelta(seconds=delta)).isoformat()
    service.client.read_card = lambda *args: pytest.fail("stale review must not fetch")
    with pytest.raises(AthenaReviewError, match="stale_review"):
        service.apply("one", actor, apply_command(source))


def test_two_simultaneous_applies_only_one_commit(service, actor):
    source = service.refresh("one", actor, refresh_command())
    barrier, original = Barrier(2), service.client.read_card
    def fetch(patient):
        result = original(patient)
        barrier.wait(timeout=3)
        return result
    service.client.read_card = fetch
    def apply(_):
        try:
            return service.apply("one", actor, apply_command(source))["revision"]
        except AthenaReviewError as exc:
            return exc.code
    with ThreadPoolExecutor(2) as executor:
        result = list(executor.map(apply, range(2)))
    assert sorted(map(str,result)) == ["2","stale_review"]
    assert len(service.store._data["receipts"]) == 2


def test_missing_case_and_disallowed_role_never_fetch(service, actor):
    service.client.read_card = lambda *args: pytest.fail("unauthorized source read")
    with pytest.raises(AthenaReviewError, match="not_found"):
        service.refresh("missing", actor, refresh_command())
    with pytest.raises(AthenaReviewError, match="not_authorized"):
        ReviewActor(actor.tenant_id,actor.user_id,"Provider")
    service.store.authorize = lambda actor:False
    with pytest.raises(AthenaReviewError, match="not_authorized"):
        service.refresh("one", actor, refresh_command())


@pytest.mark.parametrize("edit", [{"confirmedFinishedNote":False}, {"confirmedClinicalContext":False},
    {"placeOfService":"10"}, {"patientAge":True}, {"expectedNoteSha256":"f"*64}, {"providerSpecialty":"bad\ntext"}])
def test_billing_import_requires_reviewed_source_and_manual_clinical_context(service, actor, edit):
    source = service.refresh("one", actor, refresh_command())
    applied = service.apply("one", actor, apply_command(source))
    note = service.billing_read("one", actor, department_id="1", encounter_id="88001")["note"]
    with pytest.raises(AthenaReviewError):
        service.billing_apply("one", actor, {**billing_command(applied, note), **edit})
    assert not service.store.encounters.all() and service.current("one", actor)["revision"] == 2


def test_disabled_and_preview_factory_have_no_implicit_memory_fallback():
    assert make_athena_service(mode="disabled",practice_id="x",chase_store=None) is None
    with pytest.raises(ValueError):
        make_athena_service(mode="preview",practice_id="x",chase_store=None,client_id="x",client_secret="y")


def test_billing_retry_is_current_view_and_does_not_duplicate_encounter(service, actor):
    source = service.refresh("one", actor, refresh_command())
    view = service.apply("one", actor, apply_command(source))
    note = service.billing_read("one", actor, department_id="1", encounter_id="88001")["note"]
    command = billing_command(view,note)
    saved = service.billing_apply("one",actor,command)
    service.client.read_billing = lambda *args:pytest.fail("retry must not fetch")
    assert service.billing_apply("one",actor,command) == saved
    assert len(service.store.encounters.all()) == 1


@pytest.mark.parametrize("value", ["2025550123", "202-555-0123", "(202) 555-0123"])
def test_staff_country_confirmation_normalizes_source_and_preserves_provenance(service, actor, value):
    national_phone_source(service.client, value)
    source = service.refresh("one", actor, refresh_command())
    command = {**apply_command(source), "selectedPhoneCountry": "US"}
    result = service.apply("one", actor, command)
    phone = next(c for c in result["applied"]["contacts"] if c["field"] == "mobile_phone")
    assert phone == {"field": "mobile_phone", "value": "+12025550123", "consent": "unknown",
        "normalization": {"sourceValue": value, "country": "US", "confirmedByStaff": True}}
    assert result["candidate"]["contact"]["candidates"][0]["field"]["value"] == value
    assert service.store._data["contacts"]["one"][0] == phone
    assert result["candidate"]["contact"]["outreachAuthorized"] is False
    service.client.read_card = lambda *_args: pytest.fail("exact replay must not reread")
    assert service.apply("one", actor, command) == result
    with pytest.raises(AthenaReviewError, match="operation_conflict"):
        service.apply("one", actor, {**command, "selectedPhoneCountry": None})


@pytest.mark.parametrize("country", [None, "", "CA", True])
def test_national_phone_cannot_apply_without_explicit_supported_country(service, actor, country):
    national_phone_source(service.client)
    source = service.refresh("one", actor, refresh_command())
    service.client.read_card = lambda *_args: pytest.fail("unconfirmed country must not reread")
    with pytest.raises(AthenaReviewError):
        service.apply("one", actor, {**apply_command(source), "selectedPhoneCountry": country})
    assert service.current("one", actor)["revision"] == 1 and not service.store._data["contacts"]


def test_country_confirmation_does_not_override_changed_source(service, actor):
    national_phone_source(service.client)
    source = service.refresh("one", actor, refresh_command())
    national_phone_source(service.client, "2025550199")
    with pytest.raises(AthenaReviewError, match="stale_review"):
        service.apply("one", actor, {**apply_command(source), "selectedPhoneCountry": "US"})
    assert service.current("one", actor)["applied"] is None


def test_null_country_keeps_old_e164_replay_identity_and_us_is_not_reinterpreted(service, actor):
    source = service.refresh("one", actor, refresh_command())
    with pytest.raises(AthenaReviewError):
        service.apply("one", actor, {**apply_command(source), "selectedPhoneCountry": "US"})
    command = apply_command(source)
    result = service.apply("one", actor, command)
    service.client.read_card = lambda *_args: pytest.fail("old-client replay must not reread")
    assert service.apply("one", actor, {**command, "selectedPhoneCountry": None}) == result
