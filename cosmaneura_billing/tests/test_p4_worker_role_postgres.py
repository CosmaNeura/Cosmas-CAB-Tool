"""Live PostgreSQL authority checks for the dedicated chase scheduler login.

The disposable database URL must contain ``test``.  The worker necessarily sees
the chase contact route it is about to use, but receives no identity, encounter,
feedback, or access-audit read authority and no human policy-approval authority.
"""
from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest

psycopg = pytest.importorskip("psycopg")
from psycopg import sql
from psycopg.conninfo import make_conninfo

from cosmaneura_billing.migrations import (
    _provision_chase_worker_role,
    _provision_runtime_role,
)
from cosmaneura_billing.settings import Settings


TEST_DSN = os.environ.get("COSMANEURA_TEST_DATABASE_URL", "")
ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(
    not TEST_DSN, reason="COSMANEURA_TEST_DATABASE_URL is not configured",
)

P4_TABLES = (
    "chase_contact_snapshots",
    "chase_contact_routes",
    "chase_policy_revisions",
    "chase_policy_steps",
    "chase_policy_approval_events",
    "chase_message_releases",
    "chase_review_releases",
    "chase_active_policies",
    "chase_cadence_states",
    "chase_delivery_outbox",
    "chase_delivery_events",
)


def _relation_exists(conn, name: str) -> bool:
    return conn.execute(
        "SELECT to_regclass(%s)", (f"public.{name}",),
    ).fetchone()[0] is not None


@pytest.fixture(scope="session", autouse=True)
def _p4_schema():
    if not TEST_DSN:
        yield
        return
    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        if "test" not in (conn.info.dbname or "").casefold():
            pytest.fail(
                "COSMANEURA_TEST_DATABASE_URL must name a disposable test database"
            )
        migrations = ROOT / "migrations"
        if not _relation_exists(conn, "chase_items"):
            conn.execute(
                (migrations / "002_chase_persistence.up.sql").read_text(
                    encoding="utf-8"
                ),
                prepare=False,
            )
        if not _relation_exists(conn, "encounters"):
            conn.execute(
                (migrations / "003_pilot_persistence.up.sql").read_text(
                    encoding="utf-8"
                ),
                prepare=False,
            )
        if not _relation_exists(conn, "chase_delivery_outbox"):
            conn.execute(
                (migrations / "004_chase_delivery_runtime.up.sql").read_text(
                    encoding="utf-8"
                ),
                prepare=False,
            )
        if conn.execute(
            "SELECT to_regprocedure("
            "'cosmaneura_record_accepted_delivery_attempt(uuid)')"
        ).fetchone()[0] is None:
            authority_migrations = sorted(migrations.glob("005_*.up.sql"))
            if len(authority_migrations) != 1:
                pytest.fail("one 005 chase delivery authority migration is required")
            conn.execute(
                authority_migrations[0].read_text(encoding="utf-8"),
                prepare=False,
            )
        revocation_helper = conn.execute(
            "SELECT to_regprocedure("
            "'cosmaneura_cancel_revoked_chase_policy(uuid,text)')"
        ).fetchone()[0]
        stale_work_helper = conn.execute(
            "SELECT to_regprocedure("
            "'cosmaneura_cancel_stale_chase_work(text)')"
        ).fetchone()[0]
        if revocation_helper is None and stale_work_helper is None:
            revocation_migrations = sorted(migrations.glob("006_*.up.sql"))
            if len(revocation_migrations) != 1:
                pytest.fail("one 006 chase revocation migration is required")
            conn.execute(
                revocation_migrations[0].read_text(encoding="utf-8"),
                prepare=False,
            )
        elif revocation_helper is None or stale_work_helper is None:
            pytest.fail("the 006 chase revocation migration is only partially applied")
    yield


def _expect_insufficient_privilege(conn, statement: str, params=()) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with conn.transaction():
            conn.execute(statement, params)


def _remove_test_scope(
    *,
    practice_ids: tuple[str, str],
    tenant_ids: tuple[str, str],
    roles: tuple[str, ...],
) -> None:
    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        for role in roles:
            conn.execute(
                "DELETE FROM runtime_practice_bindings WHERE role_name = %s",
                (role,),
            )
            if conn.execute(
                "SELECT 1 FROM pg_roles WHERE rolname = %s", (role,),
            ).fetchone():
                conn.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
                conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
        # Immutable-history triggers and FK triggers are deliberately strict in
        # production. This superuser-only cleanup is confined to the disposable
        # test database and to generated practice/tenant identifiers.
        conn.execute("SET session_replication_role = replica")
        try:
            for table in reversed(P4_TABLES):
                conn.execute(
                    sql.SQL("DELETE FROM {} WHERE practice_id = ANY(%s)").format(
                        sql.Identifier(table)
                    ),
                    (list(practice_ids),),
                )
            for table in (
                "chase_action_receipts",
                "chase_resolutions",
                "chase_pending_tasks",
                "chase_attempts",
                "chase_items",
                "access_audit_completions",
                "access_audit_attempts",
                "coder_feedback_decisions",
                "coder_feedback",
                "encounters",
                "practice_memberships",
            ):
                conn.execute(
                    sql.SQL("DELETE FROM {} WHERE practice_id = ANY(%s)").format(
                        sql.Identifier(table)
                    ),
                    (list(practice_ids),),
                )
            conn.execute(
                "DELETE FROM users WHERE tenant_id::text = ANY(%s)",
                (list(tenant_ids),),
            )
            conn.execute(
                "DELETE FROM practices WHERE practice_id = ANY(%s)",
                (list(practice_ids),),
            )
        finally:
            conn.execute("SET session_replication_role = origin")


def test_worker_role_is_practice_bound_and_separates_delivery_authority():
    suffix = uuid4().hex
    practice_a = f"p4-worker-a-{suffix}"
    practice_b = f"p4-worker-b-{suffix}"
    tenant_a = str(uuid4())
    tenant_b = str(uuid4())
    admin_a = str(uuid4())
    admin_b = str(uuid4())
    chase_a = f"chase-a-{suffix}"
    chase_b = f"chase-b-{suffix}"
    app_role = f"p4_app_{suffix[:20]}"
    worker_role = f"p4_worker_{suffix[:17]}"
    rolled_back_role = f"p4_rollback_{suffix[:15]}"
    app_password = f"synthetic-app-{suffix}"
    worker_password = f"synthetic-worker-{suffix}"
    policy_id = uuid4()
    approval_id = uuid4()
    snapshot_id = uuid4()
    message_release_id = uuid4()
    review_release_id = uuid4()
    fingerprint = "a" * 64
    template_fingerprint = "b" * 64
    gate_fingerprint = "c" * 64

    try:
        with psycopg.connect(TEST_DSN) as conn:
            for practice_id, tenant_id, admin_id in (
                (practice_a, tenant_a, admin_a),
                (practice_b, tenant_b, admin_b),
            ):
                conn.execute(
                    "SELECT set_config('cosmaneura.practice_id', %s, true)",
                    (practice_id,),
                )
                conn.execute(
                    """INSERT INTO practices (
                           practice_id, entra_tenant_id, display_name
                       ) VALUES (%s, %s::uuid, %s)""",
                    (practice_id, tenant_id, f"Synthetic {practice_id}"),
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
                "SELECT set_config('cosmaneura.practice_id', %s, true)",
                (practice_b,),
            )
            conn.execute(
                """INSERT INTO chase_items (
                       practice_id, chase_id, patient_ref, trigger,
                       detected_at, state, bucket
                   ) VALUES (%s, %s, 'SYNTHETIC-WORKER-B', 'no_card_on_file',
                             '2026-08-18T12:00:00+00:00', 'ready', 'missing')""",
                (practice_b, chase_b),
            )
            settings = Settings(
                database_url=TEST_DSN,
                database_runtime_role=app_role,
                database_runtime_password=app_password,
                chase_worker_database_role=worker_role,
                chase_worker_database_password=worker_password,
                practice_id=practice_a,
            )
            with pytest.raises(RuntimeError, match="synthetic rollback"):
                with conn.transaction():
                    _provision_chase_worker_role(conn, Settings(
                        database_url=TEST_DSN,
                        chase_worker_database_role=rolled_back_role,
                        chase_worker_database_password=worker_password,
                        practice_id=practice_a,
                    ))
                    raise RuntimeError("synthetic rollback")
            assert conn.execute(
                "SELECT 1 FROM pg_roles WHERE rolname = %s",
                (rolled_back_role,),
            ).fetchone() is None
            _provision_runtime_role(conn, settings)
            _provision_chase_worker_role(conn, settings)

        with psycopg.connect(TEST_DSN) as conn:
            role_row = conn.execute(
                """SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole,
                          rolinherit, rolreplication, rolbypassrls
                     FROM pg_roles WHERE rolname = %s""",
                (worker_role,),
            ).fetchone()
            assert role_row == (True, False, False, False, False, False, False)
            assert conn.execute(
                """SELECT practice_id FROM runtime_practice_bindings
                    WHERE role_name = %s""",
                (worker_role,),
            ).fetchone()[0] == practice_a
            role_oid = conn.execute(
                "SELECT oid FROM pg_roles WHERE rolname = %s", (worker_role,),
            ).fetchone()[0]
            assert conn.execute(
                """SELECT count(*) FROM pg_auth_members
                    WHERE member = %s OR roleid = %s""",
                (role_oid, role_oid),
            ).fetchone()[0] == 0

        app_dsn = make_conninfo(TEST_DSN, user=app_role, password=app_password)
        with psycopg.connect(app_dsn) as conn:
            conn.execute(
                "SELECT set_config('cosmaneura.practice_id', %s, true)",
                (practice_a,),
            )
            assert conn.execute(
                "SELECT cosmaneura_chase_admin_allowed(%s::uuid, %s::uuid)",
                (tenant_a, admin_a),
            ).fetchone()[0] is True
            privileges = conn.execute(
                """SELECT
                       has_table_privilege(
                           current_user, 'chase_contact_snapshots', 'INSERT'
                       ),
                       has_table_privilege(
                           current_user, 'chase_policy_revisions', 'INSERT'
                       ),
                       has_table_privilege(
                           current_user, 'chase_policy_approval_events', 'INSERT'
                       ),
                       has_table_privilege(
                           current_user, 'chase_message_releases', 'INSERT'
                       ),
                       has_table_privilege(
                           current_user, 'chase_review_releases', 'INSERT'
                       ),
                       has_table_privilege(
                           current_user, 'chase_active_policies', 'INSERT'
                       ),
                       has_table_privilege(
                           current_user, 'chase_active_policies', 'UPDATE'
                       ),
                       has_table_privilege(
                           current_user, 'chase_active_policies', 'DELETE'
                       ),
                       has_table_privilege(
                           current_user, 'chase_cadence_states', 'SELECT'
                       ),
                       has_table_privilege(
                           current_user, 'chase_cadence_states', 'INSERT'
                       ),
                       has_table_privilege(
                           current_user, 'chase_cadence_states', 'UPDATE'
                       ),
                       has_table_privilege(
                           current_user, 'chase_cadence_states', 'DELETE'
                       ),
                       has_table_privilege(
                           current_user, 'chase_delivery_outbox', 'SELECT'
                       ),
                       has_table_privilege(
                           current_user, 'chase_delivery_outbox', 'INSERT'
                       ),
                       has_table_privilege(
                           current_user, 'chase_delivery_outbox', 'UPDATE'
                       ),
                       has_table_privilege(
                           current_user, 'chase_delivery_events', 'INSERT'
                       ),
                       has_function_privilege(
                           current_user,
                           'cosmaneura_record_accepted_delivery_attempt(uuid)',
                           'EXECUTE'
                       ),
                       has_function_privilege(
                           current_user,
                           'cosmaneura_cancel_revoked_chase_policy(uuid,text)',
                           'EXECUTE'
                       ),
                       has_function_privilege(
                           current_user,
                           'cosmaneura_cancel_stale_chase_work(text)',
                           'EXECUTE'
                       )"""
            ).fetchone()
            assert privileges == (
                True, True, True, True, True, True, True, True, True,
                False, True, True, False, False, False, False, False, True,
                True,
            )
            conn.execute(
                """INSERT INTO chase_items (
                       practice_id, chase_id, patient_ref, trigger, detected_at,
                       state, bucket, direct_contract
                   ) VALUES (%s, %s, 'SYNTHETIC-API-DETECTION',
                             'no_card_on_file', '2026-08-18T12:00:00+00:00',
                             'ready', 'missing', %s::jsonb)""",
                (
                    practice_a, chase_a,
                    '{"known":false,"blocked":true,'
                    f'"gate_fingerprint":"{gate_fingerprint}"}}',
                ),
            )
            conn.execute(
                """INSERT INTO chase_contact_snapshots (
                       practice_id, contact_snapshot_id, chase_id, source_system,
                       source_version, observed_at, verified_at,
                       verification_method, verified_by_tenant_id,
                       verified_by_user_id
                   ) VALUES (%s, %s, %s, 'synthetic_staff_entry', 'v1',
                             now() - interval '1 minute',
                             now() - interval '1 minute', 'staff', %s, %s)""",
                (practice_a, snapshot_id, chase_a, tenant_a, admin_a),
            )
            assert conn.execute(
                "SELECT cosmaneura_cancel_stale_chase_work(%s)",
                (chase_a,),
            ).fetchone()[0] == 0
            conn.execute(
                """INSERT INTO chase_contact_routes (
                       practice_id, contact_snapshot_id, channel, destination,
                       consent_status
                   ) VALUES (%s, %s, 'email', 'synthetic@example.invalid',
                             'not_required')""",
                (practice_a, snapshot_id),
            )
            conn.execute(
                """INSERT INTO chase_policy_revisions (
                       practice_id, policy_revision_id, policy_name, revision_no,
                       policy_fingerprint, approval_mode, practice_name,
                       practice_timezone, quiet_hours_start, quiet_hours_end,
                       max_touches, created_by_tenant_id, created_by_user_id,
                       created_at
                   ) VALUES (%s, %s, 'insurance-chase.v1', 1, %s,
                             'per_message', 'Synthetic Practice',
                             'America/New_York', '21:00', '08:00', 2,
                             %s, %s, now())""",
                (practice_a, policy_id, fingerprint, tenant_a, admin_a),
            )
            conn.execute(
                """INSERT INTO chase_policy_steps (
                       practice_id, policy_revision_id, step_ordinal, channel,
                       template_id, day_offset, performer, template_fingerprint
                   ) VALUES (%s, %s, 0, 'email', 'email.first', 0, 'system', %s),
                            (%s, %s, 1, 'phone', 'phone.call', 1, 'staff', %s)""",
                (
                    practice_a, policy_id, template_fingerprint,
                    practice_a, policy_id, "d" * 64,
                ),
            )
            conn.execute(
                """INSERT INTO chase_policy_approval_events (
                       practice_id, approval_event_id, policy_revision_id,
                       policy_fingerprint, event_type, actor_tenant_id,
                       actor_user_id, occurred_at, reason, payload_hash
                   ) VALUES (%s, %s, %s, %s, 'approved', %s, %s, now(), '', %s)""",
                (
                    practice_a, approval_id, policy_id, fingerprint,
                    tenant_a, admin_a, "e" * 64,
                ),
            )
            conn.execute(
                """INSERT INTO chase_active_policies (
                       practice_id, policy_revision_id, policy_fingerprint,
                       approval_event_id, activated_at, updated_at
                   ) VALUES (%s, %s, %s, %s, now(), now())""",
                (practice_a, policy_id, fingerprint, approval_id),
            )
            # EXECUTE is real (not merely visible in the ACL), while the
            # SECURITY DEFINER helper still refuses a currently approved
            # revision under the practice-bound runtime login.
            with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
                with conn.transaction():
                    conn.execute(
                        "SELECT cosmaneura_cancel_revoked_chase_policy(%s, %s)",
                        (policy_id, fingerprint),
                    )
            conn.execute(
                """UPDATE chase_active_policies
                      SET version = version + 1, updated_at = now()
                    WHERE practice_id = %s""",
                (practice_a,),
            )
            conn.execute(
                """INSERT INTO chase_review_releases (
                       practice_id, review_release_id, chase_id,
                       gate_fingerprint, actor_tenant_id, actor_user_id,
                       reason, released_at, payload_hash
                   ) VALUES (%s, %s, %s, %s, %s, %s,
                             'synthetic staff release', now(), %s)""",
                (
                    practice_a, review_release_id, chase_a, gate_fingerprint,
                    tenant_a, admin_a, "f" * 64,
                ),
            )
            conn.execute(
                """INSERT INTO chase_message_releases (
                       practice_id, release_id, chase_id, policy_revision_id,
                       policy_fingerprint, step_ordinal, contact_snapshot_id,
                       actor_tenant_id, actor_user_id, reason, released_at,
                       payload_hash
                   ) VALUES (%s, %s, %s, %s, %s, 0, %s, %s, %s,
                             'synthetic per-message release', now(), %s)""",
                (
                    practice_a, message_release_id, chase_a, policy_id,
                    fingerprint, snapshot_id, tenant_a, admin_a, "1" * 64,
                ),
            )
            assert conn.execute(
                "SELECT count(*) FROM chase_cadence_states"
            ).fetchone()[0] == 0
            _expect_insufficient_privilege(
                conn, "SELECT * FROM chase_delivery_outbox LIMIT 1",
            )
            assert conn.execute(
                "UPDATE chase_cadence_states SET version = version + 1 "
                "WHERE practice_id = %s",
                (practice_a,),
            ).rowcount == 0
            _expect_insufficient_privilege(
                conn,
                "INSERT INTO chase_delivery_events "
                "(practice_id, delivery_id, event_no, event_type, occurred_at) "
                "VALUES (%s, %s, 1, 'reserved', now())",
                (practice_a, uuid4()),
            )
            _expect_insufficient_privilege(
                conn,
                "UPDATE chase_contact_snapshots SET source_version = 'changed' "
                "WHERE practice_id = %s AND contact_snapshot_id = %s",
                (practice_a, snapshot_id),
            )

        worker_dsn = make_conninfo(
            TEST_DSN, user=worker_role, password=worker_password,
        )
        with psycopg.connect(worker_dsn) as conn:
            sms_schema = _relation_exists(conn, "chase_communication_policy_decisions")
            conn.execute(
                "SELECT set_config('cosmaneura.practice_id', %s, true)",
                (practice_a,),
            )
            assert conn.execute(
                "SELECT chase_id FROM chase_items ORDER BY chase_id"
            ).fetchall() == [(chase_a,)]
            assert conn.execute(
                "SELECT policy_revision_id FROM chase_active_policies"
            ).fetchone()[0] == policy_id
            worker_privileges = conn.execute(
                """SELECT
                       has_table_privilege(
                           current_user, 'chase_items', 'INSERT'
                       ),
                       has_table_privilege(
                           current_user, 'chase_contact_snapshots', 'INSERT'
                       ),
                       has_table_privilege(
                           current_user, 'chase_policy_revisions', 'INSERT'
                       ),
                       has_table_privilege(
                           current_user, 'chase_policy_approval_events', 'INSERT'
                       ),
                       has_table_privilege(
                           current_user, 'chase_message_releases', 'INSERT'
                       ),
                       has_table_privilege(
                           current_user, 'chase_review_releases', 'INSERT'
                       ),
                       has_table_privilege(
                           current_user, 'chase_active_policies', 'UPDATE'
                       ),
                       has_table_privilege(
                           current_user, 'chase_cadence_states', 'INSERT'
                       ),
                       has_table_privilege(
                           current_user, 'chase_delivery_outbox', 'INSERT'
                       ),
                       has_table_privilege(
                           current_user, 'chase_delivery_events', 'INSERT'
                       ),
                       has_table_privilege(
                           current_user, 'chase_attempts', 'INSERT'
                       ),
                       has_table_privilege(
                           current_user, 'chase_attempts', 'SELECT'
                       ),
                       has_table_privilege(
                           current_user, 'chase_pending_tasks', 'SELECT'
                       ),
                       has_table_privilege(
                           current_user, 'chase_pending_tasks', 'INSERT'
                       ),
                       has_table_privilege(
                           current_user, 'chase_pending_tasks', 'UPDATE'
                       ),
                       has_table_privilege(
                           current_user, 'chase_pending_tasks', 'DELETE'
                       ),
                       has_function_privilege(
                           current_user,
                           'cosmaneura_chase_admin_allowed(uuid, uuid)',
                           'EXECUTE'
                       ),
                       has_function_privilege(
                           current_user,
                           'cosmaneura_record_accepted_delivery_attempt(uuid)',
                           'EXECUTE'
                       ),
                       has_function_privilege(
                           current_user,
                           'cosmaneura_cancel_revoked_chase_policy(uuid,text)',
                           'EXECUTE'
                       ),
                       has_function_privilege(
                           current_user,
                           'cosmaneura_cancel_stale_chase_work(text)',
                           'EXECUTE'
                       )"""
            ).fetchone()
            assert worker_privileges == (
                False, False, False, False, False, False, False,
                True, True, True, False, sms_schema, True, True, False, False,
                sms_schema, True, False, False,
            )

            for table in (
                "practices",
                "users",
                "practice_memberships",
                "encounters",
                "coder_feedback",
                "coder_feedback_decisions",
                "access_audit_attempts",
                "access_audit_completions",
            ):
                _expect_insufficient_privilege(
                    conn,
                    sql.SQL("SELECT * FROM {} LIMIT 1").format(
                        sql.Identifier(table)
                    ),
                )
            _expect_insufficient_privilege(
                conn,
                "UPDATE runtime_practice_bindings SET practice_id = %s "
                "WHERE role_name = %s",
                (practice_b, worker_role),
            )
            if sms_schema:
                # SMS rechecks approval and touch evidence. These read checks
                # grant no staff-decision writes and remain practice-bound.
                assert conn.execute(
                    "SELECT cosmaneura_chase_admin_allowed(%s::uuid, %s::uuid)",
                    (tenant_a, admin_a),
                ).fetchone()[0] is True
                assert conn.execute(
                    "SELECT cosmaneura_chase_admin_allowed(%s::uuid, %s::uuid)",
                    (tenant_b, admin_b),
                ).fetchone()[0] is False
            else:
                _expect_insufficient_privilege(
                    conn,
                    "SELECT cosmaneura_chase_admin_allowed(%s::uuid, %s::uuid)",
                    (tenant_a, admin_a),
                )
            _expect_insufficient_privilege(
                conn,
                """INSERT INTO chase_items (
                       practice_id, chase_id, patient_ref, trigger, detected_at,
                       state, bucket
                   ) VALUES (%s, %s, 'FORBIDDEN-WORKER-DETECTION',
                             'manual', '2026-08-18T12:00:00+00:00',
                             'ready', 'missing')""",
                (practice_a, f"forbidden-{suffix}"),
            )
            _expect_insufficient_privilege(
                conn,
                """INSERT INTO chase_contact_snapshots (
                       practice_id, chase_id, source_system, source_version,
                       observed_at, verified_at, verification_method
                   ) VALUES (%s, %s, 'forbidden_worker_input', 'v1', now(),
                             now(), 'authoritative_source')""",
                (practice_a, chase_a),
            )
            conn.execute(
                """INSERT INTO chase_cadence_states (
                       practice_id, chase_id, policy_revision_id,
                       contact_snapshot_id, next_step_ordinal, next_due_at,
                       updated_at
                   ) VALUES (%s, %s, %s, %s, 0, now(), now())""",
                (practice_a, chase_a, policy_id, snapshot_id),
            )
            conn.execute(
                """INSERT INTO chase_pending_tasks (
                       practice_id, chase_id, payload
                   ) VALUES (%s, %s, %s::jsonb)""",
                (practice_a, chase_a, '{"kind":"staff_call"}'),
            )
            _expect_insufficient_privilege(
                conn,
                """UPDATE chase_pending_tasks
                      SET payload = %s::jsonb, updated_at = now()
                    WHERE practice_id = %s AND chase_id = %s""",
                ('{"kind":"staff_call","due":true}', practice_a, chase_a),
            )
            _expect_insufficient_privilege(
                conn,
                "DELETE FROM chase_pending_tasks "
                "WHERE practice_id = %s AND chase_id = %s",
                (practice_a, chase_a),
            )

            delivery_id = conn.execute(
                """INSERT INTO chase_delivery_outbox (
                       practice_id, chase_id, policy_revision_id,
                       policy_step_ordinal, approval_event_id,
                       policy_fingerprint, approval_mode, message_release_id,
                       gate_fingerprint, gate_disposition, review_release_id,
                       contact_snapshot_id, channel, template_id, provider,
                       local_delivery_date, state, available_at, created_at,
                       updated_at
                   ) VALUES (%s, %s, %s, 0, %s, %s, 'per_message', %s, %s,
                             'released', %s, %s, 'email', 'email.first',
                             'azure_communication_services_email',
                             '2026-08-18', 'reserved', now(), now(), now())
                   RETURNING delivery_id""",
                (
                    practice_a, chase_a, policy_id, approval_id, fingerprint,
                    message_release_id, gate_fingerprint, review_release_id,
                    snapshot_id,
                ),
            ).fetchone()[0]
            conn.execute(
                """INSERT INTO chase_delivery_events (
                       practice_id, delivery_id, event_no, event_type, occurred_at
                   ) VALUES (%s, %s, 1, 'reserved', now())""",
                (practice_a, delivery_id),
            )
            lease_token = uuid4()
            conn.execute(
                """UPDATE chase_delivery_outbox
                      SET state = 'submitting', lease_token = %s,
                          lease_expires_at = now() + interval '5 minutes',
                          submission_started_at = now(), updated_at = now()
                    WHERE practice_id = %s AND delivery_id = %s""",
                (lease_token, practice_a, delivery_id),
            )
            conn.execute(
                """INSERT INTO chase_delivery_events (
                       practice_id, delivery_id, event_no, event_type, occurred_at
                   ) VALUES (%s, %s, 2, 'submitting', now())""",
                (practice_a, delivery_id),
            )
            conn.execute(
                """UPDATE chase_delivery_outbox
                      SET state = 'accepted', lease_token = NULL,
                          lease_expires_at = NULL, finalized_at = now(),
                          provider_message_id = 'synthetic-provider-message',
                          updated_at = now()
                    WHERE practice_id = %s AND delivery_id = %s""",
                (practice_a, delivery_id),
            )
            conn.execute(
                """INSERT INTO chase_delivery_events (
                       practice_id, delivery_id, event_no, event_type, occurred_at,
                       provider_message_id
                   ) VALUES (%s, %s, 3, 'accepted', now(),
                             'synthetic-provider-message')""",
                (practice_a, delivery_id),
            )
            _expect_insufficient_privilege(
                conn,
                """INSERT INTO chase_attempts (
                       practice_id, chase_id, attempt_no, channel, template_id,
                       attempted_at, actor, outcome, delivery_id
                   ) VALUES (%s, %s, 1, 'email', 'email.first',
                             '2026-08-18T12:01:00+00:00', 'system',
                             'accepted', %s)""",
                (practice_a, chase_a, delivery_id),
            )
            assert conn.execute(
                "SELECT cosmaneura_record_accepted_delivery_attempt(%s)",
                (delivery_id,),
            ).fetchone()[0] == 1
            if sms_schema:
                assert conn.execute(
                    "SELECT attempt_no FROM chase_attempts "
                    "WHERE practice_id = %s AND delivery_id = %s",
                    (practice_a, delivery_id),
                ).fetchall() == [(1,)]
            else:
                _expect_insufficient_privilege(
                    conn,
                    "SELECT attempt_no FROM chase_attempts "
                    "WHERE practice_id = %s AND delivery_id = %s",
                    (practice_a, delivery_id),
                )
            assert conn.execute(
                "SELECT cosmaneura_record_accepted_delivery_attempt(%s)",
                (delivery_id,),
            ).fetchone()[0] == 1
            conn.execute(
                """UPDATE chase_cadence_states
                      SET next_due_at = now() + interval '1 day',
                          version = version + 1, updated_at = now()
                    WHERE practice_id = %s AND chase_id = %s""",
                (practice_a, chase_a),
            )
            conn.execute(
                """UPDATE chase_items
                      SET state = 'in_progress', version = version + 1,
                          updated_at = now()
                    WHERE practice_id = %s AND chase_id = %s""",
                (practice_a, chase_a),
            )

            _expect_insufficient_privilege(
                conn,
                "UPDATE chase_contact_snapshots SET source_version = 'changed' "
                "WHERE practice_id = %s AND contact_snapshot_id = %s",
                (practice_a, snapshot_id),
            )
            _expect_insufficient_privilege(
                conn,
                "DELETE FROM chase_delivery_events "
                "WHERE practice_id = %s AND delivery_id = %s",
                (practice_a, delivery_id),
            )
            _expect_insufficient_privilege(
                conn,
                "UPDATE chase_attempts SET outcome = 'changed' "
                "WHERE practice_id = %s AND chase_id = %s",
                (practice_a, chase_a),
            )
            _expect_insufficient_privilege(
                conn,
                "UPDATE chase_items SET patient_ref = 'changed' "
                "WHERE practice_id = %s AND chase_id = %s",
                (practice_a, chase_a),
            )
            _expect_insufficient_privilege(
                conn,
                "UPDATE chase_items SET direct_contract = '{}'::jsonb "
                "WHERE practice_id = %s AND chase_id = %s",
                (practice_a, chase_a),
            )
            _expect_insufficient_privilege(
                conn,
                "UPDATE chase_active_policies SET version = version + 1 "
                "WHERE practice_id = %s",
                (practice_a,),
            )
            _expect_insufficient_privilege(
                conn,
                """INSERT INTO chase_policy_approval_events (
                       practice_id, approval_event_id, policy_revision_id,
                       policy_fingerprint, event_type, actor_tenant_id,
                       actor_user_id, occurred_at, reason, payload_hash
                   ) VALUES (%s, %s, %s, %s, 'revoked', %s, %s, now(),
                             'not authorized', %s)""",
                (
                    practice_a, uuid4(), policy_id, fingerprint,
                    tenant_a, admin_a, "f" * 64,
                ),
            )

            conn.execute(
                "SELECT set_config('cosmaneura.practice_id', %s, true)",
                (practice_b,),
            )
            assert conn.execute("SELECT count(*) FROM chase_items").fetchone()[0] == 0
            assert conn.execute(
                "SELECT count(*) FROM chase_contact_snapshots"
            ).fetchone()[0] == 0
            switched = conn.execute(
                """UPDATE chase_items
                      SET state = 'in_progress', version = version + 1,
                          updated_at = now()
                    WHERE practice_id = %s AND chase_id = %s""",
                (practice_b, chase_b),
            )
            assert switched.rowcount == 0

        with psycopg.connect(app_dsn) as conn:
            conn.execute(
                "SELECT set_config('cosmaneura.practice_id', %s, true)",
                (practice_a,),
            )
            assert conn.execute(
                "SELECT count(*) FROM chase_items"
            ).fetchone()[0] == 1
            assert conn.execute(
                """UPDATE chase_items SET priority = priority
                    WHERE practice_id = %s AND chase_id = %s
                    RETURNING chase_id""",
                (practice_a, chase_a),
            ).fetchone()[0] == chase_a
            assert conn.execute(
                """UPDATE chase_cadence_states
                      SET next_due_at = next_due_at, version = version + 1,
                          updated_at = now()
                    WHERE practice_id = %s AND chase_id = %s
                    RETURNING chase_id""",
                (practice_a, chase_a),
            ).fetchone()[0] == chase_a
            assert conn.execute(
                """DELETE FROM chase_pending_tasks
                    WHERE practice_id = %s AND chase_id = %s
                    RETURNING chase_id""",
                (practice_a, chase_a),
            ).fetchone()[0] == chase_a

        with psycopg.connect(TEST_DSN, autocommit=True) as conn:
            with pytest.raises(RuntimeError, match="bound to another practice"):
                with conn.transaction():
                    _provision_chase_worker_role(conn, Settings(
                        database_url=TEST_DSN,
                        chase_worker_database_role=worker_role,
                        chase_worker_database_password=worker_password,
                        practice_id=practice_b,
                    ))
            assert conn.execute(
                """SELECT practice_id FROM runtime_practice_bindings
                    WHERE role_name = %s""",
                (worker_role,),
            ).fetchone()[0] == practice_a
    finally:
        _remove_test_scope(
            practice_ids=(practice_a, practice_b),
            tenant_ids=(tenant_a, tenant_b),
            roles=(app_role, worker_role, rolled_back_role),
        )
