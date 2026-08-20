"""Live PostgreSQL tests for migration 004's scheduler repository.

The module is skipped without a disposable database.  It uses only synthetic
patient/contact values and unique practice IDs; CI destroys the database after
the run, so immutable evidence rows intentionally need no trigger-bypassing
cleanup.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import time
from uuid import UUID, uuid4

import pytest

psycopg = pytest.importorskip("psycopg")

from cosmaneura_billing.chase.models import ChaseItem, Channel, State, Trigger
from cosmaneura_billing.chase.policy import (
    ApprovalMode,
    ChasePolicy,
    DEFAULT_TEMPLATES,
    Step,
)
from cosmaneura_billing.chase.policy_fingerprint import DeliveryConfiguration
from cosmaneura_billing.chase.scheduler_store import (
    ContactRouteInput,
    ContactSnapshotInput,
    PostgresChaseSchedulerRepository,
    SchedulerConflict,
    SchedulerLeaseExpired,
)
from cosmaneura_billing.chase.scheduling import ScheduleAction, cadence_due_at
from cosmaneura_billing.access_audit import AccessAuditEvent
from cosmaneura_billing.direct_contract import (
    DirectContractList,
    check_direct_contract,
)
from cosmaneura_billing.migrations import migration_body


TEST_DSN = os.environ.get("COSMANEURA_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(
    not TEST_DSN, reason="COSMANEURA_TEST_DATABASE_URL is not configured",
)
ROOT = Path(__file__).resolve().parents[1]
# These integration tests exercise durable scheduling and transaction behavior,
# not the pure quiet-hours rules covered in test_chase_scheduling.py.  Keep the
# policy window twelve hours away from the module's real-time test clock so a CI
# run crossing midnight cannot silently turn every due reservation into BLOCKED.
_TEST_CLOCK_HOUR = datetime.now(timezone.utc).hour
_TEST_QUIET_HOURS = (
    (_TEST_CLOCK_HOUR + 12) % 24,
    (_TEST_CLOCK_HOUR + 13) % 24,
)


@pytest.fixture(scope="session", autouse=True)
def _schema():
    if not TEST_DSN:
        yield
        return
    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        if "test" not in (conn.info.dbname or "").casefold():
            pytest.fail(
                "COSMANEURA_TEST_DATABASE_URL must name a disposable test database"
            )
        migrations = ROOT / "migrations"
        for relation, filename in (
            ("chase_items", "002_chase_persistence.up.sql"),
            ("encounters", "003_pilot_persistence.up.sql"),
            ("chase_delivery_outbox", "004_chase_delivery_runtime.up.sql"),
        ):
            exists = conn.execute(
                "SELECT to_regclass(%s)", (f"public.{relation}",),
            ).fetchone()[0]
            if exists is None:
                conn.execute(
                    migration_body(
                        (migrations / filename).read_text(encoding="utf-8")
                    ),
                    prepare=False,
                )
        accepted_helper = conn.execute(
            """SELECT to_regprocedure(
                   'public.cosmaneura_record_accepted_delivery_attempt(uuid)'
               )"""
        ).fetchone()[0]
        if accepted_helper is None:
            conn.execute(
                migration_body(
                    (migrations / "005_accepted_delivery_attempt.up.sql").read_text(
                        encoding="utf-8"
                    )
                ),
                prepare=False,
            )
        revocation_helpers = conn.execute(
            """SELECT
                   to_regprocedure(
                       'public.cosmaneura_cancel_revoked_chase_policy(uuid,text)'
                   ),
                   to_regprocedure(
                       'public.cosmaneura_cancel_stale_chase_work(text)'
                   )"""
        ).fetchone()
        if revocation_helpers == (None, None):
            conn.execute(
                migration_body(
                    (migrations / "006_chase_policy_revocation.up.sql").read_text(
                        encoding="utf-8"
                    )
                ),
                prepare=False,
            )
        elif None in revocation_helpers:
            pytest.fail(
                "disposable database has a partially applied migration 006; "
                "recreate it before running live scheduler tests"
            )
    yield


@dataclass(frozen=True)
class Scope:
    practice_id: str
    tenant_id: str
    admin_id: str
    staff_id: str
    repo: PostgresChaseSchedulerRepository
    base: datetime


@pytest.fixture()
def scope():
    suffix = uuid4().hex
    practice_id = f"p4-repo-test-{suffix}"
    tenant_id = str(uuid4())
    admin_id = str(uuid4())
    staff_id = str(uuid4())
    with psycopg.connect(TEST_DSN) as conn:
        conn.execute(
            "SELECT set_config('cosmaneura.practice_id', %s, true)",
            (practice_id,),
        )
        conn.execute(
            """INSERT INTO practices (
                   practice_id, entra_tenant_id, display_name
               ) VALUES (%s, %s::uuid, 'Synthetic P4 repository')""",
            (practice_id, tenant_id),
        )
        conn.execute(
            """INSERT INTO users (tenant_id, user_id)
               VALUES (%s::uuid, %s::uuid), (%s::uuid, %s::uuid)""",
            (tenant_id, admin_id, tenant_id, staff_id),
        )
        conn.execute(
            """INSERT INTO practice_memberships (
                   practice_id, tenant_id, user_id, roles
               ) VALUES (%s, %s::uuid, %s::uuid, ARRAY['Admin']::text[]),
                        (%s, %s::uuid, %s::uuid,
                         ARRAY['BillingStaff']::text[])""",
            (
                practice_id, tenant_id, admin_id,
                practice_id, tenant_id, staff_id,
            ),
        )
    yield Scope(
        practice_id=practice_id,
        tenant_id=tenant_id,
        admin_id=admin_id,
        staff_id=staff_id,
        repo=PostgresChaseSchedulerRepository(
            TEST_DSN, practice_id=practice_id,
        ),
        # A short real-time offset makes leases deterministically expirable
        # without sleeps while keeping contact evidence fresh.
        base=datetime.now(timezone.utc).replace(microsecond=0)
        - timedelta(minutes=2),
    )


def _policy(*, mode: ApprovalMode = ApprovalMode.PER_POLICY,
            include_phone: bool = True) -> ChasePolicy:
    wanted = {"email.first"}
    steps = [Step(Channel.EMAIL, "email.first", 0)]
    if include_phone:
        wanted.add("phone.call")
        steps.append(Step(Channel.PHONE, "phone.call", 1))
    return ChasePolicy(
        name=f"synthetic-policy-{uuid4().hex}",
        practice_name="Synthetic Practice",
        upload_url="https://upload.example.invalid/insurance",
        approval_mode=mode,
        quiet_hours=_TEST_QUIET_HOURS,
        max_touches=len(steps),
        steps=tuple(steps),
        templates=tuple(
            template for template in DEFAULT_TEMPLATES
            if template.template_id in wanted
        ),
    )


def _email_sms_email_policy() -> ChasePolicy:
    wanted = {"email.first", "sms.first", "email.reminder"}
    return ChasePolicy(
        name=f"synthetic-skip-policy-{uuid4().hex}",
        practice_name="Synthetic Practice",
        upload_url="https://upload.example.invalid/insurance",
        approval_mode=ApprovalMode.PER_POLICY,
        quiet_hours=_TEST_QUIET_HOURS,
        max_touches=3,
        steps=(
            Step(Channel.EMAIL, "email.first", 0),
            Step(Channel.SMS, "sms.first", 1),
            Step(Channel.EMAIL, "email.reminder", 3),
        ),
        templates=tuple(
            template for template in DEFAULT_TEMPLATES
            if template.template_id in wanted
        ),
    )


def _two_email_policy() -> ChasePolicy:
    wanted = {"email.first", "email.reminder"}
    return ChasePolicy(
        name=f"synthetic-two-email-policy-{uuid4().hex}",
        practice_name="Synthetic Practice",
        upload_url="https://upload.example.invalid/insurance",
        approval_mode=ApprovalMode.PER_POLICY,
        quiet_hours=_TEST_QUIET_HOURS,
        max_touches=2,
        steps=(
            Step(Channel.EMAIL, "email.first", 0),
            Step(Channel.EMAIL, "email.reminder", 1),
        ),
        templates=tuple(
            template for template in DEFAULT_TEMPLATES
            if template.template_id in wanted
        ),
    )


def _phone_only_policy() -> ChasePolicy:
    phone_template = next(
        template for template in DEFAULT_TEMPLATES
        if template.template_id == "phone.call"
    )
    return ChasePolicy(
        name=f"synthetic-phone-only-{uuid4().hex}",
        practice_name="Synthetic Practice",
        upload_url="https://upload.example.invalid/insurance",
        approval_mode=ApprovalMode.PER_POLICY,
        quiet_hours=_TEST_QUIET_HOURS,
        max_touches=1,
        steps=(Step(Channel.PHONE, "phone.call", 0),),
        templates=(phone_template,),
    )


def _delivery(*, contact_max_age_hours: int = 24) -> DeliveryConfiguration:
    return DeliveryConfiguration(
        provider="azure_communication_services_email",
        provider_resource="https://synthetic.communication.azure.com",
        enabled_channels=(Channel.EMAIL,),
        sender_address="sender@example.invalid",
        reply_to_address="reply@example.invalid",
        contact_snapshot_max_age_hours=contact_max_age_hours,
        scheduler_enabled=True,
        operational_enabled=True,
    )


def _gate(patient_ref: str, *, blocked: bool = False) -> dict:
    roster = DirectContractList(
        refs=[patient_ref] if blocked else ["SYNTHETIC-OTHER"],
        version="synthetic-v1",
    )
    return check_direct_contract(
        patient_ref,
        roster=roster,
        now=lambda: "2026-08-18T12:00:00+00:00",
    ).to_dict()


def _persist(
    scope: Scope,
    *,
    patient_ref: str = "SYNTHETIC-PATIENT",
    blocked: bool = False,
    state: State | None = None,
) -> tuple[str, str]:
    chase_id = f"chase-{uuid4()}"
    snapshot_id = str(uuid4())
    item = ChaseItem(
        chase_id=chase_id,
        patient_ref=patient_ref,
        trigger=Trigger.NO_CARD_ON_FILE,
        detected_at=scope.base.isoformat(),
        state=state or (State.NEEDS_REVIEW if blocked else State.READY),
        priority=80,
        direct_contract=_gate(patient_ref, blocked=blocked),
        review_reason=(
            "patient is on the direct-contract list — the practice does not bill "
            "their insurance, so confirm whether a card should be chased at all"
            if blocked else ""
        ),
    )
    contact = ContactSnapshotInput(
        contact_snapshot_id=snapshot_id,
        source_system="synthetic_authoritative_feed",
        source_version="v1",
        observed_at=scope.base,
        verified_at=scope.base + timedelta(seconds=1),
        verification_method="staff",
        routes=(
            ContactRouteInput(
                Channel.EMAIL,
                "synthetic.patient@example.invalid",
                "not_required",
            ),
            ContactRouteInput(
                Channel.PHONE,
                "+15555550100",
                "not_required",
            ),
        ),
        verified_by_tenant_id=scope.tenant_id,
        verified_by_user_id=scope.staff_id,
    )
    first = scope.repo.persist_detection(item, contact)
    replay = scope.repo.persist_detection(item, contact)
    assert replay == first
    with psycopg.connect(TEST_DSN) as conn:
        conn.execute(
            "SELECT set_config('cosmaneura.practice_id', %s, true)",
            (scope.practice_id,),
        )
        assert conn.execute(
            """SELECT version FROM chase_items
                WHERE practice_id = %s AND chase_id = %s""",
            (scope.practice_id, first.chase_id),
        ).fetchone()[0] == 0
    return first.chase_id, first.contact_snapshot_id


def _audit_attempt(
    scope: Scope,
    *,
    actor_user_id: str = "",
    actor_roles: tuple[str, ...] = ("BillingStaff",),
    action: str = "chase.decide",
    route: str = "/api/chase/detect",
) -> AccessAuditEvent:
    event = AccessAuditEvent(
        event_id=str(uuid4()),
        occurred_at=datetime.now(timezone.utc).isoformat(),
        practice_id=scope.practice_id,
        actor_tenant_id=scope.tenant_id,
        actor_user_id=actor_user_id or scope.staff_id,
        actor_roles=actor_roles,
        action=action,
        resource_id="",
        route=route,
        method="POST",
        status=200,
        outcome="allowed",
        source_ip="127.0.0.1",
    )
    with psycopg.connect(TEST_DSN) as conn:
        conn.execute(
            """INSERT INTO access_audit_attempts (
                   practice_id, event_id, occurred_at, actor_tenant_id,
                   actor_user_id, actor_roles, action, route, method, source_ip
               ) VALUES (%s, %s::uuid, %s, %s::uuid, %s::uuid, %s,
                         %s, %s, %s, %s)""",
            (
                event.practice_id, event.event_id, event.occurred_at,
                event.actor_tenant_id, event.actor_user_id,
                list(event.actor_roles), event.action, event.route,
                event.method, event.source_ip,
            ),
        )
    return event


def _batch_detection(
    scope: Scope,
    *,
    patient_ref: str,
    chase_id: str,
    snapshot_id: str,
    routes: tuple[ContactRouteInput, ...],
) -> tuple[ChaseItem, ContactSnapshotInput]:
    return (
        ChaseItem(
            chase_id=chase_id,
            patient_ref=patient_ref,
            trigger=Trigger.NO_CARD_ON_FILE,
            detected_at=scope.base.isoformat(),
            state=State.READY,
            priority=80,
            direct_contract=_gate(patient_ref),
        ),
        ContactSnapshotInput(
            contact_snapshot_id=snapshot_id,
            source_system="api.coverage_snapshot",
            source_version="2026-08-18",
            observed_at=scope.base,
            verified_at=scope.base + timedelta(seconds=1),
            verification_method="staff",
            routes=routes,
            verified_by_tenant_id=scope.tenant_id,
            verified_by_user_id=scope.staff_id,
        ),
    )


def test_detection_batch_commits_all_contacts_and_one_audit(scope):
    audit = _audit_attempt(scope)
    first_id, second_id = f"chase-{uuid4()}", f"chase-{uuid4()}"
    first_snapshot, second_snapshot = str(uuid4()), str(uuid4())
    rows = (
        _batch_detection(
            scope,
            patient_ref=f"SYNTHETIC-BATCH-ZERO-{uuid4().hex}",
            chase_id=first_id,
            snapshot_id=first_snapshot,
            routes=(),
        ),
        _batch_detection(
            scope,
            patient_ref=f"SYNTHETIC-BATCH-EMAIL-{uuid4().hex}",
            chase_id=second_id,
            snapshot_id=second_snapshot,
            routes=(ContactRouteInput(
                Channel.EMAIL,
                "synthetic.batch@example.invalid",
                "not_required",
            ),),
        ),
    )

    results = scope.repo.persist_detection_batch(rows, audit_event=audit)

    assert [result.chase_id for result in results] == [first_id, second_id]
    with psycopg.connect(TEST_DSN) as conn:
        item_count = conn.execute(
            """SELECT count(*) FROM chase_items
                WHERE practice_id = %s AND chase_id = ANY(%s)""",
            (scope.practice_id, [first_id, second_id]),
        ).fetchone()[0]
        route_counts = conn.execute(
            """SELECT snapshot.contact_snapshot_id, count(route.channel)
                 FROM chase_contact_snapshots AS snapshot
                 LEFT JOIN chase_contact_routes AS route
                   ON route.practice_id = snapshot.practice_id
                  AND route.contact_snapshot_id = snapshot.contact_snapshot_id
                WHERE snapshot.practice_id = %s
                  AND snapshot.contact_snapshot_id = ANY(%s::uuid[])
                GROUP BY snapshot.contact_snapshot_id""",
            (scope.practice_id, [first_snapshot, second_snapshot]),
        ).fetchall()
        completions = conn.execute(
            """SELECT count(*) FROM access_audit_completions
                WHERE practice_id = %s AND event_id = %s::uuid""",
            (scope.practice_id, audit.event_id),
        ).fetchone()[0]
    assert item_count == 2
    assert {str(row[0]): row[1] for row in route_counts} == {
        first_snapshot: 0,
        second_snapshot: 1,
    }
    assert completions == 1


def test_detection_batch_rolls_back_items_contacts_and_audit(scope):
    audit = _audit_attempt(scope)
    first_id, second_id = f"chase-{uuid4()}", f"chase-{uuid4()}"
    shared_snapshot = str(uuid4())
    rows = (
        _batch_detection(
            scope,
            patient_ref=f"SYNTHETIC-BATCH-ROLLBACK-A-{uuid4().hex}",
            chase_id=first_id,
            snapshot_id=shared_snapshot,
            routes=(),
        ),
        _batch_detection(
            scope,
            patient_ref=f"SYNTHETIC-BATCH-ROLLBACK-B-{uuid4().hex}",
            chase_id=second_id,
            snapshot_id=shared_snapshot,
            routes=(ContactRouteInput(
                Channel.EMAIL,
                "synthetic.conflict@example.invalid",
                "not_required",
            ),),
        ),
    )

    with pytest.raises(SchedulerConflict):
        scope.repo.persist_detection_batch(rows, audit_event=audit)

    with psycopg.connect(TEST_DSN) as conn:
        item_count = conn.execute(
            """SELECT count(*) FROM chase_items
                WHERE practice_id = %s AND chase_id = ANY(%s)""",
            (scope.practice_id, [first_id, second_id]),
        ).fetchone()[0]
        snapshot_count = conn.execute(
            """SELECT count(*) FROM chase_contact_snapshots
                WHERE practice_id = %s AND contact_snapshot_id = %s::uuid""",
            (scope.practice_id, shared_snapshot),
        ).fetchone()[0]
        completion_count = conn.execute(
            """SELECT count(*) FROM access_audit_completions
                WHERE practice_id = %s AND event_id = %s::uuid""",
            (scope.practice_id, audit.event_id),
        ).fetchone()[0]
    assert (item_count, snapshot_count, completion_count) == (0, 0, 0)


def _refresh_gate_detection(
    scope: Scope,
    *,
    chase_id: str,
    patient_ref: str,
    blocked: bool,
    repository: PostgresChaseSchedulerRepository | None = None,
) -> tuple[str, dict]:
    gate = _gate(patient_ref, blocked=blocked)
    verified = datetime.now(timezone.utc) - timedelta(seconds=1)
    snapshot_id = str(uuid4())
    result = (repository or scope.repo).persist_detection(
        ChaseItem(
            chase_id=chase_id,
            patient_ref=patient_ref,
            trigger=Trigger.NO_CARD_ON_FILE,
            detected_at=scope.base.isoformat(),
            state=State.READY,
            priority=80,
            direct_contract=gate,
        ),
        ContactSnapshotInput(
            contact_snapshot_id=snapshot_id,
            source_system="synthetic_gate_refresh",
            source_version="v2",
            observed_at=verified,
            verified_at=verified,
            verification_method="staff",
            routes=(
                ContactRouteInput(
                    Channel.EMAIL,
                    "synthetic.refresh@example.invalid",
                    "not_required",
                ),
                ContactRouteInput(
                    Channel.PHONE,
                    "+15555550101",
                    "not_required",
                ),
            ),
            verified_by_tenant_id=scope.tenant_id,
            verified_by_user_id=scope.staff_id,
        ),
    )
    assert result.chase_id == chase_id
    return result.contact_snapshot_id, gate


def _approve_and_schedule(
    scope: Scope,
    chase_id: str,
    snapshot_id: str,
    policy: ChasePolicy,
    delivery: DeliveryConfiguration,
):
    revision_id = str(uuid4())
    revision = scope.repo.create_policy(
        policy,
        delivery,
        practice_timezone="UTC",
        policy_revision_id=revision_id,
        actor_tenant_id=scope.tenant_id,
        actor_user_id=scope.admin_id,
    )
    replay = scope.repo.create_policy(
        policy,
        delivery,
        practice_timezone="UTC",
        policy_revision_id=revision_id,
        actor_tenant_id=scope.tenant_id,
        actor_user_id=scope.admin_id,
    )
    assert replay == revision
    approval_id = str(uuid4())
    approval = scope.repo.approve_policy(
        policy,
        delivery,
        practice_timezone="UTC",
        approval_event_id=approval_id,
        policy_revision_id=revision.policy_revision_id,
        expected_policy_fingerprint=revision.policy_fingerprint,
        actor_tenant_id=scope.tenant_id,
        actor_user_id=scope.admin_id,
        reason="synthetic approval",
        activate=True,
    )
    assert scope.repo.approve_policy(
        policy,
        delivery,
        practice_timezone="UTC",
        approval_event_id=approval_id,
        policy_revision_id=revision.policy_revision_id,
        expected_policy_fingerprint=revision.policy_fingerprint,
        actor_tenant_id=scope.tenant_id,
        actor_user_id=scope.admin_id,
        reason="synthetic approval",
        activate=True,
    ) == approval
    first_due = cadence_due_at(
        scope.base,
        day_offset=policy.steps[0].day_offset,
        practice_timezone="UTC",
        quiet_hours=policy.quiet_hours,
    )
    scope.repo.schedule_chase(
        chase_id=chase_id,
        policy_revision_id=revision.policy_revision_id,
        expected_policy_fingerprint=revision.policy_fingerprint,
        contact_snapshot_id=snapshot_id,
        next_due_at=first_due,
        as_of=scope.base + timedelta(seconds=5),
    )
    return revision, approval


def _reserve_email(scope: Scope, policy: ChasePolicy,
                   delivery: DeliveryConfiguration):
    reserved = scope.repo.reserve_due(
        policy,
        delivery,
        practice_timezone="UTC",
        as_of=scope.base + timedelta(seconds=6),
    )
    transport = [row for row in reserved if row.action is ScheduleAction.TRANSPORT]
    assert len(transport) == 1
    assert transport[0].delivery_id
    return transport[0]


def test_detection_policy_claim_acceptance_and_phone_task_are_one_exact_flow(scope):
    chase_id, snapshot_id = _persist(scope)
    policy = _policy(include_phone=True)
    delivery = _delivery()
    revision, _ = _approve_and_schedule(
        scope, chase_id, snapshot_id, policy, delivery,
    )
    reservation = _reserve_email(scope, policy, delivery)

    claim = scope.repo.claim_delivery(
        expected_policy_fingerprint=revision.policy_fingerprint,
        contact_max_age_hours=24,
        as_of=scope.base + timedelta(seconds=7),
        lease_seconds=60,
    )
    assert claim is not None
    assert claim.delivery_id == reservation.delivery_id
    assert claim.recipient == "synthetic.patient@example.invalid"
    assert claim.patient_ref == "SYNTHETIC-PATIENT"
    assert claim.gate_fingerprint == _gate("SYNTHETIC-PATIENT")[
        "gate_fingerprint"
    ]
    assert claim.gate_disposition == "clear"
    assert claim.review_release_id == ""
    rendered = repr(claim)
    assert "synthetic.patient" not in rendered
    assert "SYNTHETIC-PATIENT" not in rendered

    next_due = cadence_due_at(
        scope.base,
        day_offset=policy.steps[1].day_offset,
        practice_timezone="UTC",
        quiet_hours=policy.quiet_hours,
    )
    result = scope.repo.finalize_delivery(
        delivery_id=claim.delivery_id,
        lease_token=claim.lease_token,
        outcome="accepted",
        as_of=scope.base + timedelta(seconds=8),
        provider_message_id="synthetic-acs-operation",
    )
    assert result.attempt_recorded is True
    assert result.cadence_advanced is True

    with psycopg.connect(TEST_DSN) as conn:
        conn.execute(
            "SELECT set_config('cosmaneura.practice_id', %s, true)",
            (scope.practice_id,),
        )
        attempt = conn.execute(
            """SELECT delivery_id, channel, outcome FROM chase_attempts
                WHERE practice_id = %s AND chase_id = %s""",
            (scope.practice_id, chase_id),
        ).fetchone()
        cadence = conn.execute(
            """SELECT next_step_ordinal, next_due_at
                 FROM chase_cadence_states
                WHERE practice_id = %s AND chase_id = %s""",
            (scope.practice_id, chase_id),
        ).fetchone()
    assert str(attempt[0]) == claim.delivery_id
    assert attempt[1:] == ("email", "accepted")
    assert cadence == (1, next_due)

    phone = scope.repo.reserve_due(
        policy,
        delivery,
        practice_timezone="UTC",
        as_of=next_due + timedelta(seconds=1),
    )
    assert len(phone) == 1
    assert phone[0].action is ScheduleAction.STAFF_TASK
    with psycopg.connect(TEST_DSN) as conn:
        conn.execute(
            "SELECT set_config('cosmaneura.practice_id', %s, true)",
            (scope.practice_id,),
        )
        payload = conn.execute(
            """SELECT payload FROM chase_pending_tasks
                WHERE practice_id = %s AND chase_id = %s""",
            (scope.practice_id, chase_id),
        ).fetchone()[0]
    assert payload["channel"] == "phone"
    assert payload["policy_step_ordinal"] == 1
    assert "script" not in payload
    assert "destination" not in payload
    assert "upload_url" not in payload

    completed = scope.repo.complete_staff_task(
        policy,
        delivery,
        practice_timezone="UTC",
        chase_id=chase_id,
        action_id=str(uuid4()),
        actor_tenant_id=scope.tenant_id,
        actor_user_id=scope.staff_id,
        outcome="no_answer",
        minutes_spent=2.0,
        note="synthetic call only",
    )
    assert completed.attempt_recorded is True
    assert completed.cadence_advanced is False
    with psycopg.connect(TEST_DSN) as conn:
        conn.execute(
            "SELECT set_config('cosmaneura.practice_id', %s, true)",
            (scope.practice_id,),
        )
        item_state = conn.execute(
            """SELECT state FROM chase_items
                WHERE practice_id = %s AND chase_id = %s""",
            (scope.practice_id, chase_id),
        ).fetchone()[0]
        assert conn.execute(
            """SELECT count(*) FROM chase_pending_tasks
                WHERE practice_id = %s AND chase_id = %s""",
            (scope.practice_id, chase_id),
        ).fetchone()[0] == 0
        assert conn.execute(
            """SELECT count(*) FROM chase_attempts
                WHERE practice_id = %s AND chase_id = %s""",
            (scope.practice_id, chase_id),
        ).fetchone()[0] == 2
    assert item_state == "escalated"


def test_changed_gate_retires_pending_phone_until_exact_review_release(scope):
    patient_ref = f"SYNTHETIC-PHONE-GATE-REFRESH-{uuid4().hex}"
    chase_id, snapshot_id = _persist(scope, patient_ref=patient_ref)
    policy = _phone_only_policy()
    delivery = _delivery()
    revision, approval = _approve_and_schedule(
        scope, chase_id, snapshot_id, policy, delivery,
    )
    queued = scope.repo.reserve_due(
        policy,
        delivery,
        practice_timezone="UTC",
        as_of=scope.base + timedelta(seconds=6),
    )
    assert queued[0].action is ScheduleAction.STAFF_TASK

    _, blocked_gate = _refresh_gate_detection(
        scope,
        chase_id=chase_id,
        patient_ref=patient_ref,
        blocked=True,
    )
    with psycopg.connect(TEST_DSN) as conn:
        conn.execute(
            "SELECT set_config('cosmaneura.practice_id', %s, true)",
            (scope.practice_id,),
        )
        held = conn.execute(
            """SELECT item.state, item.review_reason,
                      cadence.next_step_ordinal
                 FROM chase_items AS item
                 JOIN chase_cadence_states AS cadence
                   ON cadence.practice_id = item.practice_id
                  AND cadence.chase_id = item.chase_id
                WHERE item.practice_id = %s AND item.chase_id = %s""",
            (scope.practice_id, chase_id),
        ).fetchone()
        task = conn.execute(
            """SELECT 1 FROM chase_pending_tasks
                WHERE practice_id = %s AND chase_id = %s""",
            (scope.practice_id, chase_id),
        ).fetchone()
    assert held == (
        "needs_review",
        "patient is on the direct-contract list — the practice does not bill "
        "their insurance, so confirm whether a card should be chased at all",
        0,
    )
    assert task is None
    with pytest.raises(SchedulerConflict, match="no pending staff task"):
        scope.repo.complete_staff_task(
            policy,
            delivery,
            practice_timezone="UTC",
            chase_id=chase_id,
            action_id=str(uuid4()),
            actor_tenant_id=scope.tenant_id,
            actor_user_id=scope.staff_id,
            outcome="no_answer",
        )

    review_id = str(uuid4())
    scope.repo.release_direct_contract(
        review_release_id=review_id,
        chase_id=chase_id,
        gate_fingerprint=blocked_gate["gate_fingerprint"],
        actor_tenant_id=scope.tenant_id,
        actor_user_id=scope.admin_id,
        reason="synthetic refreshed gate release",
    )
    recreated = scope.repo.reserve_due(
        policy,
        delivery,
        practice_timezone="UTC",
        as_of=scope.base + timedelta(seconds=7),
    )
    assert recreated[0].action is ScheduleAction.STAFF_TASK
    with psycopg.connect(TEST_DSN) as conn:
        conn.execute(
            "SELECT set_config('cosmaneura.practice_id', %s, true)",
            (scope.practice_id,),
        )
        payload = conn.execute(
            """SELECT payload FROM chase_pending_tasks
                WHERE practice_id = %s AND chase_id = %s""",
            (scope.practice_id, chase_id),
        ).fetchone()[0]
    assert payload["gate_fingerprint"] == blocked_gate["gate_fingerprint"]
    assert payload["gate_disposition"] == "released"
    assert payload["review_release_id"] == review_id
    assert payload["approval_event_id"] == approval.approval_event_id
    assert payload["policy_fingerprint"] == revision.policy_fingerprint


def test_changed_gate_cancels_reserved_email_and_allows_exact_replacement(scope):
    patient_ref = f"SYNTHETIC-EMAIL-GATE-REFRESH-{uuid4().hex}"
    chase_id, snapshot_id = _persist(scope, patient_ref=patient_ref)
    policy = _policy(include_phone=False)
    delivery = _delivery()
    revision, _ = _approve_and_schedule(
        scope, chase_id, snapshot_id, policy, delivery,
    )
    original = _reserve_email(scope, policy, delivery)
    _, blocked_gate = _refresh_gate_detection(
        scope,
        chase_id=chase_id,
        patient_ref=patient_ref,
        blocked=True,
    )
    with psycopg.connect(TEST_DSN) as conn:
        conn.execute(
            "SELECT set_config('cosmaneura.practice_id', %s, true)",
            (scope.practice_id,),
        )
        cancelled = conn.execute(
            """SELECT state, error_code FROM chase_delivery_outbox
                WHERE practice_id = %s AND delivery_id = %s::uuid""",
            (scope.practice_id, original.delivery_id),
        ).fetchone()
        event = conn.execute(
            """SELECT event_type, error_code FROM chase_delivery_events
                WHERE practice_id = %s AND delivery_id = %s::uuid
                ORDER BY event_no DESC LIMIT 1""",
            (scope.practice_id, original.delivery_id),
        ).fetchone()
        cadence = conn.execute(
            """SELECT next_step_ordinal FROM chase_cadence_states
                WHERE practice_id = %s AND chase_id = %s""",
            (scope.practice_id, chase_id),
        ).fetchone()
    assert cancelled == ("cancelled", "gate_evidence_changed")
    assert event == ("cancelled", "gate_evidence_changed")
    assert cadence == (0,)

    review_id = str(uuid4())
    scope.repo.release_direct_contract(
        review_release_id=review_id,
        chase_id=chase_id,
        gate_fingerprint=blocked_gate["gate_fingerprint"],
        actor_tenant_id=scope.tenant_id,
        actor_user_id=scope.admin_id,
        reason="synthetic refreshed email gate release",
    )
    replacement = _reserve_email(scope, policy, delivery)
    assert replacement.delivery_id != original.delivery_id
    with psycopg.connect(TEST_DSN) as conn:
        conn.execute(
            "SELECT set_config('cosmaneura.practice_id', %s, true)",
            (scope.practice_id,),
        )
        evidence = conn.execute(
            """SELECT gate_fingerprint, gate_disposition, review_release_id
                 FROM chase_delivery_outbox
                WHERE practice_id = %s AND delivery_id = %s::uuid""",
            (scope.practice_id, replacement.delivery_id),
        ).fetchone()
    assert evidence == (
        blocked_gate["gate_fingerprint"], "released", UUID(review_id),
    )


def test_same_gate_fingerprint_refresh_keeps_queued_exact_work(scope):
    patient_ref = f"SYNTHETIC-SAME-GATE-REFRESH-{uuid4().hex}"
    chase_id, snapshot_id = _persist(scope, patient_ref=patient_ref)
    policy = _policy(include_phone=False)
    delivery = _delivery()
    revision, _ = _approve_and_schedule(
        scope, chase_id, snapshot_id, policy, delivery,
    )
    original = _reserve_email(scope, policy, delivery)
    refreshed_gate = dict(_gate(patient_ref))
    refreshed_gate["checked_at"] = "2026-08-18T13:00:00+00:00"
    refreshed_gate["source"] = "in_memory:synthetic-v1-refreshed"
    verified = datetime.now(timezone.utc) - timedelta(seconds=1)
    scope.repo.persist_detection(
        ChaseItem(
            chase_id=chase_id,
            patient_ref=patient_ref,
            trigger=Trigger.NO_CARD_ON_FILE,
            detected_at=scope.base.isoformat(),
            state=State.READY,
            priority=80,
            direct_contract=refreshed_gate,
        ),
        ContactSnapshotInput(
            contact_snapshot_id=str(uuid4()),
            source_system="synthetic_same_gate_refresh",
            source_version="v2",
            observed_at=verified,
            verified_at=verified,
            verification_method="staff",
            routes=(ContactRouteInput(
                Channel.EMAIL,
                "synthetic.same-gate@example.invalid",
                "not_required",
            ),),
            verified_by_tenant_id=scope.tenant_id,
            verified_by_user_id=scope.staff_id,
        ),
    )
    with psycopg.connect(TEST_DSN) as conn:
        conn.execute(
            "SELECT set_config('cosmaneura.practice_id', %s, true)",
            (scope.practice_id,),
        )
        queued = conn.execute(
            """SELECT state, error_code FROM chase_delivery_outbox
                WHERE practice_id = %s AND delivery_id = %s::uuid""",
            (scope.practice_id, original.delivery_id),
        ).fetchone()
        item = conn.execute(
            """SELECT state, direct_contract ->> 'checked_at'
                 FROM chase_items
                WHERE practice_id = %s AND chase_id = %s""",
            (scope.practice_id, chase_id),
        ).fetchone()
    assert queued == ("reserved", None)
    assert item == ("ready", "2026-08-18T13:00:00+00:00")
    claim = scope.repo.claim_delivery(
        expected_policy_fingerprint=revision.policy_fingerprint,
        contact_max_age_hours=24,
        as_of=datetime.now(timezone.utc),
        lease_seconds=60,
    )
    assert claim is not None
    assert claim.delivery_id == original.delivery_id


def test_gate_refresh_serializes_behind_provider_submission_guard(scope):
    patient_ref = f"SYNTHETIC-GATE-GUARD-RACE-{uuid4().hex}"
    chase_id, snapshot_id = _persist(scope, patient_ref=patient_ref)
    policy = _policy(include_phone=False)
    delivery = _delivery()
    revision, _ = _approve_and_schedule(
        scope, chase_id, snapshot_id, policy, delivery,
    )
    _reserve_email(scope, policy, delivery)
    claim = scope.repo.claim_delivery(
        expected_policy_fingerprint=revision.policy_fingerprint,
        contact_max_age_hours=24,
        as_of=datetime.now(timezone.utc),
        lease_seconds=60,
    )
    assert claim is not None
    application_name = f"p4-gate-refresh-guard-{uuid4().hex}"
    race_repo = PostgresChaseSchedulerRepository(
        psycopg.conninfo.make_conninfo(
            TEST_DSN, application_name=application_name,
        ),
        practice_id=scope.practice_id,
    )
    provider_boundary_entered = False
    with ThreadPoolExecutor(max_workers=1) as executor:
        with scope.repo.guard_submission(claim):
            provider_boundary_entered = True
            future = executor.submit(
                _refresh_gate_detection,
                scope,
                chase_id=chase_id,
                patient_ref=patient_ref,
                blocked=True,
                repository=race_repo,
            )
            waited_on_lock = False
            deadline = time.monotonic() + 5
            with psycopg.connect(TEST_DSN, autocommit=True) as observer:
                while time.monotonic() < deadline and not future.done():
                    wait = observer.execute(
                        """SELECT wait_event_type FROM pg_stat_activity
                            WHERE application_name = %s""",
                        (application_name,),
                    ).fetchone()
                    if wait is not None and wait[0] == "Lock":
                        waited_on_lock = True
                        break
                    time.sleep(0.02)
            assert future.done() is False
        future.result(timeout=5)
    assert provider_boundary_entered is True
    assert waited_on_lock is True
    result = scope.repo.finalize_delivery(
        delivery_id=claim.delivery_id,
        lease_token=claim.lease_token,
        outcome="accepted",
        as_of=datetime.now(timezone.utc),
    )
    assert result.attempt_recorded is True
    with psycopg.connect(TEST_DSN) as conn:
        conn.execute(
            "SELECT set_config('cosmaneura.practice_id', %s, true)",
            (scope.practice_id,),
        )
        state = conn.execute(
            """SELECT state FROM chase_items
                WHERE practice_id = %s AND chase_id = %s""",
            (scope.practice_id, chase_id),
        ).fetchone()[0]
        cadence = conn.execute(
            """SELECT next_step_ordinal FROM chase_cadence_states
                WHERE practice_id = %s AND chase_id = %s""",
            (scope.practice_id, chase_id),
        ).fetchone()
    assert state == "needs_review"
    assert cadence == (0,)


def test_permanent_failure_records_no_attempt_and_does_not_advance(scope):
    chase_id, snapshot_id = _persist(scope, patient_ref="SYNTHETIC-FAILURE")
    policy = _policy(include_phone=True)
    delivery = _delivery()
    revision, _ = _approve_and_schedule(
        scope, chase_id, snapshot_id, policy, delivery,
    )
    _reserve_email(scope, policy, delivery)
    claim = scope.repo.claim_delivery(
        expected_policy_fingerprint=revision.policy_fingerprint,
        contact_max_age_hours=24,
        as_of=scope.base + timedelta(seconds=7),
        lease_seconds=60,
    )
    assert claim is not None
    result = scope.repo.finalize_delivery(
        delivery_id=claim.delivery_id,
        lease_token=claim.lease_token,
        outcome="permanent_failure",
        as_of=scope.base + timedelta(seconds=8),
        error_code="invalid_recipient",
    )
    assert result.attempt_recorded is False
    assert result.cadence_advanced is False
    with psycopg.connect(TEST_DSN) as conn:
        conn.execute(
            "SELECT set_config('cosmaneura.practice_id', %s, true)",
            (scope.practice_id,),
        )
        attempt_count = conn.execute(
            """SELECT count(*) FROM chase_attempts
                WHERE practice_id = %s AND chase_id = %s""",
            (scope.practice_id, chase_id),
        ).fetchone()[0]
        cadence = conn.execute(
            """SELECT next_step_ordinal FROM chase_cadence_states
                WHERE practice_id = %s AND chase_id = %s""",
            (scope.practice_id, chase_id),
        ).fetchone()
        state = conn.execute(
            """SELECT state FROM chase_items
                WHERE practice_id = %s AND chase_id = %s""",
            (scope.practice_id, chase_id),
        ).fetchone()[0]
    assert attempt_count == 0
    assert cadence == (0,)
    assert state == "needs_review"


def test_only_unavailable_channel_reason_skips_without_counting_a_touch(scope):
    chase_id, snapshot_id = _persist(scope, patient_ref="SYNTHETIC-SKIP")
    policy = _email_sms_email_policy()
    delivery = _delivery(contact_max_age_hours=168)
    revision, _ = _approve_and_schedule(
        scope, chase_id, snapshot_id, policy, delivery,
    )
    _reserve_email(scope, policy, delivery)
    claim = scope.repo.claim_delivery(
        expected_policy_fingerprint=revision.policy_fingerprint,
        contact_max_age_hours=24,
        as_of=scope.base + timedelta(seconds=7),
        lease_seconds=60,
    )
    assert claim is not None
    sms_due = cadence_due_at(
        scope.base,
        day_offset=1,
        practice_timezone="UTC",
        quiet_hours=policy.quiet_hours,
    )
    scope.repo.finalize_delivery(
        delivery_id=claim.delivery_id,
        lease_token=claim.lease_token,
        outcome="accepted",
        as_of=scope.base + timedelta(seconds=8),
    )
    skipped = scope.repo.reserve_due(
        policy,
        delivery,
        practice_timezone="UTC",
        as_of=sms_due + timedelta(seconds=1),
    )
    assert len(skipped) == 1
    assert skipped[0].action is ScheduleAction.BLOCKED
    assert skipped[0].reason_code.value == "channel_unavailable"

    email_due = cadence_due_at(
        scope.base,
        day_offset=3,
        practice_timezone="UTC",
        quiet_hours=policy.quiet_hours,
    )
    with psycopg.connect(TEST_DSN) as conn:
        conn.execute(
            "SELECT set_config('cosmaneura.practice_id', %s, true)",
            (scope.practice_id,),
        )
        cadence = conn.execute(
            """SELECT next_step_ordinal, next_due_at
                 FROM chase_cadence_states
                WHERE practice_id = %s AND chase_id = %s""",
            (scope.practice_id, chase_id),
        ).fetchone()
        attempts = conn.execute(
            """SELECT count(*) FROM chase_attempts
                WHERE practice_id = %s AND chase_id = %s""",
            (scope.practice_id, chase_id),
        ).fetchone()[0]
    assert cadence == (2, email_due)
    assert attempts == 1
    next_reservation = scope.repo.reserve_due(
        policy,
        delivery,
        practice_timezone="UTC",
        as_of=email_due + timedelta(seconds=1),
    )
    assert next_reservation[0].action is ScheduleAction.TRANSPORT


def test_policy_fingerprint_drift_fails_job_without_skipping_cadence(scope):
    chase_id, snapshot_id = _persist(scope, patient_ref="SYNTHETIC-HARD-BLOCK")
    policy = _policy(include_phone=False)
    approved_delivery = _delivery()
    _approve_and_schedule(
        scope, chase_id, snapshot_id, policy, approved_delivery,
    )
    drifted_delivery = DeliveryConfiguration(
        provider="azure_communication_services_email",
        provider_resource="https://synthetic.communication.azure.com",
        enabled_channels=(Channel.EMAIL,),
        sender_address="changed@example.invalid",
        reply_to_address="reply@example.invalid",
        contact_snapshot_max_age_hours=24,
        scheduler_enabled=True,
        operational_enabled=True,
    )
    with pytest.raises(SchedulerConflict, match="policy_fingerprint_mismatch"):
        scope.repo.reserve_due(
            policy,
            drifted_delivery,
            practice_timezone="UTC",
            as_of=scope.base + timedelta(seconds=6),
        )
    with psycopg.connect(TEST_DSN) as conn:
        conn.execute(
            "SELECT set_config('cosmaneura.practice_id', %s, true)",
            (scope.practice_id,),
        )
        assert conn.execute(
            """SELECT next_step_ordinal FROM chase_cadence_states
                WHERE practice_id = %s AND chase_id = %s""",
            (scope.practice_id, chase_id),
        ).fetchone()[0] == 0


def test_gate_review_preserves_and_resumes_the_exact_later_rung(scope):
    patient_ref = f"SYNTHETIC-GATE-RESUME-{uuid4().hex}"
    chase_id, snapshot_id = _persist(scope, patient_ref=patient_ref)
    policy = _policy(include_phone=True)
    delivery = _delivery(contact_max_age_hours=168)
    revision, _ = _approve_and_schedule(
        scope, chase_id, snapshot_id, policy, delivery,
    )
    _reserve_email(scope, policy, delivery)
    claim = scope.repo.claim_delivery(
        expected_policy_fingerprint=revision.policy_fingerprint,
        contact_max_age_hours=168,
        as_of=scope.base + timedelta(seconds=7),
        lease_seconds=60,
    )
    assert claim is not None
    scope.repo.finalize_delivery(
        delivery_id=claim.delivery_id,
        lease_token=claim.lease_token,
        outcome="accepted",
        as_of=scope.base + timedelta(seconds=8),
    )
    blocked_gate = _gate(patient_ref, blocked=True)
    with psycopg.connect(TEST_DSN) as conn:
        conn.execute(
            "SELECT set_config('cosmaneura.practice_id', %s, true)",
            (scope.practice_id,),
        )
        conn.execute(
            """UPDATE chase_items
                  SET direct_contract = %s, version = version + 1
                WHERE practice_id = %s AND chase_id = %s""",
            (
                psycopg.types.json.Jsonb(blocked_gate),
                scope.practice_id, chase_id,
            ),
        )
        later_due = conn.execute(
            """SELECT next_due_at FROM chase_cadence_states
                WHERE practice_id = %s AND chase_id = %s""",
            (scope.practice_id, chase_id),
        ).fetchone()[0]
    blocked = scope.repo.reserve_due(
        policy,
        delivery,
        practice_timezone="UTC",
        as_of=later_due + timedelta(seconds=1),
    )
    assert blocked[0].reason_code.value == "gate_not_cleared"
    with psycopg.connect(TEST_DSN) as conn:
        conn.execute(
            "SELECT set_config('cosmaneura.practice_id', %s, true)",
            (scope.practice_id,),
        )
        state_and_rung = conn.execute(
            """SELECT item.state, cadence.next_step_ordinal
                 FROM chase_items AS item
                 JOIN chase_cadence_states AS cadence
                   ON cadence.practice_id = item.practice_id
                  AND cadence.chase_id = item.chase_id
                WHERE item.practice_id = %s AND item.chase_id = %s""",
            (scope.practice_id, chase_id),
        ).fetchone()
    assert state_and_rung == ("needs_review", 1)

    scope.repo.release_direct_contract(
        review_release_id=str(uuid4()),
        chase_id=chase_id,
        gate_fingerprint=blocked_gate["gate_fingerprint"],
        actor_tenant_id=scope.tenant_id,
        actor_user_id=scope.admin_id,
        reason="synthetic later-rung review release",
    )
    resumed = scope.repo.reserve_due(
        policy,
        delivery,
        practice_timezone="UTC",
        as_of=later_due + timedelta(seconds=2),
    )
    assert len(resumed) == 1
    assert resumed[0].action is ScheduleAction.STAFF_TASK
    assert resumed[0].policy_step_ordinal == 1


def test_acceptance_during_review_advances_rung_without_clearing_hold(scope):
    patient_ref = f"SYNTHETIC-LATE-ACCEPT-REVIEW-{uuid4().hex}"
    chase_id, snapshot_id = _persist(scope, patient_ref=patient_ref)
    policy = _policy(include_phone=True)
    delivery = _delivery(contact_max_age_hours=168)
    revision, _ = _approve_and_schedule(
        scope, chase_id, snapshot_id, policy, delivery,
    )
    _reserve_email(scope, policy, delivery)
    claim = scope.repo.claim_delivery(
        expected_policy_fingerprint=revision.policy_fingerprint,
        contact_max_age_hours=168,
        as_of=scope.base + timedelta(seconds=7),
        lease_seconds=60,
    )
    assert claim is not None
    blocked_gate = _gate(patient_ref, blocked=True)
    review_reason = (
        "patient is on the direct-contract list — the practice does not bill "
        "their insurance, so confirm whether a card should be chased at all"
    )
    with psycopg.connect(TEST_DSN) as conn:
        conn.execute(
            "SELECT set_config('cosmaneura.practice_id', %s, true)",
            (scope.practice_id,),
        )
        conn.execute(
            """UPDATE chase_items
                  SET state = 'needs_review', direct_contract = %s,
                      review_reason = %s, version = version + 1
                WHERE practice_id = %s AND chase_id = %s""",
            (
                psycopg.types.json.Jsonb(blocked_gate), review_reason,
                scope.practice_id, chase_id,
            ),
        )
    result = scope.repo.finalize_delivery(
        delivery_id=claim.delivery_id,
        lease_token=claim.lease_token,
        outcome="accepted",
        as_of=scope.base + timedelta(seconds=8),
    )
    assert result.attempt_recorded is True
    assert result.cadence_advanced is True
    with psycopg.connect(TEST_DSN) as conn:
        conn.execute(
            "SELECT set_config('cosmaneura.practice_id', %s, true)",
            (scope.practice_id,),
        )
        held = conn.execute(
            """SELECT item.state, item.review_reason,
                      cadence.next_step_ordinal, cadence.next_due_at
                 FROM chase_items AS item
                 JOIN chase_cadence_states AS cadence
                   ON cadence.practice_id = item.practice_id
                  AND cadence.chase_id = item.chase_id
                WHERE item.practice_id = %s AND item.chase_id = %s""",
            (scope.practice_id, chase_id),
        ).fetchone()
    assert held[:3] == ("needs_review", review_reason, 1)
    scope.repo.release_direct_contract(
        review_release_id=str(uuid4()),
        chase_id=chase_id,
        gate_fingerprint=blocked_gate["gate_fingerprint"],
        actor_tenant_id=scope.tenant_id,
        actor_user_id=scope.admin_id,
        reason="synthetic post-acceptance review release",
    )
    resumed = scope.repo.reserve_due(
        policy,
        delivery,
        practice_timezone="UTC",
        as_of=held[3] + timedelta(seconds=1),
    )
    assert len(resumed) == 1
    assert resumed[0].action is ScheduleAction.STAFF_TASK
    assert resumed[0].policy_step_ordinal == 1


def test_final_acceptance_during_review_exhausts_after_exact_release(scope):
    patient_ref = f"SYNTHETIC-FINAL-ACCEPT-REVIEW-{uuid4().hex}"
    chase_id, snapshot_id = _persist(scope, patient_ref=patient_ref)
    policy = _policy(include_phone=False)
    delivery = _delivery()
    revision, _ = _approve_and_schedule(
        scope, chase_id, snapshot_id, policy, delivery,
    )
    _reserve_email(scope, policy, delivery)
    claim = scope.repo.claim_delivery(
        expected_policy_fingerprint=revision.policy_fingerprint,
        contact_max_age_hours=24,
        as_of=scope.base + timedelta(seconds=7),
        lease_seconds=60,
    )
    assert claim is not None
    blocked_gate = _gate(patient_ref, blocked=True)
    review_reason = (
        "patient is on the direct-contract list — the practice does not bill "
        "their insurance, so confirm whether a card should be chased at all"
    )
    with psycopg.connect(TEST_DSN) as conn:
        conn.execute(
            "SELECT set_config('cosmaneura.practice_id', %s, true)",
            (scope.practice_id,),
        )
        conn.execute(
            """UPDATE chase_items
                  SET state = 'needs_review', direct_contract = %s,
                      review_reason = %s, version = version + 1
                WHERE practice_id = %s AND chase_id = %s""",
            (
                psycopg.types.json.Jsonb(blocked_gate), review_reason,
                scope.practice_id, chase_id,
            ),
        )
    finalized_at = scope.base + timedelta(seconds=8)
    result = scope.repo.finalize_delivery(
        delivery_id=claim.delivery_id,
        lease_token=claim.lease_token,
        outcome="accepted",
        as_of=finalized_at,
    )
    assert result.attempt_recorded is True
    assert result.cadence_advanced is False
    with psycopg.connect(TEST_DSN) as conn:
        conn.execute(
            "SELECT set_config('cosmaneura.practice_id', %s, true)",
            (scope.practice_id,),
        )
        assert conn.execute(
            """SELECT next_step_ordinal FROM chase_cadence_states
                WHERE practice_id = %s AND chase_id = %s""",
            (scope.practice_id, chase_id),
        ).fetchone() == (0,)

    scope.repo.release_direct_contract(
        review_release_id=str(uuid4()),
        chase_id=chase_id,
        gate_fingerprint=blocked_gate["gate_fingerprint"],
        actor_tenant_id=scope.tenant_id,
        actor_user_id=scope.admin_id,
        reason="synthetic final-rung review release",
    )
    assert scope.repo.reserve_due(
        policy,
        delivery,
        practice_timezone="UTC",
        as_of=scope.base + timedelta(seconds=9),
    ) == []
    assert scope.repo.schedule_unscheduled(
        policy,
        delivery,
        practice_timezone="UTC",
        as_of=scope.base + timedelta(seconds=10),
    ) == 0
    with psycopg.connect(TEST_DSN) as conn:
        conn.execute(
            "SELECT set_config('cosmaneura.practice_id', %s, true)",
            (scope.practice_id,),
        )
        item = conn.execute(
            """SELECT state, closed_at, review_reason FROM chase_items
                WHERE practice_id = %s AND chase_id = %s""",
            (scope.practice_id, chase_id),
        ).fetchone()
        cadence = conn.execute(
            """SELECT 1 FROM chase_cadence_states
                WHERE practice_id = %s AND chase_id = %s""",
            (scope.practice_id, chase_id),
        ).fetchone()
        attempts = conn.execute(
            """SELECT count(*) FROM chase_attempts
                WHERE practice_id = %s AND chase_id = %s""",
            (scope.practice_id, chase_id),
        ).fetchone()[0]
    assert item == (
        "escalated", finalized_at.isoformat(), "approved cadence exhausted",
    )
    assert cadence is None
    assert attempts == 1


def test_permanent_failure_preserves_exact_rung_for_reviewed_retry(scope):
    chase_id, snapshot_id = _persist(
        scope, patient_ref=f"SYNTHETIC-FAILURE-RESUME-{uuid4().hex}",
    )
    policy = _two_email_policy()
    delivery = _delivery(contact_max_age_hours=168)
    revision, _ = _approve_and_schedule(
        scope, chase_id, snapshot_id, policy, delivery,
    )
    _reserve_email(scope, policy, delivery)
    first_claim = scope.repo.claim_delivery(
        expected_policy_fingerprint=revision.policy_fingerprint,
        contact_max_age_hours=168,
        as_of=scope.base + timedelta(seconds=7),
        lease_seconds=60,
    )
    assert first_claim is not None
    scope.repo.finalize_delivery(
        delivery_id=first_claim.delivery_id,
        lease_token=first_claim.lease_token,
        outcome="accepted",
        as_of=scope.base + timedelta(seconds=8),
    )
    with psycopg.connect(TEST_DSN) as conn:
        conn.execute(
            "SELECT set_config('cosmaneura.practice_id', %s, true)",
            (scope.practice_id,),
        )
        later_due = conn.execute(
            """SELECT next_due_at FROM chase_cadence_states
                WHERE practice_id = %s AND chase_id = %s""",
            (scope.practice_id, chase_id),
        ).fetchone()[0]
    second = scope.repo.reserve_due(
        policy,
        delivery,
        practice_timezone="UTC",
        as_of=later_due + timedelta(seconds=1),
    )
    assert second[0].policy_step_ordinal == 1
    second_claim = scope.repo.claim_delivery(
        expected_policy_fingerprint=revision.policy_fingerprint,
        contact_max_age_hours=168,
        as_of=later_due + timedelta(seconds=2),
        lease_seconds=60,
    )
    assert second_claim is not None
    scope.repo.finalize_delivery(
        delivery_id=second_claim.delivery_id,
        lease_token=second_claim.lease_token,
        outcome="permanent_failure",
        as_of=later_due + timedelta(seconds=3),
        error_code="invalid_recipient",
    )
    with psycopg.connect(TEST_DSN) as conn:
        conn.execute(
            "SELECT set_config('cosmaneura.practice_id', %s, true)",
            (scope.practice_id,),
        )
        held = conn.execute(
            """SELECT item.state, cadence.next_step_ordinal
                 FROM chase_items AS item
                 JOIN chase_cadence_states AS cadence
                   ON cadence.practice_id = item.practice_id
                  AND cadence.chase_id = item.chase_id
                WHERE item.practice_id = %s AND item.chase_id = %s""",
            (scope.practice_id, chase_id),
        ).fetchone()
        conn.execute(
            """UPDATE chase_items
                  SET state = 'ready', review_reason = '',
                      version = version + 1
                WHERE practice_id = %s AND chase_id = %s""",
            (scope.practice_id, chase_id),
        )
    assert held == ("needs_review", 1)
    replacement = scope.repo.reserve_due(
        policy,
        delivery,
        practice_timezone="UTC",
        as_of=later_due + timedelta(seconds=4),
    )
    assert len(replacement) == 1
    assert replacement[0].action is ScheduleAction.TRANSPORT
    assert replacement[0].policy_step_ordinal == 1
    assert replacement[0].delivery_id != second_claim.delivery_id


def test_expired_claim_is_ambiguous_never_reclaimed_and_reconciles_once(scope):
    chase_id, snapshot_id = _persist(scope, patient_ref="SYNTHETIC-AMBIGUOUS")
    policy = _policy(include_phone=False)
    delivery = _delivery()
    revision, _ = _approve_and_schedule(
        scope, chase_id, snapshot_id, policy, delivery,
    )
    _reserve_email(scope, policy, delivery)
    claim = scope.repo.claim_delivery(
        expected_policy_fingerprint=revision.policy_fingerprint,
        contact_max_age_hours=24,
        as_of=scope.base + timedelta(seconds=7),
        lease_seconds=1,
    )
    assert claim is not None
    second_ref = f"SYNTHETIC-AFTER-AMBIGUOUS-{uuid4().hex}"
    second_id, second_snapshot_id = _persist(scope, patient_ref=second_ref)
    second_due = cadence_due_at(
        scope.base,
        day_offset=policy.steps[0].day_offset,
        practice_timezone="UTC",
        quiet_hours=policy.quiet_hours,
    )
    scope.repo.schedule_chase(
        chase_id=second_id,
        policy_revision_id=revision.policy_revision_id,
        expected_policy_fingerprint=revision.policy_fingerprint,
        contact_snapshot_id=second_snapshot_id,
        next_due_at=second_due,
        as_of=scope.base + timedelta(seconds=8),
    )
    scope.repo.reserve_due(
        policy,
        delivery,
        practice_timezone="UTC",
        as_of=scope.base + timedelta(seconds=9),
    )
    quarantined = scope.repo.quarantine_expired_submissions(
        as_of=scope.base + timedelta(seconds=10),
    )
    assert quarantined == [claim.delivery_id]
    assert scope.repo.claim_delivery(
        expected_policy_fingerprint=revision.policy_fingerprint,
        contact_max_age_hours=24,
        as_of=scope.base + timedelta(seconds=11),
        lease_seconds=60,
    ) is None
    ambiguous = scope.repo.list_ambiguous()
    assert [row.delivery_id for row in ambiguous] == [claim.delivery_id]
    result = scope.repo.reconcile_ambiguous(
        delivery_id=claim.delivery_id,
        outcome="accepted",
        as_of=scope.base + timedelta(seconds=12),
    )
    assert result.attempt_recorded is True
    assert result.cadence_advanced is False
    replay = scope.repo.reconcile_ambiguous(
        delivery_id=claim.delivery_id,
        outcome="accepted",
        as_of=scope.base + timedelta(seconds=13),
    )
    assert replay.attempt_recorded is True
    resumed = scope.repo.claim_delivery(
        expected_policy_fingerprint=revision.policy_fingerprint,
        contact_max_age_hours=24,
        as_of=scope.base + timedelta(seconds=14),
        lease_seconds=60,
    )
    assert resumed is not None
    assert resumed.patient_ref == second_ref
    with psycopg.connect(TEST_DSN) as conn:
        conn.execute(
            "SELECT set_config('cosmaneura.practice_id', %s, true)",
            (scope.practice_id,),
        )
        assert conn.execute(
            """SELECT count(*) FROM chase_attempts
                WHERE practice_id = %s AND chase_id = %s""",
            (scope.practice_id, chase_id),
        ).fetchone()[0] == 1


def test_exact_human_releases_are_idempotent_and_revocation_stops_claim(scope):
    patient_ref = "SYNTHETIC-REVIEW"
    chase_id, snapshot_id = _persist(
        scope, patient_ref=patient_ref, blocked=True,
    )
    policy = _policy(
        mode=ApprovalMode.PER_MESSAGE,
        include_phone=False,
    )
    delivery = _delivery()
    revision, _ = _approve_and_schedule(
        scope, chase_id, snapshot_id, policy, delivery,
    )
    gate = _gate(patient_ref, blocked=True)["gate_fingerprint"]
    review_id = str(uuid4())
    review = scope.repo.release_direct_contract(
        review_release_id=review_id,
        chase_id=chase_id,
        gate_fingerprint=gate,
        actor_tenant_id=scope.tenant_id,
        actor_user_id=scope.admin_id,
        reason="synthetic exact review",
    )
    assert scope.repo.release_direct_contract(
        review_release_id=review_id,
        chase_id=chase_id,
        gate_fingerprint=gate,
        actor_tenant_id=scope.tenant_id,
        actor_user_id=scope.admin_id,
        reason="synthetic exact review",
    ) == review
    assert scope.repo.reserve_due(
        policy,
        delivery,
        practice_timezone="UTC",
        as_of=datetime.now(timezone.utc),
    ) == []
    message_id = str(uuid4())
    message = scope.repo.release_message(
        release_id=message_id,
        chase_id=chase_id,
        policy_revision_id=revision.policy_revision_id,
        expected_policy_fingerprint=revision.policy_fingerprint,
        step_ordinal=0,
        contact_snapshot_id=snapshot_id,
        actor_tenant_id=scope.tenant_id,
        actor_user_id=scope.admin_id,
        reason="synthetic message release",
    )
    assert scope.repo.release_message(
        release_id=message_id,
        chase_id=chase_id,
        policy_revision_id=revision.policy_revision_id,
        expected_policy_fingerprint=revision.policy_fingerprint,
        step_ordinal=0,
        contact_snapshot_id=snapshot_id,
        actor_tenant_id=scope.tenant_id,
        actor_user_id=scope.admin_id,
        reason="synthetic message release",
    ) == message
    reservation_at = datetime.now(timezone.utc) + timedelta(seconds=1)
    reservation = scope.repo.reserve_due(
        policy,
        delivery,
        practice_timezone="UTC",
        as_of=reservation_at,
    )
    assert reservation[0].action is ScheduleAction.TRANSPORT
    claim = scope.repo.claim_delivery(
        expected_policy_fingerprint=revision.policy_fingerprint,
        contact_max_age_hours=24,
        as_of=reservation_at + timedelta(seconds=1),
        lease_seconds=60,
    )
    assert claim is not None
    assert claim.gate_disposition == "released"
    assert claim.review_release_id == review_id

    scope.repo.revoke_policy(
        approval_event_id=str(uuid4()),
        policy_revision_id=revision.policy_revision_id,
        expected_policy_fingerprint=revision.policy_fingerprint,
        actor_tenant_id=scope.tenant_id,
        actor_user_id=scope.admin_id,
        reason="synthetic revocation",
    )
    assert scope.repo.claim_delivery(
        expected_policy_fingerprint=revision.policy_fingerprint,
        contact_max_age_hours=24,
        as_of=reservation_at + timedelta(seconds=2),
        lease_seconds=60,
    ) is None


def test_message_release_exact_retry_survives_cadence_advance(scope):
    chase_id, snapshot_id = _persist(
        scope, patient_ref=f"SYNTHETIC-MESSAGE-RETRY-{uuid4().hex}",
    )
    policy = _policy(mode=ApprovalMode.PER_MESSAGE, include_phone=True)
    delivery = _delivery()
    revision, _ = _approve_and_schedule(
        scope, chase_id, snapshot_id, policy, delivery,
    )
    release_id = str(uuid4())
    reason = "practice owner reviewed this exact synthetic message"
    target = scope.repo.message_release_target(
        release_id=release_id,
        chase_id=chase_id,
        expected_policy_fingerprint=revision.policy_fingerprint,
        actor_tenant_id=scope.tenant_id,
        actor_user_id=scope.admin_id,
        reason=reason,
    )
    assert target == {
        "policy_revision_id": revision.policy_revision_id,
        "contact_snapshot_id": snapshot_id,
        "step_ordinal": 0,
    }
    released = scope.repo.release_message(
        release_id=release_id,
        chase_id=chase_id,
        policy_revision_id=target["policy_revision_id"],
        expected_policy_fingerprint=revision.policy_fingerprint,
        step_ordinal=target["step_ordinal"],
        contact_snapshot_id=target["contact_snapshot_id"],
        actor_tenant_id=scope.tenant_id,
        actor_user_id=scope.admin_id,
        reason=reason,
    )
    now = datetime.now(timezone.utc)
    [reserved] = scope.repo.reserve_due(
        policy,
        delivery,
        practice_timezone="UTC",
        as_of=now + timedelta(seconds=1),
    )
    assert reserved.action is ScheduleAction.TRANSPORT
    claim = scope.repo.claim_delivery(
        expected_policy_fingerprint=revision.policy_fingerprint,
        contact_max_age_hours=24,
        as_of=now + timedelta(seconds=2),
        lease_seconds=60,
    )
    assert claim is not None
    finalized = scope.repo.finalize_delivery(
        delivery_id=claim.delivery_id,
        lease_token=claim.lease_token,
        outcome="accepted",
        as_of=now + timedelta(seconds=3),
        provider_message_id=f"synthetic-retry-{uuid4()}",
    )
    assert finalized.cadence_advanced is True

    retry_audit = _audit_attempt(
        scope,
        actor_user_id=scope.admin_id,
        actor_roles=("Admin",),
        action="chase.decide",
        route="/api/chase/release-message",
    )
    retry_target = scope.repo.message_release_target(
        release_id=release_id,
        chase_id=chase_id,
        expected_policy_fingerprint=revision.policy_fingerprint,
        actor_tenant_id=scope.tenant_id,
        actor_user_id=scope.admin_id,
        reason=reason,
    )
    assert retry_target == target
    replay = scope.repo.release_message(
        release_id=release_id,
        chase_id=chase_id,
        policy_revision_id=retry_target["policy_revision_id"],
        expected_policy_fingerprint=revision.policy_fingerprint,
        step_ordinal=retry_target["step_ordinal"],
        contact_snapshot_id=retry_target["contact_snapshot_id"],
        actor_tenant_id=scope.tenant_id,
        actor_user_id=scope.admin_id,
        reason=reason,
        audit_event=retry_audit,
    )
    assert replay == released
    with psycopg.connect(TEST_DSN) as conn:
        cadence_ordinal = conn.execute(
            """SELECT next_step_ordinal FROM chase_cadence_states
                WHERE practice_id = %s AND chase_id = %s""",
            (scope.practice_id, chase_id),
        ).fetchone()[0]
        completion_count = conn.execute(
            """SELECT count(*) FROM access_audit_completions
                WHERE practice_id = %s AND event_id = %s::uuid""",
            (scope.practice_id, retry_audit.event_id),
        ).fetchone()[0]
    assert cadence_ordinal == 1
    assert completion_count == 1

    with pytest.raises(SchedulerConflict):
        scope.repo.message_release_target(
            release_id=release_id,
            chase_id=chase_id,
            expected_policy_fingerprint=revision.policy_fingerprint,
            actor_tenant_id=scope.tenant_id,
            actor_user_id=scope.admin_id,
            reason="a changed reason must not replay",
        )
    with pytest.raises(SchedulerConflict):
        scope.repo.message_release_target(
            release_id=release_id,
            chase_id=f"different-path-{uuid4()}",
            expected_policy_fingerprint=revision.policy_fingerprint,
            actor_tenant_id=scope.tenant_id,
            actor_user_id=scope.admin_id,
            reason=reason,
        )

    second_admin = str(uuid4())
    with psycopg.connect(TEST_DSN) as conn:
        conn.execute(
            "INSERT INTO users (tenant_id, user_id) VALUES (%s::uuid, %s::uuid)",
            (scope.tenant_id, second_admin),
        )
        conn.execute(
            """INSERT INTO practice_memberships (
                   practice_id, tenant_id, user_id, roles
               ) VALUES (%s, %s::uuid, %s::uuid, ARRAY['Admin']::text[])""",
            (scope.practice_id, scope.tenant_id, second_admin),
        )
    with pytest.raises(SchedulerConflict):
        scope.repo.message_release_target(
            release_id=release_id,
            chase_id=chase_id,
            expected_policy_fingerprint=revision.policy_fingerprint,
            actor_tenant_id=scope.tenant_id,
            actor_user_id=second_admin,
            reason=reason,
        )


def test_contact_snapshot_uuid_conflict_fails_closed(scope):
    patient_ref = "SYNTHETIC-CONFLICT"
    chase_id = f"chase-{uuid4()}"
    snapshot_id = str(uuid4())
    item = ChaseItem(
        chase_id=chase_id,
        patient_ref=patient_ref,
        trigger=Trigger.NO_CARD_ON_FILE,
        detected_at=scope.base.isoformat(),
        state=State.READY,
        direct_contract=_gate(patient_ref),
    )
    first = ContactSnapshotInput(
        contact_snapshot_id=snapshot_id,
        source_system="synthetic",
        source_version="v1",
        observed_at=scope.base,
        verified_at=scope.base,
        verification_method="staff",
        routes=(ContactRouteInput(
            Channel.EMAIL, "first@example.invalid", "not_required",
        ),),
        verified_by_tenant_id=scope.tenant_id,
        verified_by_user_id=scope.staff_id,
    )
    scope.repo.persist_detection(item, first)
    changed = ContactSnapshotInput(
        **{
            **first.__dict__,
            "routes": (ContactRouteInput(
                Channel.EMAIL, "changed@example.invalid", "not_required",
            ),),
        }
    )
    with pytest.raises(SchedulerConflict):
        scope.repo.persist_detection(item, changed)


@pytest.mark.parametrize("outcome", ["accepted", "permanent_failure"])
def test_in_flight_finalization_preserves_verified_closed(scope, outcome):
    chase_id, snapshot_id = _persist(
        scope, patient_ref=f"SYNTHETIC-CLOSED-{outcome}",
    )
    policy = _policy(include_phone=False)
    delivery = _delivery()
    revision, _ = _approve_and_schedule(
        scope, chase_id, snapshot_id, policy, delivery,
    )
    _reserve_email(scope, policy, delivery)
    claim = scope.repo.claim_delivery(
        expected_policy_fingerprint=revision.policy_fingerprint,
        contact_max_age_hours=24,
        as_of=scope.base + timedelta(seconds=7),
        lease_seconds=60,
    )
    assert claim is not None
    with psycopg.connect(TEST_DSN) as conn:
        conn.execute(
            "SELECT set_config('cosmaneura.practice_id', %s, true)",
            (scope.practice_id,),
        )
        conn.execute(
            """UPDATE chase_items
                  SET state = 'verified_closed', closed_at = %s,
                      review_reason = 'synthetic verified close',
                      version = version + 1
                WHERE practice_id = %s AND chase_id = %s""",
            (
                (scope.base + timedelta(seconds=8)).isoformat(),
                scope.practice_id, chase_id,
            ),
        )
    kwargs = (
        {"error_code": "invalid_recipient"}
        if outcome == "permanent_failure" else {}
    )
    result = scope.repo.finalize_delivery(
        delivery_id=claim.delivery_id,
        lease_token=claim.lease_token,
        outcome=outcome,
        as_of=scope.base + timedelta(seconds=9),
        **kwargs,
    )
    assert result.cadence_advanced is False
    assert result.attempt_recorded is (outcome == "accepted")
    with psycopg.connect(TEST_DSN) as conn:
        conn.execute(
            "SELECT set_config('cosmaneura.practice_id', %s, true)",
            (scope.practice_id,),
        )
        state = conn.execute(
            """SELECT state, review_reason FROM chase_items
                WHERE practice_id = %s AND chase_id = %s""",
            (scope.practice_id, chase_id),
        ).fetchone()
        cadence = conn.execute(
            """SELECT 1 FROM chase_cadence_states
                WHERE practice_id = %s AND chase_id = %s""",
            (scope.practice_id, chase_id),
        ).fetchone()
        attempts = conn.execute(
            """SELECT count(*) FROM chase_attempts
                WHERE practice_id = %s AND chase_id = %s""",
            (scope.practice_id, chase_id),
        ).fetchone()[0]
    assert state == ("verified_closed", "synthetic verified close")
    assert cadence is None
    assert attempts == (1 if outcome == "accepted" else 0)


def test_late_phone_log_preserves_verified_closed_and_retires_cadence(scope):
    chase_id, snapshot_id = _persist(
        scope, patient_ref="SYNTHETIC-CLOSED-PHONE",
    )
    policy = _policy(include_phone=True)
    delivery = _delivery()
    revision, _ = _approve_and_schedule(
        scope, chase_id, snapshot_id, policy, delivery,
    )
    _reserve_email(scope, policy, delivery)
    claim = scope.repo.claim_delivery(
        expected_policy_fingerprint=revision.policy_fingerprint,
        contact_max_age_hours=24,
        as_of=scope.base + timedelta(seconds=7),
        lease_seconds=60,
    )
    assert claim is not None
    phone_due = cadence_due_at(
        scope.base,
        day_offset=policy.steps[1].day_offset,
        practice_timezone="UTC",
        quiet_hours=policy.quiet_hours,
    )
    scope.repo.finalize_delivery(
        delivery_id=claim.delivery_id,
        lease_token=claim.lease_token,
        outcome="accepted",
        as_of=scope.base + timedelta(seconds=8),
    )
    reserved = scope.repo.reserve_due(
        policy,
        delivery,
        practice_timezone="UTC",
        as_of=phone_due + timedelta(seconds=1),
    )
    assert reserved[0].action is ScheduleAction.STAFF_TASK
    with psycopg.connect(TEST_DSN) as conn:
        conn.execute(
            "SELECT set_config('cosmaneura.practice_id', %s, true)",
            (scope.practice_id,),
        )
        conn.execute(
            """UPDATE chase_items
                  SET state = 'verified_closed', closed_at = %s,
                      review_reason = 'synthetic verified close',
                      version = version + 1
                WHERE practice_id = %s AND chase_id = %s""",
            (
                (phone_due + timedelta(seconds=2)).isoformat(),
                scope.practice_id, chase_id,
            ),
        )
    completed = scope.repo.complete_staff_task(
        policy,
        delivery,
        practice_timezone="UTC",
        chase_id=chase_id,
        action_id=str(uuid4()),
        actor_tenant_id=scope.tenant_id,
        actor_user_id=scope.staff_id,
        outcome="no_answer",
        minutes_spent=1.0,
        note="synthetic late log",
    )
    assert completed.attempt_recorded is True
    assert completed.cadence_advanced is False
    with psycopg.connect(TEST_DSN) as conn:
        conn.execute(
            "SELECT set_config('cosmaneura.practice_id', %s, true)",
            (scope.practice_id,),
        )
        state = conn.execute(
            """SELECT state, review_reason FROM chase_items
                WHERE practice_id = %s AND chase_id = %s""",
            (scope.practice_id, chase_id),
        ).fetchone()
        pending = conn.execute(
            """SELECT 1 FROM chase_pending_tasks
                WHERE practice_id = %s AND chase_id = %s""",
            (scope.practice_id, chase_id),
        ).fetchone()
        cadence = conn.execute(
            """SELECT 1 FROM chase_cadence_states
                WHERE practice_id = %s AND chase_id = %s""",
            (scope.practice_id, chase_id),
        ).fetchone()
        attempts = conn.execute(
            """SELECT count(*) FROM chase_attempts
                WHERE practice_id = %s AND chase_id = %s""",
            (scope.practice_id, chase_id),
        ).fetchone()[0]
    assert state == ("verified_closed", "synthetic verified close")
    assert pending is None
    assert cadence is None
    assert attempts == 2


def test_pending_phone_completion_rechecks_current_active_policy(scope):
    chase_id, snapshot_id = _persist(
        scope, patient_ref=f"SYNTHETIC-REVOKED-PHONE-{uuid4().hex}",
    )
    policy = _policy(include_phone=True)
    delivery = _delivery(contact_max_age_hours=168)
    revision, _ = _approve_and_schedule(
        scope, chase_id, snapshot_id, policy, delivery,
    )
    _reserve_email(scope, policy, delivery)
    claim = scope.repo.claim_delivery(
        expected_policy_fingerprint=revision.policy_fingerprint,
        contact_max_age_hours=24,
        as_of=scope.base + timedelta(seconds=7),
        lease_seconds=60,
    )
    assert claim is not None
    scope.repo.finalize_delivery(
        delivery_id=claim.delivery_id,
        lease_token=claim.lease_token,
        outcome="accepted",
        as_of=scope.base + timedelta(seconds=8),
    )
    phone_due = cadence_due_at(
        scope.base,
        day_offset=policy.steps[1].day_offset,
        practice_timezone="UTC",
        quiet_hours=policy.quiet_hours,
    )
    reserved = scope.repo.reserve_due(
        policy,
        delivery,
        practice_timezone="UTC",
        as_of=phone_due + timedelta(seconds=1),
    )
    assert reserved[0].action is ScheduleAction.STAFF_TASK
    scope.repo.revoke_policy(
        approval_event_id=str(uuid4()),
        policy_revision_id=revision.policy_revision_id,
        expected_policy_fingerprint=revision.policy_fingerprint,
        actor_tenant_id=scope.tenant_id,
        actor_user_id=scope.admin_id,
        reason="synthetic revocation before call log",
    )
    with pytest.raises(SchedulerConflict, match="no pending staff task"):
        scope.repo.complete_staff_task(
            policy,
            delivery,
            practice_timezone="UTC",
            chase_id=chase_id,
            action_id=str(uuid4()),
            actor_tenant_id=scope.tenant_id,
            actor_user_id=scope.staff_id,
            outcome="no_answer",
        )
    with psycopg.connect(TEST_DSN) as conn:
        conn.execute(
            "SELECT set_config('cosmaneura.practice_id', %s, true)",
            (scope.practice_id,),
        )
        assert conn.execute(
            """SELECT count(*) FROM chase_pending_tasks
                WHERE practice_id = %s AND chase_id = %s""",
            (scope.practice_id, chase_id),
        ).fetchone()[0] == 0
        assert conn.execute(
            """SELECT count(*) FROM chase_attempts
                WHERE practice_id = %s AND chase_id = %s""",
            (scope.practice_id, chase_id),
        ).fetchone()[0] == 1


def test_direct_contract_release_cannot_clear_an_unrelated_review(scope):
    chase_id, _ = _persist(
        scope, patient_ref="SYNTHETIC-OTHER-HOLD", blocked=True,
    )
    gate = _gate("SYNTHETIC-OTHER-HOLD", blocked=True)["gate_fingerprint"]
    with psycopg.connect(TEST_DSN) as conn:
        conn.execute(
            "SELECT set_config('cosmaneura.practice_id', %s, true)",
            (scope.practice_id,),
        )
        conn.execute(
            """UPDATE chase_items
                  SET review_reason = 'automated delivery failed: synthetic',
                      version = version + 1
                WHERE practice_id = %s AND chase_id = %s""",
            (scope.practice_id, chase_id),
        )
    with pytest.raises(SchedulerConflict):
        scope.repo.release_direct_contract(
            review_release_id=str(uuid4()),
            chase_id=chase_id,
            gate_fingerprint=gate,
            actor_tenant_id=scope.tenant_id,
            actor_user_id=scope.admin_id,
            reason="must not clear the unrelated hold",
        )
    with psycopg.connect(TEST_DSN) as conn:
        conn.execute(
            "SELECT set_config('cosmaneura.practice_id', %s, true)",
            (scope.practice_id,),
        )
        assert conn.execute(
            """SELECT state, review_reason FROM chase_items
                WHERE practice_id = %s AND chase_id = %s""",
            (scope.practice_id, chase_id),
        ).fetchone() == (
            "needs_review", "automated delivery failed: synthetic",
        )
        assert conn.execute(
            """SELECT count(*) FROM chase_review_releases
                WHERE practice_id = %s AND chase_id = %s""",
            (scope.practice_id, chase_id),
        ).fetchone()[0] == 0


def test_policy_approval_rolls_back_when_stored_steps_are_not_canonical(scope):
    policy = _policy(include_phone=False)
    delivery = _delivery()
    revision = scope.repo.create_policy(
        policy,
        delivery,
        practice_timezone="UTC",
        policy_revision_id=str(uuid4()),
        actor_tenant_id=scope.tenant_id,
        actor_user_id=scope.admin_id,
    )
    with psycopg.connect(TEST_DSN) as conn:
        conn.execute(
            "SELECT set_config('cosmaneura.practice_id', %s, true)",
            (scope.practice_id,),
        )
        template_fingerprint = conn.execute(
            """SELECT template_fingerprint FROM chase_policy_steps
                WHERE practice_id = %s AND policy_revision_id = %s::uuid
                  AND step_ordinal = 0""",
            (scope.practice_id, revision.policy_revision_id),
        ).fetchone()[0]
        conn.execute(
            """INSERT INTO chase_policy_steps (
                   practice_id, policy_revision_id, step_ordinal, channel,
                   template_id, day_offset, performer, template_fingerprint
               ) VALUES (%s, %s::uuid, 1, 'email', 'email.first', 1,
                         'system', %s)""",
            (
                scope.practice_id, revision.policy_revision_id,
                template_fingerprint,
            ),
        )
    with pytest.raises(SchedulerConflict):
        scope.repo.approve_policy(
            policy,
            delivery,
            practice_timezone="UTC",
            approval_event_id=str(uuid4()),
            policy_revision_id=revision.policy_revision_id,
            expected_policy_fingerprint=revision.policy_fingerprint,
            actor_tenant_id=scope.tenant_id,
            actor_user_id=scope.admin_id,
            reason="must validate exact durable steps",
        )
    with psycopg.connect(TEST_DSN) as conn:
        conn.execute(
            "SELECT set_config('cosmaneura.practice_id', %s, true)",
            (scope.practice_id,),
        )
        assert conn.execute(
            """SELECT count(*) FROM chase_policy_approval_events
                WHERE practice_id = %s AND policy_revision_id = %s::uuid""",
            (scope.practice_id, revision.policy_revision_id),
        ).fetchone()[0] == 0


def test_posted_contact_cannot_assert_authoritative_or_sms_opt_in(scope):
    patient_ref = "SYNTHETIC-CONSENT"
    item = ChaseItem(
        chase_id=f"chase-{uuid4()}",
        patient_ref=patient_ref,
        trigger=Trigger.NO_CARD_ON_FILE,
        detected_at=scope.base.isoformat(),
        state=State.READY,
        direct_contract=_gate(patient_ref),
    )
    common = {
        "contact_snapshot_id": str(uuid4()),
        "source_system": "synthetic",
        "source_version": "v1",
        "observed_at": scope.base,
        "verified_at": scope.base,
        "routes": (ContactRouteInput(
            Channel.EMAIL, "synthetic@example.invalid", "not_required",
        ),),
        "verified_by_tenant_id": scope.tenant_id,
        "verified_by_user_id": scope.staff_id,
    }
    with pytest.raises(ValueError, match="requires staff verification"):
        scope.repo.persist_detection(
            item,
            ContactSnapshotInput(
                **common, verification_method="authoritative_source",
            ),
        )
    sms = {
        **common,
        "contact_snapshot_id": str(uuid4()),
        "verification_method": "staff",
        "routes": (ContactRouteInput(
            Channel.SMS,
            "+15555550100",
            "granted",
            consent_source="staff",
            consent_recorded_at=scope.base,
        ),),
    }
    with pytest.raises(ValueError, match="cannot create SMS opt-in"):
        scope.repo.persist_detection(item, ContactSnapshotInput(**sms))


def test_worker_initializes_only_eligible_unscheduled_rows_with_latest_contact(scope):
    ready_ref = f"SYNTHETIC-INIT-READY-{uuid4().hex}"
    ready_id, first_snapshot_id = _persist(scope, patient_ref=ready_ref)
    latest_snapshot_id = str(uuid4())
    scope.repo.persist_detection(
        ChaseItem(
            chase_id=ready_id,
            patient_ref=ready_ref,
            trigger=Trigger.NO_CARD_ON_FILE,
            detected_at=scope.base.isoformat(),
            state=State.READY,
            priority=80,
            direct_contract=_gate(ready_ref),
        ),
        ContactSnapshotInput(
            contact_snapshot_id=latest_snapshot_id,
            source_system="synthetic_authoritative_feed",
            source_version="v2",
            observed_at=scope.base + timedelta(seconds=2),
            verified_at=scope.base + timedelta(seconds=3),
            verification_method="staff",
            routes=(ContactRouteInput(
                Channel.EMAIL,
                "newer.synthetic.patient@example.invalid",
                "not_required",
            ),),
            verified_by_tenant_id=scope.tenant_id,
            verified_by_user_id=scope.staff_id,
        ),
    )
    blocked_ref = f"SYNTHETIC-INIT-BLOCKED-{uuid4().hex}"
    blocked_id, _ = _persist(scope, patient_ref=blocked_ref, blocked=True)
    submitted_id, _ = _persist(
        scope,
        patient_ref=f"SYNTHETIC-INIT-SUBMITTED-{uuid4().hex}",
        state=State.SUBMITTED,
    )
    policy = _policy(include_phone=False)
    delivery = _delivery()
    revision = scope.repo.create_policy(
        policy,
        delivery,
        practice_timezone="UTC",
        policy_revision_id=str(uuid4()),
        actor_tenant_id=scope.tenant_id,
        actor_user_id=scope.admin_id,
    )
    scope.repo.approve_policy(
        policy,
        delivery,
        practice_timezone="UTC",
        approval_event_id=str(uuid4()),
        policy_revision_id=revision.policy_revision_id,
        expected_policy_fingerprint=revision.policy_fingerprint,
        actor_tenant_id=scope.tenant_id,
        actor_user_id=scope.admin_id,
        reason="synthetic initialization approval",
        activate=True,
    )

    assert scope.repo.schedule_unscheduled(
        policy,
        delivery,
        practice_timezone="UTC",
        as_of=scope.base + timedelta(seconds=5),
        limit=10,
    ) == 1
    assert scope.repo.schedule_unscheduled(
        policy,
        delivery,
        practice_timezone="UTC",
        as_of=scope.base + timedelta(seconds=6),
        limit=10,
    ) == 0
    due = cadence_due_at(
        scope.base,
        day_offset=0,
        practice_timezone="UTC",
        quiet_hours=policy.quiet_hours,
    )
    with psycopg.connect(TEST_DSN) as conn:
        conn.execute(
            "SELECT set_config('cosmaneura.practice_id', %s, true)",
            (scope.practice_id,),
        )
        cadence = conn.execute(
            """SELECT chase_id, contact_snapshot_id, next_step_ordinal,
                      next_due_at
                 FROM chase_cadence_states
                WHERE practice_id = %s""",
            (scope.practice_id,),
        ).fetchall()
    assert cadence == [(ready_id, UUID(latest_snapshot_id), 0, due)]
    assert str(cadence[0][1]) != first_snapshot_id

    gate = _gate(blocked_ref, blocked=True)["gate_fingerprint"]
    scope.repo.release_direct_contract(
        review_release_id=str(uuid4()),
        chase_id=blocked_id,
        gate_fingerprint=gate,
        actor_tenant_id=scope.tenant_id,
        actor_user_id=scope.admin_id,
        reason="synthetic exact release before initialization",
    )
    assert scope.repo.schedule_unscheduled(
        policy,
        delivery,
        practice_timezone="UTC",
        as_of=scope.base + timedelta(seconds=7),
        limit=10,
    ) == 1
    with psycopg.connect(TEST_DSN) as conn:
        conn.execute(
            "SELECT set_config('cosmaneura.practice_id', %s, true)",
            (scope.practice_id,),
        )
        scheduled_ids = {
            row[0] for row in conn.execute(
                """SELECT chase_id FROM chase_cadence_states
                    WHERE practice_id = %s""",
                (scope.practice_id,),
            ).fetchall()
        }
    assert scheduled_ids == {ready_id, blocked_id}
    assert submitted_id not in scheduled_ids


def test_record_only_initialization_skips_system_rungs_to_phone_task(scope):
    chase_id, _ = _persist(
        scope, patient_ref=f"SYNTHETIC-RECORD-ONLY-{uuid4().hex}",
    )
    wanted = {"email.first", "sms.first", "email.reminder", "phone.call"}
    policy = ChasePolicy(
        name=f"synthetic-record-only-policy-{uuid4().hex}",
        practice_name="Synthetic Practice",
        upload_url="https://upload.example.invalid/insurance",
        approval_mode=ApprovalMode.PER_POLICY,
        quiet_hours=_TEST_QUIET_HOURS,
        max_touches=4,
        steps=(
            Step(Channel.EMAIL, "email.first", 0),
            Step(Channel.SMS, "sms.first", 0),
            Step(Channel.EMAIL, "email.reminder", 0),
            Step(Channel.PHONE, "phone.call", 0),
        ),
        templates=tuple(
            template for template in DEFAULT_TEMPLATES
            if template.template_id in wanted
        ),
    )
    delivery = DeliveryConfiguration(
        contact_snapshot_max_age_hours=24,
        scheduler_enabled=True,
    )
    revision = scope.repo.create_policy(
        policy,
        delivery,
        practice_timezone="UTC",
        policy_revision_id=str(uuid4()),
        actor_tenant_id=scope.tenant_id,
        actor_user_id=scope.admin_id,
    )
    scope.repo.approve_policy(
        policy,
        delivery,
        practice_timezone="UTC",
        approval_event_id=str(uuid4()),
        policy_revision_id=revision.policy_revision_id,
        expected_policy_fingerprint=revision.policy_fingerprint,
        actor_tenant_id=scope.tenant_id,
        actor_user_id=scope.admin_id,
        reason="synthetic record-only approval",
        activate=True,
    )
    as_of = scope.base + timedelta(seconds=10)
    assert scope.repo.schedule_unscheduled(
        policy,
        delivery,
        practice_timezone="UTC",
        as_of=as_of,
    ) == 1
    decisions = [
        scope.repo.reserve_due(
            policy,
            delivery,
            practice_timezone="UTC",
            as_of=as_of + timedelta(seconds=offset),
        )[0]
        for offset in range(4)
    ]
    assert [decision.action for decision in decisions] == [
        ScheduleAction.BLOCKED,
        ScheduleAction.BLOCKED,
        ScheduleAction.BLOCKED,
        ScheduleAction.STAFF_TASK,
    ]
    assert [decision.reason_code.value for decision in decisions[:3]] == [
        "channel_unavailable", "channel_unavailable", "channel_unavailable",
    ]
    with psycopg.connect(TEST_DSN) as conn:
        conn.execute(
            "SELECT set_config('cosmaneura.practice_id', %s, true)",
            (scope.practice_id,),
        )
        assert conn.execute(
            """SELECT count(*) FROM chase_delivery_outbox
                WHERE practice_id = %s AND chase_id = %s""",
            (scope.practice_id, chase_id),
        ).fetchone()[0] == 0
        assert conn.execute(
            """SELECT count(*) FROM chase_attempts
                WHERE practice_id = %s AND chase_id = %s""",
            (scope.practice_id, chase_id),
        ).fetchone()[0] == 0
        pending = conn.execute(
            """SELECT payload FROM chase_pending_tasks
                WHERE practice_id = %s AND chase_id = %s""",
            (scope.practice_id, chase_id),
        ).fetchone()[0]
    assert pending["channel"] == "phone"
    assert pending["policy_step_ordinal"] == 3


def test_persistent_blocked_rows_do_not_starve_later_due_work(scope):
    policy = _policy(include_phone=False)
    delivery = _delivery(contact_max_age_hours=1)
    revision = scope.repo.create_policy(
        policy,
        delivery,
        practice_timezone="UTC",
        policy_revision_id=str(uuid4()),
        actor_tenant_id=scope.tenant_id,
        actor_user_id=scope.admin_id,
    )
    scope.repo.approve_policy(
        policy,
        delivery,
        practice_timezone="UTC",
        approval_event_id=str(uuid4()),
        policy_revision_id=revision.policy_revision_id,
        expected_policy_fingerprint=revision.policy_fingerprint,
        actor_tenant_id=scope.tenant_id,
        actor_user_id=scope.admin_id,
        reason="synthetic fairness approval",
        activate=True,
    )
    stale_ids: list[str] = []
    stale_at = scope.base - timedelta(hours=2)
    for index in range(25):
        patient_ref = f"SYNTHETIC-STALE-{index:02d}-{uuid4().hex}"
        result = scope.repo.persist_detection(
            ChaseItem(
                chase_id=f"chase-{uuid4()}",
                patient_ref=patient_ref,
                trigger=Trigger.NO_CARD_ON_FILE,
                detected_at=stale_at.isoformat(),
                state=State.READY,
                direct_contract=_gate(patient_ref),
            ),
            ContactSnapshotInput(
                contact_snapshot_id=str(uuid4()),
                source_system="synthetic_stale_feed",
                source_version="v1",
                observed_at=stale_at,
                verified_at=stale_at,
                verification_method="staff",
                routes=(ContactRouteInput(
                    Channel.EMAIL,
                    f"stale-{index:02d}@example.invalid",
                    "not_required",
                ),),
                verified_by_tenant_id=scope.tenant_id,
                verified_by_user_id=scope.staff_id,
            ),
        )
        stale_ids.append(result.chase_id)
    fresh_id, _ = _persist(
        scope, patient_ref=f"SYNTHETIC-FAIR-FRESH-{uuid4().hex}",
    )
    assert scope.repo.schedule_unscheduled(
        policy,
        delivery,
        practice_timezone="UTC",
        as_of=scope.base + timedelta(seconds=10),
        limit=100,
    ) == 26
    first_pass = scope.repo.reserve_due(
        policy,
        delivery,
        practice_timezone="UTC",
        as_of=scope.base + timedelta(seconds=11),
        limit=25,
    )
    assert len(first_pass) == 25
    assert {
        decision.reason_code.value for decision in first_pass
    } == {"contact_snapshot_stale"}
    second_pass = scope.repo.reserve_due(
        policy,
        delivery,
        practice_timezone="UTC",
        as_of=scope.base + timedelta(seconds=12),
        limit=25,
    )
    assert len(second_pass) == 1
    assert second_pass[0].action is ScheduleAction.TRANSPORT
    with psycopg.connect(TEST_DSN) as conn:
        conn.execute(
            "SELECT set_config('cosmaneura.practice_id', %s, true)",
            (scope.practice_id,),
        )
        retained = conn.execute(
            """SELECT count(*), min(next_due_at), max(next_due_at)
                 FROM chase_cadence_states
                WHERE practice_id = %s AND chase_id = ANY(%s)""",
            (scope.practice_id, stale_ids),
        ).fetchone()
        still_ready = conn.execute(
            """SELECT count(*) FROM chase_items
                WHERE practice_id = %s AND chase_id = ANY(%s)
                  AND state = 'ready'""",
            (scope.practice_id, stale_ids),
        ).fetchone()[0]
        fresh_cadence = conn.execute(
            """SELECT count(*) FROM chase_cadence_states
                WHERE practice_id = %s AND chase_id = %s""",
            (scope.practice_id, fresh_id),
        ).fetchone()[0]
    assert retained[0] == 25
    assert retained[1] == retained[2] == (
        scope.base + timedelta(seconds=11, minutes=15)
    )
    assert still_ready == 25
    assert fresh_cadence == 1


def test_accepted_overdue_automated_rung_defers_to_next_local_day(scope):
    patient_ref = f"SYNTHETIC-SAME-DAY-{uuid4().hex}"
    chase_id, snapshot_id = _persist(scope, patient_ref=patient_ref)
    wanted = {"email.first", "email.reminder"}
    policy = ChasePolicy(
        name=f"synthetic-same-day-policy-{uuid4().hex}",
        practice_name="Synthetic Practice",
        upload_url="https://upload.example.invalid/insurance",
        approval_mode=ApprovalMode.PER_POLICY,
        quiet_hours=_TEST_QUIET_HOURS,
        max_touches=2,
        steps=(
            Step(Channel.EMAIL, "email.first", 0),
            Step(Channel.EMAIL, "email.reminder", 0),
        ),
        templates=tuple(
            template for template in DEFAULT_TEMPLATES
            if template.template_id in wanted
        ),
    )
    delivery = _delivery(contact_max_age_hours=168)
    revision, _ = _approve_and_schedule(
        scope, chase_id, snapshot_id, policy, delivery,
    )
    _reserve_email(scope, policy, delivery)
    claim = scope.repo.claim_delivery(
        expected_policy_fingerprint=revision.policy_fingerprint,
        contact_max_age_hours=24,
        as_of=scope.base + timedelta(seconds=7),
        lease_seconds=60,
    )
    assert claim is not None
    accepted_at = scope.base + timedelta(seconds=8)
    result = scope.repo.finalize_delivery(
        delivery_id=claim.delivery_id,
        lease_token=claim.lease_token,
        outcome="accepted",
        as_of=accepted_at,
    )
    assert result.cadence_advanced is True
    deferred = cadence_due_at(
        scope.base,
        day_offset=1,
        practice_timezone="UTC",
        quiet_hours=policy.quiet_hours,
    )
    with psycopg.connect(TEST_DSN) as conn:
        conn.execute(
            "SELECT set_config('cosmaneura.practice_id', %s, true)",
            (scope.practice_id,),
        )
        assert conn.execute(
            """SELECT next_step_ordinal, next_due_at
                 FROM chase_cadence_states
                WHERE practice_id = %s AND chase_id = %s""",
            (scope.practice_id, chase_id),
        ).fetchone() == (1, deferred)
    assert scope.repo.reserve_due(
        policy,
        delivery,
        practice_timezone="UTC",
        as_of=accepted_at + timedelta(seconds=1),
    ) == []
    reserved = scope.repo.reserve_due(
        policy,
        delivery,
        practice_timezone="UTC",
        as_of=deferred + timedelta(seconds=1),
    )
    assert len(reserved) == 1
    assert reserved[0].action is ScheduleAction.TRANSPORT


def test_different_policy_activation_cannot_strand_an_existing_cadence(scope):
    chase_id, snapshot_id = _persist(
        scope, patient_ref=f"SYNTHETIC-POLICY-SWAP-{uuid4().hex}",
    )
    delivery = _delivery()
    policy_a = _policy(include_phone=False)
    revision_a, approval_a = _approve_and_schedule(
        scope, chase_id, snapshot_id, policy_a, delivery,
    )
    policy_b = _policy(include_phone=False)
    revision_b = scope.repo.create_policy(
        policy_b,
        delivery,
        practice_timezone="UTC",
        policy_revision_id=str(uuid4()),
        actor_tenant_id=scope.tenant_id,
        actor_user_id=scope.admin_id,
    )
    with pytest.raises(SchedulerConflict, match="cannot strand"):
        scope.repo.approve_policy(
            policy_b,
            delivery,
            practice_timezone="UTC",
            approval_event_id=str(uuid4()),
            policy_revision_id=revision_b.policy_revision_id,
            expected_policy_fingerprint=revision_b.policy_fingerprint,
            actor_tenant_id=scope.tenant_id,
            actor_user_id=scope.admin_id,
            reason="synthetic unsafe replacement",
            activate=True,
        )
    with psycopg.connect(TEST_DSN) as conn:
        conn.execute(
            "SELECT set_config('cosmaneura.practice_id', %s, true)",
            (scope.practice_id,),
        )
        active = conn.execute(
            """SELECT policy_revision_id, approval_event_id
                 FROM chase_active_policies WHERE practice_id = %s""",
            (scope.practice_id,),
        ).fetchone()
        cadence_revision = conn.execute(
            """SELECT policy_revision_id FROM chase_cadence_states
                WHERE practice_id = %s AND chase_id = %s""",
            (scope.practice_id, chase_id),
        ).fetchone()[0]
    assert active == (
        UUID(revision_a.policy_revision_id), UUID(approval_a.approval_event_id),
    )
    assert cadence_revision == UUID(revision_a.policy_revision_id)


def test_stale_exact_revocation_replay_cannot_delete_new_activation(scope):
    policy = _policy(include_phone=False)
    delivery = _delivery()
    revision = scope.repo.create_policy(
        policy,
        delivery,
        practice_timezone="UTC",
        policy_revision_id=str(uuid4()),
        actor_tenant_id=scope.tenant_id,
        actor_user_id=scope.admin_id,
    )
    first_approval = scope.repo.approve_policy(
        policy,
        delivery,
        practice_timezone="UTC",
        approval_event_id=str(uuid4()),
        policy_revision_id=revision.policy_revision_id,
        expected_policy_fingerprint=revision.policy_fingerprint,
        actor_tenant_id=scope.tenant_id,
        actor_user_id=scope.admin_id,
        reason="synthetic first approval",
        activate=True,
    )
    assert first_approval.event_type == "approved"
    revocation_id = str(uuid4())
    revocation = scope.repo.revoke_policy(
        approval_event_id=revocation_id,
        policy_revision_id=revision.policy_revision_id,
        expected_policy_fingerprint=revision.policy_fingerprint,
        actor_tenant_id=scope.tenant_id,
        actor_user_id=scope.admin_id,
        reason="synthetic revocation",
    )
    newer = scope.repo.approve_policy(
        policy,
        delivery,
        practice_timezone="UTC",
        approval_event_id=str(uuid4()),
        policy_revision_id=revision.policy_revision_id,
        expected_policy_fingerprint=revision.policy_fingerprint,
        actor_tenant_id=scope.tenant_id,
        actor_user_id=scope.admin_id,
        reason="synthetic reapproval",
        activate=True,
    )
    assert scope.repo.revoke_policy(
        approval_event_id=revocation_id,
        policy_revision_id=revision.policy_revision_id,
        expected_policy_fingerprint=revision.policy_fingerprint,
        actor_tenant_id=scope.tenant_id,
        actor_user_id=scope.admin_id,
        reason="synthetic revocation",
    ) == revocation
    with psycopg.connect(TEST_DSN) as conn:
        conn.execute(
            "SELECT set_config('cosmaneura.practice_id', %s, true)",
            (scope.practice_id,),
        )
        active = conn.execute(
            """SELECT approval_event_id FROM chase_active_policies
                WHERE practice_id = %s""",
            (scope.practice_id,),
        ).fetchone()
    assert active == (UUID(newer.approval_event_id),)


def test_new_approval_cannot_replace_active_reserved_email_evidence(scope):
    chase_id, snapshot_id = _persist(
        scope, patient_ref=f"SYNTHETIC-REAPPROVE-EMAIL-{uuid4().hex}",
    )
    policy = _policy(include_phone=False)
    delivery = _delivery()
    revision, approval = _approve_and_schedule(
        scope, chase_id, snapshot_id, policy, delivery,
    )
    original = _reserve_email(scope, policy, delivery)
    rejected_event_id = str(uuid4())
    with pytest.raises(SchedulerConflict, match="queued exact work"):
        scope.repo.approve_policy(
            policy,
            delivery,
            practice_timezone="UTC",
            approval_event_id=rejected_event_id,
            policy_revision_id=revision.policy_revision_id,
            expected_policy_fingerprint=revision.policy_fingerprint,
            actor_tenant_id=scope.tenant_id,
            actor_user_id=scope.admin_id,
            reason="synthetic unsafe queued-email reapproval",
            activate=True,
        )
    with psycopg.connect(TEST_DSN) as conn:
        conn.execute(
            "SELECT set_config('cosmaneura.practice_id', %s, true)",
            (scope.practice_id,),
        )
        active = conn.execute(
            """SELECT approval_event_id FROM chase_active_policies
                WHERE practice_id = %s""",
            (scope.practice_id,),
        ).fetchone()[0]
        rejected = conn.execute(
            """SELECT 1 FROM chase_policy_approval_events
                WHERE practice_id = %s AND approval_event_id = %s::uuid""",
            (scope.practice_id, rejected_event_id),
        ).fetchone()
    assert active == UUID(approval.approval_event_id)
    assert rejected is None
    claim = scope.repo.claim_delivery(
        expected_policy_fingerprint=revision.policy_fingerprint,
        contact_max_age_hours=24,
        as_of=scope.base + timedelta(seconds=7),
        lease_seconds=60,
    )
    assert claim is not None
    assert claim.delivery_id == original.delivery_id


def test_new_approval_cannot_replace_active_pending_phone_evidence(scope):
    chase_id, snapshot_id = _persist(
        scope, patient_ref=f"SYNTHETIC-REAPPROVE-PHONE-{uuid4().hex}",
    )
    phone_template = next(
        template for template in DEFAULT_TEMPLATES
        if template.template_id == "phone.call"
    )
    policy = ChasePolicy(
        name=f"synthetic-reapproval-phone-only-{uuid4().hex}",
        practice_name="Synthetic Practice",
        upload_url="https://upload.example.invalid/insurance",
        approval_mode=ApprovalMode.PER_POLICY,
        quiet_hours=_TEST_QUIET_HOURS,
        max_touches=1,
        steps=(Step(Channel.PHONE, "phone.call", 0),),
        templates=(phone_template,),
    )
    delivery = _delivery()
    revision, approval = _approve_and_schedule(
        scope, chase_id, snapshot_id, policy, delivery,
    )
    queued = scope.repo.reserve_due(
        policy,
        delivery,
        practice_timezone="UTC",
        as_of=scope.base + timedelta(seconds=6),
    )
    assert queued[0].action is ScheduleAction.STAFF_TASK
    rejected_event_id = str(uuid4())
    with pytest.raises(SchedulerConflict, match="queued exact work"):
        scope.repo.approve_policy(
            policy,
            delivery,
            practice_timezone="UTC",
            approval_event_id=rejected_event_id,
            policy_revision_id=revision.policy_revision_id,
            expected_policy_fingerprint=revision.policy_fingerprint,
            actor_tenant_id=scope.tenant_id,
            actor_user_id=scope.admin_id,
            reason="synthetic unsafe pending-phone reapproval",
            activate=True,
        )
    with psycopg.connect(TEST_DSN) as conn:
        conn.execute(
            "SELECT set_config('cosmaneura.practice_id', %s, true)",
            (scope.practice_id,),
        )
        active = conn.execute(
            """SELECT approval_event_id FROM chase_active_policies
                WHERE practice_id = %s""",
            (scope.practice_id,),
        ).fetchone()[0]
        task = conn.execute(
            """SELECT payload ->> 'approval_event_id'
                 FROM chase_pending_tasks
                WHERE practice_id = %s AND chase_id = %s""",
            (scope.practice_id, chase_id),
        ).fetchone()[0]
    assert active == UUID(approval.approval_event_id)
    assert task == approval.approval_event_id
    completed = scope.repo.complete_staff_task(
        policy,
        delivery,
        practice_timezone="UTC",
        chase_id=chase_id,
        action_id=str(uuid4()),
        actor_tenant_id=scope.tenant_id,
        actor_user_id=scope.staff_id,
        outcome="no_answer",
    )
    assert completed.attempt_recorded is True


def test_revoke_then_reapprove_replaces_only_never_submitted_reservation(scope):
    chase_id, snapshot_id = _persist(
        scope, patient_ref=f"SYNTHETIC-REVOKE-EMAIL-{uuid4().hex}",
    )
    policy = _policy(include_phone=False)
    delivery = _delivery()
    revision, _ = _approve_and_schedule(
        scope, chase_id, snapshot_id, policy, delivery,
    )
    original = _reserve_email(scope, policy, delivery)
    scope.repo.revoke_policy(
        approval_event_id=str(uuid4()),
        policy_revision_id=revision.policy_revision_id,
        expected_policy_fingerprint=revision.policy_fingerprint,
        actor_tenant_id=scope.tenant_id,
        actor_user_id=scope.admin_id,
        reason="synthetic queued-email revocation",
    )
    with psycopg.connect(TEST_DSN) as conn:
        conn.execute(
            "SELECT set_config('cosmaneura.practice_id', %s, true)",
            (scope.practice_id,),
        )
        assert conn.execute(
            """SELECT state, error_code FROM chase_delivery_outbox
                WHERE practice_id = %s AND delivery_id = %s::uuid""",
            (scope.practice_id, original.delivery_id),
        ).fetchone() == ("cancelled", "policy_revoked")
        assert conn.execute(
            """SELECT event_type FROM chase_delivery_events
                WHERE practice_id = %s AND delivery_id = %s::uuid
                ORDER BY event_no DESC LIMIT 1""",
            (scope.practice_id, original.delivery_id),
        ).fetchone()[0] == "cancelled"
    newer = scope.repo.approve_policy(
        policy,
        delivery,
        practice_timezone="UTC",
        approval_event_id=str(uuid4()),
        policy_revision_id=revision.policy_revision_id,
        expected_policy_fingerprint=revision.policy_fingerprint,
        actor_tenant_id=scope.tenant_id,
        actor_user_id=scope.admin_id,
        reason="synthetic queued-email reapproval",
        activate=True,
    )
    replacement = _reserve_email(scope, policy, delivery)
    assert replacement.delivery_id != original.delivery_id
    claimed = scope.repo.claim_delivery(
        expected_policy_fingerprint=revision.policy_fingerprint,
        contact_max_age_hours=24,
        as_of=scope.base + timedelta(seconds=10),
        lease_seconds=60,
    )
    assert claimed is not None
    assert claimed.delivery_id == replacement.delivery_id
    with psycopg.connect(TEST_DSN) as conn:
        conn.execute(
            "SELECT set_config('cosmaneura.practice_id', %s, true)",
            (scope.practice_id,),
        )
        approval_event_id = conn.execute(
            """SELECT approval_event_id FROM chase_delivery_outbox
                WHERE practice_id = %s AND delivery_id = %s::uuid""",
            (scope.practice_id, replacement.delivery_id),
        ).fetchone()[0]
    assert approval_event_id == UUID(newer.approval_event_id)


def test_revoke_then_reapprove_removes_and_recreates_exact_phone_task(scope):
    chase_id, snapshot_id = _persist(
        scope, patient_ref=f"SYNTHETIC-REVOKE-PHONE-{uuid4().hex}",
    )
    phone_template = next(
        template for template in DEFAULT_TEMPLATES
        if template.template_id == "phone.call"
    )
    policy = ChasePolicy(
        name=f"synthetic-phone-only-{uuid4().hex}",
        practice_name="Synthetic Practice",
        upload_url="https://upload.example.invalid/insurance",
        approval_mode=ApprovalMode.PER_POLICY,
        quiet_hours=_TEST_QUIET_HOURS,
        max_touches=1,
        steps=(Step(Channel.PHONE, "phone.call", 0),),
        templates=(phone_template,),
    )
    delivery = _delivery()
    revision, _ = _approve_and_schedule(
        scope, chase_id, snapshot_id, policy, delivery,
    )
    first = scope.repo.reserve_due(
        policy,
        delivery,
        practice_timezone="UTC",
        as_of=scope.base + timedelta(seconds=6),
    )
    assert first[0].action is ScheduleAction.STAFF_TASK
    scope.repo.revoke_policy(
        approval_event_id=str(uuid4()),
        policy_revision_id=revision.policy_revision_id,
        expected_policy_fingerprint=revision.policy_fingerprint,
        actor_tenant_id=scope.tenant_id,
        actor_user_id=scope.admin_id,
        reason="synthetic queued-phone revocation",
    )
    with psycopg.connect(TEST_DSN) as conn:
        conn.execute(
            "SELECT set_config('cosmaneura.practice_id', %s, true)",
            (scope.practice_id,),
        )
        assert conn.execute(
            """SELECT count(*) FROM chase_pending_tasks
                WHERE practice_id = %s AND chase_id = %s""",
            (scope.practice_id, chase_id),
        ).fetchone()[0] == 0
    with pytest.raises(SchedulerConflict, match="no pending staff task"):
        scope.repo.complete_staff_task(
            policy,
            delivery,
            practice_timezone="UTC",
            chase_id=chase_id,
            action_id=str(uuid4()),
            actor_tenant_id=scope.tenant_id,
            actor_user_id=scope.staff_id,
            outcome="no_answer",
        )
    newer = scope.repo.approve_policy(
        policy,
        delivery,
        practice_timezone="UTC",
        approval_event_id=str(uuid4()),
        policy_revision_id=revision.policy_revision_id,
        expected_policy_fingerprint=revision.policy_fingerprint,
        actor_tenant_id=scope.tenant_id,
        actor_user_id=scope.admin_id,
        reason="synthetic queued-phone reapproval",
        activate=True,
    )
    recreated = scope.repo.reserve_due(
        policy,
        delivery,
        practice_timezone="UTC",
        as_of=scope.base + timedelta(seconds=8),
    )
    assert recreated[0].action is ScheduleAction.STAFF_TASK
    with psycopg.connect(TEST_DSN) as conn:
        conn.execute(
            "SELECT set_config('cosmaneura.practice_id', %s, true)",
            (scope.practice_id,),
        )
        payload = conn.execute(
            """SELECT payload FROM chase_pending_tasks
                WHERE practice_id = %s AND chase_id = %s""",
            (scope.practice_id, chase_id),
        ).fetchone()[0]
    assert payload["approval_event_id"] == newer.approval_event_id


@pytest.mark.parametrize("mutation", ["terminal", "gate"])
def test_claim_revalidates_item_and_gate_after_concurrent_update(scope, mutation):
    patient_ref = f"SYNTHETIC-CLAIM-RACE-{mutation}-{uuid4().hex}"
    chase_id, snapshot_id = _persist(scope, patient_ref=patient_ref)
    policy = _policy(include_phone=False)
    delivery = _delivery()
    revision, _ = _approve_and_schedule(
        scope, chase_id, snapshot_id, policy, delivery,
    )
    _reserve_email(scope, policy, delivery)
    application_name = f"p4-claim-item-race-{uuid4().hex}"
    race_repo = PostgresChaseSchedulerRepository(
        psycopg.conninfo.make_conninfo(
            TEST_DSN, application_name=application_name,
        ),
        practice_id=scope.practice_id,
    )
    with psycopg.connect(TEST_DSN) as blocker:
        blocker.execute(
            "SELECT set_config('cosmaneura.practice_id', %s, true)",
            (scope.practice_id,),
        )
        if mutation == "terminal":
            blocker.execute(
                """UPDATE chase_items
                      SET state = 'verified_closed',
                          closed_at = %s, version = version + 1
                    WHERE practice_id = %s AND chase_id = %s""",
                (
                    (scope.base + timedelta(seconds=8)).isoformat(),
                    scope.practice_id, chase_id,
                ),
            )
        else:
            blocker.execute(
                """UPDATE chase_items
                      SET direct_contract = %s, version = version + 1
                    WHERE practice_id = %s AND chase_id = %s""",
                (
                    psycopg.types.json.Jsonb(_gate(patient_ref, blocked=True)),
                    scope.practice_id, chase_id,
                ),
            )

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                race_repo.claim_delivery,
                expected_policy_fingerprint=revision.policy_fingerprint,
                contact_max_age_hours=24,
                as_of=scope.base + timedelta(seconds=9),
                lease_seconds=60,
            )
            waited_on_lock = False
            deadline = time.monotonic() + 5
            with psycopg.connect(TEST_DSN, autocommit=True) as observer:
                while time.monotonic() < deadline and not future.done():
                    wait = observer.execute(
                        """SELECT wait_event_type FROM pg_stat_activity
                            WHERE application_name = %s""",
                        (application_name,),
                    ).fetchone()
                    if wait is not None and wait[0] == "Lock":
                        waited_on_lock = True
                        break
                    time.sleep(0.02)
            blocker.commit()
            claimed = future.result(timeout=5)
    assert waited_on_lock is True
    assert claimed is None
    with psycopg.connect(TEST_DSN) as conn:
        conn.execute(
            "SELECT set_config('cosmaneura.practice_id', %s, true)",
            (scope.practice_id,),
        )
        assert conn.execute(
            """SELECT state FROM chase_delivery_outbox
                WHERE practice_id = %s AND chase_id = %s""",
            (scope.practice_id, chase_id),
        ).fetchone()[0] == "reserved"


def test_submission_guard_rejects_a_claim_revoked_before_provider_io(scope):
    chase_id, snapshot_id = _persist(
        scope, patient_ref=f"SYNTHETIC-GUARD-REVOKED-{uuid4().hex}",
    )
    policy = _policy(include_phone=False)
    delivery = _delivery()
    revision, _ = _approve_and_schedule(
        scope, chase_id, snapshot_id, policy, delivery,
    )
    _reserve_email(scope, policy, delivery)
    claim_at = datetime.now(timezone.utc)
    claim = scope.repo.claim_delivery(
        expected_policy_fingerprint=revision.policy_fingerprint,
        contact_max_age_hours=24,
        as_of=claim_at,
        lease_seconds=60,
    )
    assert claim is not None
    scope.repo.revoke_policy(
        approval_event_id=str(uuid4()),
        policy_revision_id=revision.policy_revision_id,
        expected_policy_fingerprint=revision.policy_fingerprint,
        actor_tenant_id=scope.tenant_id,
        actor_user_id=scope.admin_id,
        reason="synthetic pre-provider revocation",
    )
    yielded = False
    with pytest.raises(SchedulerConflict, match="no longer exact"):
        with scope.repo.guard_submission(claim):
            yielded = True
    assert yielded is False
    failed = scope.repo.finalize_delivery(
        delivery_id=claim.delivery_id,
        lease_token=claim.lease_token,
        outcome="permanent_failure",
        as_of=datetime.now(timezone.utc),
        error_code="submission_authorization_changed",
    )
    assert failed.attempt_recorded is False


def test_submission_guard_holds_revocation_through_provider_boundary(scope):
    chase_id, snapshot_id = _persist(
        scope, patient_ref=f"SYNTHETIC-GUARD-HELD-{uuid4().hex}",
    )
    policy = _policy(include_phone=False)
    delivery = _delivery()
    revision, _ = _approve_and_schedule(
        scope, chase_id, snapshot_id, policy, delivery,
    )
    _reserve_email(scope, policy, delivery)
    claim = scope.repo.claim_delivery(
        expected_policy_fingerprint=revision.policy_fingerprint,
        contact_max_age_hours=24,
        as_of=datetime.now(timezone.utc),
        lease_seconds=60,
    )
    assert claim is not None
    application_name = f"p4-guard-revocation-{uuid4().hex}"
    race_repo = PostgresChaseSchedulerRepository(
        psycopg.conninfo.make_conninfo(
            TEST_DSN, application_name=application_name,
        ),
        practice_id=scope.practice_id,
    )
    provider_called = False
    with ThreadPoolExecutor(max_workers=1) as executor:
        with scope.repo.guard_submission(claim) as guarded:
            assert guarded == claim
            future = executor.submit(
                race_repo.revoke_policy,
                approval_event_id=str(uuid4()),
                policy_revision_id=revision.policy_revision_id,
                expected_policy_fingerprint=revision.policy_fingerprint,
                actor_tenant_id=scope.tenant_id,
                actor_user_id=scope.admin_id,
                reason="synthetic revocation waiting on provider guard",
            )
            waited_on_lock = False
            deadline = time.monotonic() + 5
            with psycopg.connect(TEST_DSN, autocommit=True) as observer:
                while time.monotonic() < deadline and not future.done():
                    wait = observer.execute(
                        """SELECT wait_event_type FROM pg_stat_activity
                            WHERE application_name = %s""",
                        (application_name,),
                    ).fetchone()
                    if wait is not None and wait[0] == "Lock":
                        waited_on_lock = True
                        break
                    time.sleep(0.02)
            assert future.done() is False
            provider_called = True
        future.result(timeout=5)
    assert waited_on_lock is True
    assert provider_called is True


def test_submission_guard_rejects_expired_lease_before_provider_io(scope):
    chase_id, snapshot_id = _persist(
        scope, patient_ref=f"SYNTHETIC-GUARD-EXPIRED-{uuid4().hex}",
    )
    policy = _policy(include_phone=False)
    delivery = _delivery()
    revision, _ = _approve_and_schedule(
        scope, chase_id, snapshot_id, policy, delivery,
    )
    _reserve_email(scope, policy, delivery)
    with psycopg.connect(TEST_DSN) as conn:
        claim_at = conn.execute("SELECT statement_timestamp()").fetchone()[0]
    claim = scope.repo.claim_delivery(
        expected_policy_fingerprint=revision.policy_fingerprint,
        contact_max_age_hours=24,
        as_of=claim_at,
        lease_seconds=1,
    )
    assert claim is not None

    deadline = time.monotonic() + 3
    expired_at = None
    while time.monotonic() < deadline:
        with psycopg.connect(TEST_DSN) as conn:
            expired_at, expired = conn.execute(
                """SELECT statement_timestamp(),
                          lease_expires_at <= statement_timestamp()
                     FROM chase_delivery_outbox
                    WHERE practice_id = %s AND delivery_id = %s::uuid""",
                (scope.practice_id, claim.delivery_id),
            ).fetchone()
        if expired:
            break
        time.sleep(0.02)
    else:
        pytest.fail("synthetic provider submission lease did not expire")

    yielded = False
    with pytest.raises(SchedulerLeaseExpired, match="expired"):
        with scope.repo.guard_submission(claim):
            yielded = True
    assert yielded is False
    assert scope.repo.quarantine_expired_submissions(
        as_of=expired_at,
    ) == [claim.delivery_id]
    assert [row.delivery_id for row in scope.repo.list_ambiguous()] == [
        claim.delivery_id,
    ]


@pytest.mark.parametrize("operation", ["reserve", "claim"])
def test_revocation_serializes_with_reserve_and_claim(scope, operation):
    chase_id, snapshot_id = _persist(
        scope, patient_ref=f"SYNTHETIC-REVOKE-RACE-{operation}",
    )
    policy = _policy(include_phone=False)
    delivery = _delivery()
    revision, approval = _approve_and_schedule(
        scope, chase_id, snapshot_id, policy, delivery,
    )
    if operation == "claim":
        _reserve_email(scope, policy, delivery)

    application_name = f"p4-revocation-race-{uuid4().hex}"
    race_dsn = psycopg.conninfo.make_conninfo(
        TEST_DSN, application_name=application_name,
    )
    race_repo = PostgresChaseSchedulerRepository(
        race_dsn, practice_id=scope.practice_id,
    )
    with psycopg.connect(TEST_DSN) as blocker:
        blocker.execute(
            "SELECT set_config('cosmaneura.practice_id', %s, true)",
            (scope.practice_id,),
        )
        blocker.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"{scope.practice_id}\x1factive-policy",),
        )
        blocker.execute(
            """DELETE FROM chase_active_policies
                WHERE practice_id = %s AND policy_revision_id = %s::uuid
                  AND policy_fingerprint = %s
                  AND approval_event_id = %s::uuid""",
            (
                scope.practice_id, revision.policy_revision_id,
                revision.policy_fingerprint, approval.approval_event_id,
            ),
        )

        def execute():
            if operation == "reserve":
                return race_repo.reserve_due(
                    policy,
                    delivery,
                    practice_timezone="UTC",
                    as_of=scope.base + timedelta(seconds=7),
                )
            return race_repo.claim_delivery(
                expected_policy_fingerprint=revision.policy_fingerprint,
                contact_max_age_hours=24,
                as_of=scope.base + timedelta(seconds=7),
                lease_seconds=60,
            )

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(execute)
            waited_on_lock = False
            deadline = time.monotonic() + 5
            with psycopg.connect(TEST_DSN, autocommit=True) as observer:
                while time.monotonic() < deadline and not future.done():
                    wait = observer.execute(
                        """SELECT wait_event_type FROM pg_stat_activity
                            WHERE application_name = %s""",
                        (application_name,),
                    ).fetchone()
                    if wait is not None and wait[0] == "Lock":
                        waited_on_lock = True
                        break
                    time.sleep(0.02)
            blocker.commit()
            result = future.result(timeout=5)
    assert waited_on_lock is True
    assert result == ([] if operation == "reserve" else None)
    with psycopg.connect(TEST_DSN) as conn:
        conn.execute(
            "SELECT set_config('cosmaneura.practice_id', %s, true)",
            (scope.practice_id,),
        )
        if operation == "claim":
            assert conn.execute(
                """SELECT state FROM chase_delivery_outbox
                    WHERE practice_id = %s AND chase_id = %s""",
                (scope.practice_id, chase_id),
            ).fetchone()[0] == "reserved"
