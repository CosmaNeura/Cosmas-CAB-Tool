"""Real restricted-login preparation checks; no dispatcher or vendor transport.

Owner inserts arrange synthetic historical evidence through the unchanged SQL
constraints. They do not stand in for approval/dispatch API acceptance.
"""
from dataclasses import fields, replace
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from psycopg import sql

from test_chase_communications_postgres import (
    DSN, PHONE, SENDER, bind, fails, lane, prepare, psycopg, receive, seed_case,
    schema as foundation_schema,
)
from cosmaneura_billing.access_audit import AccessAuditEvent
from cosmaneura_billing.persistence import PostgresAccessAuditStore
from cosmaneura_billing.chase.communications.event_review import review_ref
from cosmaneura_billing.chase.communications.postgres import (
    PostgresCommunicationsStore, SmsPreparationEligibility,
)

pytestmark = pytest.mark.skipif(not DSN, reason="disposable PostgreSQL is not configured")
DRAFT = SmsPreparationEligibility(**{f.name: True for f in fields(SmsPreparationEligibility)})
READ_TABLES = ["chase_communication_" + suffix for suffix in (
    "consent_decisions", "provider_events", "dispatch_starts", "dispatch_receipts",
    "unknown_resolutions",
)]


@pytest.fixture(scope="module", autouse=True)
def complete_schema(foundation_schema):
    if not DSN:
        return
    with psycopg.connect(DSN) as db:
        # The locked migration runner must have installed the final schema first.
        for table in READ_TABLES + ["chase_communication_unassigned_sources"]:
            assert db.execute("SELECT to_regclass(%s)", (table,)).fetchone()[0], table


@pytest.fixture
def purpose_lane(lane):
    with psycopg.connect(DSN) as db:
        db.execute(sql.SQL("GRANT SELECT ON {} TO {}").format(
            sql.SQL(",").join(map(sql.Identifier, READ_TABLES)), sql.Identifier(lane.role)))
        # PostgreSQL row locking needs an UPDATE privilege; no identity-table
        # mutation or broad UPDATE grant is needed for the existing case lock.
        db.execute(sql.SQL("GRANT UPDATE(state) ON chase_items TO {}").format(sql.Identifier(lane.role)))
    lane.store = PostgresCommunicationsStore(lane.dsn, practice_id=lane.practice,
                                           purpose_consent_enabled=True)
    return lane


def consent(lane, binding, granted=True):
    decision = str(uuid4())
    with psycopg.connect(DSN) as db:
        db.execute("""INSERT INTO chase_communication_consent_decisions
            (practice_id,decision_id,binding_id,granted,source_reference,actor_tenant_id,actor_id)
            VALUES (%s,%s,%s,%s,'synthetic:source-permission',%s,%s)""",
            (lane.practice, decision, binding, granted, lane.tenant, lane.actor))
    return decision


def unknown_start(lane, operation):
    """Arrange one durable historical start, without invoking a transport."""
    policy, decision = str(uuid4()), consent(lane, operation["bindingId"])
    with psycopg.connect(DSN) as db:
        db.execute("SELECT set_config('cosmaneura.practice_id',%s,true)", (lane.practice,))
        db.execute("""INSERT INTO chase_communication_policy_decisions
            (practice_id,decision_id,policy_hash,approved,actor_tenant_id,actor_id)
            VALUES (%s,%s,%s,true,%s,%s)""", (lane.practice, policy, "a"*64, lane.tenant, lane.actor))
        db.execute("""INSERT INTO chase_communication_authorizations
            (practice_id,operation_id,policy_decision_id,consent_decision_id,binding_id,
             direct_contract_hash,actor_tenant_id,actor_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
            (lane.practice, operation["operationId"], policy, decision, operation["bindingId"],
             "b"*64, lane.tenant, lane.actor))
        db.execute("INSERT INTO chase_communication_dispatch_starts(practice_id,operation_id) VALUES (%s,%s)",
                   (lane.practice, operation["operationId"]))


@pytest.mark.parametrize("generic", ["unknown", "not_required"])
def test_purpose_evidence_is_required_and_latest_revocation_blocks_new_draft(purpose_lane, generic):
    lane = purpose_lane
    lane.snapshot = seed_case(lane, consent=generic, observed="2020-01-02T00:00:00Z")
    binding = bind(lane)
    fails("communications_consent_unavailable", lambda: prepare(lane, binding, eligibility=DRAFT))
    consent(lane, binding)
    operation = prepare(lane, binding, eligibility=DRAFT)
    assert operation["status"] == "prepared" and operation["providerSid"] is None
    consent(lane, binding, False)
    fails("communications_consent_unavailable", lambda: prepare(lane, binding, eligibility=DRAFT))
    # The immutable reservation is recoverable without creating new authority.
    assert prepare(lane, binding, operation_id=operation["operationId"], eligibility=DRAFT) == operation
    with psycopg.connect(DSN) as db:
        for table in ("authorizations", "dispatch_starts", "dispatch_receipts"):
            assert db.execute(sql.SQL("SELECT count(*) FROM {} WHERE practice_id=%s").format(
                sql.Identifier("chase_communication_" + table)), (lane.practice,)).fetchone()[0] == 0


def test_purpose_grant_cannot_override_explicit_route_denial(purpose_lane):
    lane = purpose_lane
    lane.snapshot = seed_case(lane, consent="denied", observed="2020-01-02T00:00:00Z")
    binding = bind(lane)
    consent(lane, binding)
    fails("communications_consent_unavailable", lambda: prepare(lane, binding, eligibility=DRAFT))


@pytest.mark.parametrize("field", [f.name for f in fields(SmsPreparationEligibility)])
@pytest.mark.parametrize("invalid", [False, 1, "true", None])
def test_draft_attestation_is_strict_and_creates_no_preparation(purpose_lane, field, invalid):
    lane = purpose_lane
    fails("communications_policy_blocked", lambda: prepare(lane, bind(lane),
        eligibility=replace(DRAFT, **{field: invalid})))
    assert lane.store.case_view(lane.case)["operations"] == []


def test_draft_attestation_is_not_a_dict_voice_or_legacy_permission(purpose_lane):
    lane = purpose_lane
    binding = bind(lane)
    fails("communications_policy_blocked", lambda: prepare(lane, binding, eligibility=vars(DRAFT)))
    fails("communications_policy_blocked", lambda: prepare(lane, binding, eligibility=DRAFT, channel="automated_voice"))
    lane.store = PostgresCommunicationsStore(lane.dsn, practice_id=lane.practice)
    fails("communications_policy_blocked", lambda: prepare(lane, binding, eligibility=DRAFT))


@pytest.mark.parametrize("source", ["legacy", "provider"])
def test_route_rotation_never_clears_recipient_stop(purpose_lane, source):
    lane = purpose_lane
    bind(lane)
    if source == "legacy":
        event = receive(lane, "STOP")
        assert event["reviewRef"] == review_ref("legacy", lane.account, event["eventId"])
    else:
        with psycopg.connect(DSN) as db:
            db.execute("""INSERT INTO chase_communication_provider_events
                (practice_id,account_sid,event_id,provider_sid,kind,channel,sender,recipient,
                 text,status,disposition,body_hash) VALUES (%s,%s,%s,%s,'sms_inbound','sms',%s,%s,
                 'STOP','received','sms_opt_out',%s)""",
                (lane.practice, lane.account, uuid4().hex*2, "SM"+uuid4().hex, PHONE, SENDER, "c"*64))
    rotated = bind(lane, sender="+12025550198", account_sid="AC"+uuid4().hex)
    consent(lane, rotated)
    fails("communications_contact_suppressed", lambda: prepare(lane, rotated, eligibility=DRAFT))
    assert lane.store.case_view(lane.case)["suppression"]["sms"] is True


def test_unknown_start_blocks_new_preparation_but_preserves_exact_receipt(purpose_lane):
    lane = purpose_lane
    binding = bind(lane)
    operation = prepare(lane, binding, eligibility=DRAFT)
    unknown_start(lane, operation)
    fails("communications_unknown_requires_resolution", lambda: prepare(lane, binding, eligibility=DRAFT))
    assert prepare(lane, binding, operation_id=operation["operationId"], eligibility=DRAFT) == operation
    assert lane.store.case_view(lane.case)["operations"] == [operation]


def test_real_append_only_audit_and_preparation_commit_or_roll_back_together(purpose_lane):
    lane = purpose_lane
    binding = bind(lane)
    with psycopg.connect(DSN) as db:
        db.execute(sql.SQL("GRANT INSERT ON access_audit_attempts,access_audit_completions TO {}").format(sql.Identifier(lane.role)))
    audit_ids = []
    def audit(db, action, case_id, operation_id=None):
        assert action == "communications_prepare" and case_id == lane.case
        assert db.execute("SELECT 1 FROM chase_communication_operations WHERE practice_id=%s AND operation_id=%s",
                          (lane.practice, operation_id)).fetchone()
        event = AccessAuditEvent(event_id=str(uuid4()), occurred_at=datetime.now(timezone.utc).isoformat(),
            practice_id=lane.practice, actor_tenant_id=lane.tenant, actor_user_id=lane.actor,
            actor_roles=("BillingStaff",), action="chase.communications", resource_id=case_id,
            route="/api/chase/communications/action", method="POST", status=200, outcome="allowed", source_ip="127.0.0.1")
        audit_ids.append(event.event_id)
        PostgresAccessAuditStore._insert_attempt(db, event)
        PostgresAccessAuditStore._insert_completion(db, event)
    lane.store.transaction_audit = audit
    first = prepare(lane, binding, eligibility=DRAFT)
    with psycopg.connect(DSN) as db:
        db.execute(sql.SQL("REVOKE INSERT ON access_audit_completions FROM {}").format(sql.Identifier(lane.role)))
    fails("communications_store_unavailable", lambda: prepare(lane, binding, eligibility=DRAFT))
    assert lane.store.case_view(lane.case)["operations"] == [first]
    with psycopg.connect(DSN) as db:
        for table in ("access_audit_attempts", "access_audit_completions"):
            assert db.execute(sql.SQL("SELECT event_id FROM {} WHERE practice_id=%s").format(sql.Identifier(table)),
                              (lane.practice,)).fetchall() == [(UUID(audit_ids[0]),)]
        assert db.execute("SELECT count(*) FROM chase_communication_events WHERE practice_id=%s", (lane.practice,)).fetchone()[0] == 1
