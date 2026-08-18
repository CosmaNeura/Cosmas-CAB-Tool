"""Narrow policy-revocation cleanup and retry-boundary checks."""
from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest


ROOT = Path(__file__).resolve().parents[1]
UP_PATH = ROOT / "migrations" / "006_chase_policy_revocation.up.sql"
DOWN_PATH = ROOT / "migrations" / "006_chase_policy_revocation.down.sql"
UP_SQL = UP_PATH.read_text(encoding="utf-8")
DOWN_SQL = DOWN_PATH.read_text(encoding="utf-8")
TEST_DSN = os.environ.get("COSMANEURA_TEST_DATABASE_URL", "")


def test_revocation_cleanup_is_narrow_and_not_public():
    normalized = " ".join(UP_SQL.split())
    assert "CREATE FUNCTION cosmaneura_cancel_revoked_chase_policy" in UP_SQL
    assert "outbox.state = 'reserved'" in UP_SQL
    assert "latest_event_type IS DISTINCT FROM 'revoked'" in UP_SQL
    assert "state = 'cancelled'" in UP_SQL
    assert "error_code = 'policy_revoked'" in UP_SQL
    assert "cosmaneura.chase-staff-task.v1" in UP_SQL
    assert (
        "REVOKE ALL ON FUNCTION "
        "cosmaneura_cancel_revoked_chase_policy(uuid, text) FROM PUBLIC"
    ) in normalized
    assert "DROP FUNCTION IF EXISTS " in DOWN_SQL
    assert "CREATE FUNCTION cosmaneura_cancel_stale_chase_work" in UP_SQL
    assert "gate_evidence_changed" in UP_SQL
    assert UP_SQL.count("pg_advisory_xact_lock") == 2
    assert (
        "REVOKE ALL ON FUNCTION cosmaneura_cancel_stale_chase_work(text) "
        "FROM PUBLIC"
    ) in normalized


def test_only_no_touch_terminal_rows_leave_the_live_uniqueness_boundary():
    normalized = " ".join(UP_SQL.split())
    predicate = "WHERE state NOT IN ('cancelled', 'permanent_failure')"
    assert normalized.count(predicate) == 2
    assert "chase_delivery_outbox_live_item_step_unique" in UP_SQL
    assert "chase_delivery_outbox_live_item_local_day_unique" in UP_SQL
    assert "accepted" not in predicate
    assert "ambiguous" not in predicate


psycopg = pytest.importorskip("psycopg")


@pytest.mark.skipif(
    not TEST_DSN, reason="COSMANEURA_TEST_DATABASE_URL is not configured",
)
def test_revocation_cancels_only_queued_work_and_allows_no_touch_replacement():
    suffix = uuid4().hex
    practice_id = f"p4-revoke-{suffix}"
    tenant_id = uuid4()
    admin_id = uuid4()
    chase_id = f"chase-{suffix}"
    snapshot_id = uuid4()
    policy_id = uuid4()
    approved_id = uuid4()
    revoked_id = uuid4()
    reapproved_id = uuid4()
    delivery_id = uuid4()
    second_delivery_id = uuid4()
    third_delivery_id = uuid4()
    fingerprint = "a" * 64
    gate = "b" * 64

    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        if "test" not in (conn.info.dbname or "").casefold():
            pytest.fail(
                "COSMANEURA_TEST_DATABASE_URL must name a disposable test database"
            )
        migrations = ROOT / "migrations"
        for relation, filename in (
            ("chase_items", "002_chase_persistence.up.sql"),
            ("practices", "003_pilot_persistence.up.sql"),
            ("chase_delivery_outbox", "004_chase_delivery_runtime.up.sql"),
        ):
            if conn.execute(
                "SELECT to_regclass(%s)", (f"public.{relation}",),
            ).fetchone()[0] is None:
                conn.execute(
                    (migrations / filename).read_text(encoding="utf-8"),
                    prepare=False,
                )
        if conn.execute(
            "SELECT to_regprocedure("
            "'cosmaneura_record_accepted_delivery_attempt(uuid)')"
        ).fetchone()[0] is None:
            conn.execute(
                (migrations / "005_accepted_delivery_attempt.up.sql").read_text(
                    encoding="utf-8"
                ),
                prepare=False,
            )
        if conn.execute(
            "SELECT to_regprocedure("
            "'cosmaneura_cancel_revoked_chase_policy(uuid,text)')"
        ).fetchone()[0] is None:
            conn.execute(UP_SQL, prepare=False)

    try:
        with psycopg.connect(TEST_DSN) as conn:
            conn.execute(
                "SELECT set_config('cosmaneura.practice_id', %s, true)",
                (practice_id,),
            )
            conn.execute(
                """INSERT INTO practices (
                       practice_id, entra_tenant_id, display_name
                   ) VALUES (%s, %s, 'Synthetic Revocation Practice')""",
                (practice_id, tenant_id),
            )
            conn.execute(
                "INSERT INTO users (tenant_id, user_id) VALUES (%s, %s)",
                (tenant_id, admin_id),
            )
            conn.execute(
                """INSERT INTO practice_memberships (
                       practice_id, tenant_id, user_id, roles
                   ) VALUES (%s, %s, %s, ARRAY['Admin']::text[])""",
                (practice_id, tenant_id, admin_id),
            )
            conn.execute(
                """INSERT INTO chase_items (
                       practice_id, chase_id, patient_ref, trigger, detected_at,
                       state, bucket, direct_contract
                   ) VALUES (
                       %s, %s, %s, 'no_card_on_file',
                       '2026-08-18T12:00:00+00:00', 'ready', 'missing',
                       %s::jsonb
                   )""",
                (
                    practice_id,
                    chase_id,
                    f"SYNTHETIC-{suffix}",
                    '{"known":true,"blocked":false,'
                    f'"gate_fingerprint":"{gate}"}}',
                ),
            )
            conn.execute(
                """INSERT INTO chase_contact_snapshots (
                       practice_id, contact_snapshot_id, chase_id,
                       source_system, source_version, observed_at, verified_at,
                       verification_method, created_at
                   ) VALUES (
                       %s, %s, %s, 'synthetic', 'v1',
                       '2026-08-18T11:59:00+00:00',
                       '2026-08-18T11:59:00+00:00',
                       'authoritative_source', '2026-08-18T12:00:00+00:00'
                   )""",
                (practice_id, snapshot_id, chase_id),
            )
            conn.execute(
                """INSERT INTO chase_contact_routes (
                       practice_id, contact_snapshot_id, channel, destination,
                       consent_status
                   ) VALUES (%s, %s, 'email', 'synthetic@example.invalid',
                             'not_required')""",
                (practice_id, snapshot_id),
            )
            conn.execute(
                """INSERT INTO chase_policy_revisions (
                       practice_id, policy_revision_id, policy_name, revision_no,
                       policy_fingerprint, approval_mode, practice_name,
                       practice_timezone, quiet_hours_start, quiet_hours_end,
                       max_touches, created_by_tenant_id, created_by_user_id,
                       created_at
                   ) VALUES (
                       %s, %s, 'synthetic-revocation', 1, %s, 'per_policy',
                       'Synthetic Practice', 'UTC', '21:00', '08:00', 1,
                       %s, %s, '2026-08-18T12:00:00+00:00'
                   )""",
                (practice_id, policy_id, fingerprint, tenant_id, admin_id),
            )
            conn.execute(
                """INSERT INTO chase_policy_steps (
                       practice_id, policy_revision_id, step_ordinal, channel,
                       template_id, day_offset, performer, template_fingerprint
                   ) VALUES (%s, %s, 0, 'email', 'email.first', 0, 'system', %s)""",
                (practice_id, policy_id, "c" * 64),
            )
            conn.execute(
                """INSERT INTO chase_policy_approval_events (
                       practice_id, approval_event_id, policy_revision_id,
                       policy_fingerprint, event_type, actor_tenant_id,
                       actor_user_id, occurred_at, reason, payload_hash
                   ) VALUES (
                       %s, %s, %s, %s, 'approved', %s, %s,
                       '2026-08-18T12:01:00+00:00', 'synthetic approval', %s
                   )""",
                (
                    practice_id, approved_id, policy_id, fingerprint,
                    tenant_id, admin_id, "d" * 64,
                ),
            )
            conn.execute(
                """INSERT INTO chase_cadence_states (
                       practice_id, chase_id, policy_revision_id,
                       contact_snapshot_id, next_step_ordinal, next_due_at,
                       updated_at
                   ) VALUES (
                       %s, %s, %s, %s, 0,
                       '2026-08-18T12:00:00+00:00',
                       '2026-08-18T12:00:00+00:00'
                   )""",
                (practice_id, chase_id, policy_id, snapshot_id),
            )

            def insert_outbox(target_delivery_id):
                conn.execute(
                    """INSERT INTO chase_delivery_outbox (
                           practice_id, delivery_id, provider_operation_id,
                           chase_id, policy_revision_id, policy_step_ordinal,
                           approval_event_id, policy_fingerprint, approval_mode,
                           gate_fingerprint, gate_disposition,
                           contact_snapshot_id, channel, template_id, provider,
                           local_delivery_date, state, available_at,
                           created_at, updated_at
                       ) VALUES (
                           %s, %s, %s, %s, %s, 0, %s, %s, 'per_policy',
                           %s, 'clear', %s, 'email', 'email.first',
                           'azure_communication_services_email',
                           '2026-08-18', 'reserved',
                           '2026-08-18T12:00:00+00:00',
                           '2026-08-18T12:00:00+00:00',
                           '2026-08-18T12:00:00+00:00'
                       )""",
                    (
                        practice_id, target_delivery_id, uuid4(), chase_id,
                        policy_id, approved_id, fingerprint, gate, snapshot_id,
                    ),
                )
                conn.execute(
                    """INSERT INTO chase_delivery_events (
                           practice_id, delivery_id, event_no, event_type,
                           occurred_at
                       ) VALUES (%s, %s, 1, 'reserved',
                                 '2026-08-18T12:00:00+00:00')""",
                    (practice_id, target_delivery_id),
                )

            insert_outbox(delivery_id)
            conn.execute(
                """INSERT INTO chase_pending_tasks (
                       practice_id, chase_id, payload, updated_at
                   ) VALUES (
                       %s, %s, %s::jsonb, '2026-08-18T12:00:00+00:00'
                   )""",
                (
                    practice_id,
                    chase_id,
                    '{"schema":"cosmaneura.chase-staff-task.v1",'
                    f'"policy_revision_id":"{policy_id}",'
                    f'"policy_fingerprint":"{fingerprint}"}}',
                ),
            )
            conn.execute(
                """INSERT INTO chase_policy_approval_events (
                       practice_id, approval_event_id, policy_revision_id,
                       policy_fingerprint, event_type, actor_tenant_id,
                       actor_user_id, occurred_at, reason, payload_hash
                   ) VALUES (
                       %s, %s, %s, %s, 'revoked', %s, %s,
                       '2026-08-18T12:02:00+00:00', 'synthetic revocation', %s
                   )""",
                (
                    practice_id, revoked_id, policy_id, fingerprint,
                    tenant_id, admin_id, "e" * 64,
                ),
            )

            assert conn.execute(
                "SELECT cosmaneura_cancel_revoked_chase_policy(%s, %s)",
                (policy_id, fingerprint),
            ).fetchone()[0] == 2
            assert conn.execute(
                """SELECT state, error_code, finalized_at IS NOT NULL
                     FROM chase_delivery_outbox
                    WHERE practice_id = %s AND delivery_id = %s""",
                (practice_id, delivery_id),
            ).fetchone() == ("cancelled", "policy_revoked", True)
            assert conn.execute(
                """SELECT event_type, error_code FROM chase_delivery_events
                    WHERE practice_id = %s AND delivery_id = %s
                    ORDER BY event_no""",
                (practice_id, delivery_id),
            ).fetchall() == [("reserved", None), ("cancelled", "policy_revoked")]
            assert conn.execute(
                """SELECT count(*) FROM chase_pending_tasks
                    WHERE practice_id = %s AND chase_id = %s""",
                (practice_id, chase_id),
            ).fetchone()[0] == 0
            assert conn.execute(
                """SELECT count(*) FROM chase_cadence_states
                    WHERE practice_id = %s AND chase_id = %s""",
                (practice_id, chase_id),
            ).fetchone()[0] == 1

            insert_outbox(second_delivery_id)
            with pytest.raises(psycopg.errors.UniqueViolation):
                with conn.transaction():
                    insert_outbox(third_delivery_id)
            conn.execute(
                """UPDATE chase_delivery_outbox
                      SET state = 'permanent_failure', finalized_at = now(),
                          error_code = 'synthetic_failure', updated_at = now()
                    WHERE practice_id = %s AND delivery_id = %s""",
                (practice_id, second_delivery_id),
            )
            insert_outbox(third_delivery_id)

            conn.execute(
                """INSERT INTO chase_policy_approval_events (
                       practice_id, approval_event_id, policy_revision_id,
                       policy_fingerprint, event_type, actor_tenant_id,
                       actor_user_id, occurred_at, reason, payload_hash
                   ) VALUES (
                       %s, %s, %s, %s, 'approved', %s, %s,
                       '2026-08-18T12:03:00+00:00', 'synthetic reapproval', %s
                   )""",
                (
                    practice_id, reapproved_id, policy_id, fingerprint,
                    tenant_id, admin_id, "f" * 64,
                ),
            )
            with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
                with conn.transaction():
                    conn.execute(
                        "SELECT cosmaneura_cancel_revoked_chase_policy(%s, %s)",
                        (policy_id, fingerprint),
                    )

            conn.execute(
                """INSERT INTO chase_pending_tasks (
                       practice_id, chase_id, payload, updated_at
                   ) VALUES (%s, %s, %s::jsonb, now())""",
                (
                    practice_id,
                    chase_id,
                    '{"schema":"cosmaneura.chase-staff-task.v1",'
                    f'"policy_revision_id":"{policy_id}",'
                    f'"policy_fingerprint":"{fingerprint}",'
                    f'"gate_fingerprint":"{gate}"}}',
                ),
            )
            refreshed_gate = "9" * 64
            conn.execute(
                """UPDATE chase_items
                      SET direct_contract = %s::jsonb, version = version + 1,
                          updated_at = now()
                    WHERE practice_id = %s AND chase_id = %s""",
                (
                    '{"known":false,"blocked":true,'
                    f'"gate_fingerprint":"{refreshed_gate}"}}',
                    practice_id,
                    chase_id,
                ),
            )
            assert conn.execute(
                "SELECT cosmaneura_cancel_stale_chase_work(%s)",
                (chase_id,),
            ).fetchone()[0] == 2
            assert conn.execute(
                """SELECT state, error_code
                     FROM chase_delivery_outbox
                    WHERE practice_id = %s AND delivery_id = %s""",
                (practice_id, third_delivery_id),
            ).fetchone() == ("cancelled", "gate_evidence_changed")
            assert conn.execute(
                """SELECT count(*) FROM chase_pending_tasks
                    WHERE practice_id = %s AND chase_id = %s""",
                (practice_id, chase_id),
            ).fetchone()[0] == 0
            assert conn.execute(
                """SELECT count(*) FROM chase_cadence_states
                    WHERE practice_id = %s AND chase_id = %s""",
                (practice_id, chase_id),
            ).fetchone()[0] == 1
    finally:
        with psycopg.connect(TEST_DSN, autocommit=True) as conn:
            conn.execute("SET session_replication_role = replica")
            try:
                for table in (
                    "chase_delivery_events",
                    "chase_delivery_outbox",
                    "chase_cadence_states",
                    "chase_active_policies",
                    "chase_review_releases",
                    "chase_message_releases",
                    "chase_policy_approval_events",
                    "chase_policy_steps",
                    "chase_policy_revisions",
                    "chase_contact_routes",
                    "chase_contact_snapshots",
                    "chase_pending_tasks",
                    "chase_attempts",
                    "chase_items",
                    "practice_memberships",
                ):
                    conn.execute(
                        f"DELETE FROM {table} WHERE practice_id = %s",
                        (practice_id,),
                    )
                conn.execute(
                    "DELETE FROM users WHERE tenant_id = %s", (tenant_id,),
                )
                conn.execute(
                    "DELETE FROM practices WHERE practice_id = %s", (practice_id,),
                )
            finally:
                conn.execute("SET session_replication_role = origin")
