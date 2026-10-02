"""P4 delivery-runtime schema invariants and optional PostgreSQL checks."""
from __future__ import annotations

import os
import re
from pathlib import Path
from uuid import uuid4

import pytest

from cosmaneura_billing.migrations import migration_body
from postgres_test_support import isolated_database


ROOT = Path(__file__).resolve().parents[1]
UP_PATH = ROOT / "migrations" / "004_chase_delivery_runtime.up.sql"
DOWN_PATH = ROOT / "migrations" / "004_chase_delivery_runtime.down.sql"
UP_SQL = UP_PATH.read_text(encoding="utf-8")
DOWN_SQL = DOWN_PATH.read_text(encoding="utf-8")

DOMAIN_TABLES = (
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


def _table_definition(sql: str, table: str) -> str:
    match = re.search(
        rf"CREATE TABLE {re.escape(table)}\s*\((.*?)\n\);",
        sql,
        flags=re.DOTALL,
    )
    assert match is not None, f"missing CREATE TABLE for {table}"
    return match.group(1)


def test_every_p4_domain_table_leads_with_practice_id_and_forces_rls():
    for table in DOMAIN_TABLES:
        definition = _table_definition(UP_SQL, table)
        first_column = next(
            line.strip() for line in definition.splitlines() if line.strip()
        )
        assert first_column.startswith("practice_id")
        assert f"CREATE POLICY {table}_practice_isolation" in UP_SQL
        assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;" in UP_SQL
        assert f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;" in UP_SQL


def test_schema_persists_no_message_content_url_or_generic_route_json():
    outbox = _table_definition(UP_SQL, "chase_delivery_outbox").casefold()
    events = _table_definition(UP_SQL, "chase_delivery_events").casefold()
    policy = _table_definition(UP_SQL, "chase_policy_revisions").casefold()
    routes = _table_definition(UP_SQL, "chase_contact_routes").casefold()

    for forbidden in (
        "subject", "body", "rendered", "message_content", "upload_url",
        "destination", "recipient",
    ):
        assert forbidden not in outbox
        assert forbidden not in events
    assert "upload_url" not in policy
    assert " json" not in routes
    assert "destination" in routes


def test_policy_fingerprints_bind_unpersisted_full_configuration():
    normalized = " ".join(UP_SQL.casefold().split())
    for phrase in (
        "complete canonical policy",
        "full template body",
        "practice name",
        "upload url",
        "cadence",
        "quiet hours",
        "timezone",
        "provider/resource identity",
        "enabled channel set",
        "sender and reply-to addresses",
        "contact max-age",
        "operational-enabled latch",
        "worker recomputes this exact fingerprint",
    ):
        assert phrase in normalized
    assert "template_fingerprint" in _table_definition(
        UP_SQL, "chase_policy_steps"
    )


def test_migration_approves_schedules_and_reserves_nothing():
    statements = " ".join(
        line for line in UP_SQL.splitlines()
        if not line.lstrip().startswith("--")
    ).casefold()
    assert "insert into chase_policy_approval_events" not in statements
    assert "insert into chase_active_policies" not in statements
    assert "insert into chase_cadence_states" not in statements
    assert "insert into chase_delivery_outbox" not in statements


def test_delivery_idempotency_and_exact_approval_are_database_constraints():
    outbox = " ".join(
        _table_definition(UP_SQL, "chase_delivery_outbox").split()
    )
    active = " ".join(
        _table_definition(UP_SQL, "chase_active_policies").split()
    )
    releases = " ".join(
        _table_definition(UP_SQL, "chase_message_releases").split()
    )
    assert (
        "UNIQUE ( practice_id, chase_id, policy_revision_id, "
        "policy_step_ordinal )"
    ) in outbox
    assert "UNIQUE (practice_id, chase_id, local_delivery_date)" in outbox
    assert "approval_event_type = 'approved'" in outbox
    assert "approval_mode = 'per_message'" in outbox
    assert "message_release_id IS NOT NULL" in outbox
    assert "gate_disposition = 'clear'" in outbox
    assert "gate_disposition = 'released'" in outbox
    assert "review_release_id IS NOT NULL" in outbox
    assert "PRIMARY KEY (practice_id)" in active
    assert "approval_event_type = 'approved'" in active
    assert (
        "UNIQUE ( practice_id, chase_id, policy_revision_id, step_ordinal, "
        "contact_snapshot_id )"
    ) in releases


def test_accepted_delivery_links_to_exactly_one_contact_log_row():
    normalized_up = " ".join(UP_SQL.split())
    normalized_down = " ".join(DOWN_SQL.split())
    assert "ALTER TABLE chase_attempts ADD COLUMN delivery_id uuid" in normalized_up
    assert "UNIQUE (practice_id, delivery_id)" in normalized_up
    assert (
        "UNIQUE (practice_id, delivery_id, chase_id)"
    ) in normalized_up
    assert (
        "FOREIGN KEY ( practice_id, delivery_id, chase_id, channel, "
        "template_id ) REFERENCES chase_delivery_outbox ( practice_id, "
        "delivery_id, chase_id, channel, template_id )"
    ) in normalized_up
    assert "DROP COLUMN IF EXISTS delivery_id" in normalized_down
    assert "CREATE TRIGGER chase_attempts_accepted_delivery_guard" in UP_SQL
    assert "delivery.state = 'accepted'" in UP_SQL
    assert DOWN_SQL.index("ALTER TABLE chase_attempts") < DOWN_SQL.index(
        "DROP TABLE IF EXISTS chase_delivery_outbox"
    )
    transition_guard = UP_SQL.partition(
        "CREATE FUNCTION cosmaneura_validate_delivery_outbox_update"
    )[2]
    transition_guard = transition_guard.partition(
        "CREATE TRIGGER chase_delivery_outbox_transition_guard"
    )[0]
    assert "'accepted', 'ambiguous', 'permanent_failure', 'cancelled'" not in (
        transition_guard
    )
    assert "OLD.state = 'ambiguous'" in transition_guard
    assert "'accepted', 'permanent_failure'" in transition_guard
    assert "OLD.lease_expires_at > statement_timestamp()" in transition_guard
    assert "active delivery lease cannot be quarantined" in transition_guard


def test_immutable_history_and_admin_authorization_are_enforced_in_sql():
    for table in (
        "chase_contact_snapshots",
        "chase_contact_routes",
        "chase_policy_revisions",
        "chase_policy_steps",
        "chase_policy_approval_events",
        "chase_message_releases",
        "chase_review_releases",
        "chase_delivery_events",
    ):
        assert f"CREATE TRIGGER {table}_immutable" in UP_SQL
    assert "CREATE FUNCTION cosmaneura_chase_admin_allowed" in UP_SQL
    assert "CREATE TRIGGER chase_policy_steps_sealed_after_approval" in UP_SQL
    assert "CREATE TRIGGER chase_policy_approval_events_revision_lock" in UP_SQL
    assert "CREATE FUNCTION cosmaneura_lock_policy_revision_for_approval" in UP_SQL
    step_lock = UP_SQL.partition(
        "CREATE FUNCTION cosmaneura_reject_approved_policy_step_insert"
    )[2].partition("CREATE TRIGGER chase_policy_steps_sealed_after_approval")[0]
    approval_lock = UP_SQL.partition(
        "CREATE FUNCTION cosmaneura_lock_policy_revision_for_approval"
    )[2].partition("CREATE TRIGGER chase_policy_approval_events_revision_lock")[0]
    assert "FOR UPDATE" in step_lock
    assert "FOR UPDATE" in approval_lock
    assert "'Admin' = ANY (membership.roles)" in UP_SQL
    assert (
        "REVOKE ALL ON FUNCTION cosmaneura_chase_admin_allowed(uuid, uuid) "
        "FROM PUBLIC;"
    ) in UP_SQL
    for table in (
        "chase_policy_approval_events",
        "chase_message_releases",
        "chase_review_releases",
    ):
        definition = _table_definition(UP_SQL, table)
        assert "payload_hash" in definition
        assert "^[0-9a-f]{64}$" in definition


def test_down_migration_removes_only_p4_objects():
    lowered = DOWN_SQL.casefold()
    for table in DOMAIN_TABLES:
        assert f"drop table if exists {table}" in lowered
    for preserved in (
        "practices", "users", "practice_memberships", "chase_items",
        "chase_attempts", "chase_pending_tasks", "chase_resolutions",
        "chase_action_receipts",
    ):
        assert f"drop table if exists {preserved};" not in lowered
    assert "alter table chase_items" not in lowered


TEST_DSN = os.environ.get("COSMANEURA_TEST_DATABASE_URL", "")


@pytest.mark.skipif(
    not TEST_DSN, reason="COSMANEURA_TEST_DATABASE_URL is not configured",
)
def test_live_postgres_upgrade_preserves_p3_row_and_enforces_p4_contract():
    psycopg = pytest.importorskip("psycopg")
    practice_id = f"p4-migration-{uuid4()}"
    chase_id = f"chase-{uuid4()}"
    other_chase_id = f"chase-{uuid4()}"
    tenant_id = uuid4()
    admin_id = uuid4()
    coder_id = uuid4()
    policy_id = uuid4()
    snapshot_id = uuid4()
    approval_id = uuid4()
    first_delivery_id = uuid4()
    per_message_policy_id = uuid4()
    per_message_approval_id = uuid4()
    message_release_id = uuid4()
    review_release_id = uuid4()

    with isolated_database(TEST_DSN, "p4") as dsn, psycopg.connect(dsn, autocommit=True) as conn:
        if "test" not in (conn.info.dbname or "").casefold():
            pytest.fail(
                "COSMANEURA_TEST_DATABASE_URL must name a disposable test database"
            )
        migrations = ROOT / "migrations"
        if conn.execute(
            "SELECT to_regclass('public.chase_items')"
        ).fetchone()[0] is None:
            conn.execute(
                (migrations / "002_chase_persistence.up.sql").read_text(
                    encoding="utf-8"
                ),
                prepare=False,
            )
        if conn.execute(
            "SELECT to_regclass('public.encounters')"
        ).fetchone()[0] is None:
            conn.execute(
                (migrations / "003_pilot_persistence.up.sql").read_text(
                    encoding="utf-8"
                ),
                prepare=False,
            )

        # Exercise both directions on the historical 002/003 boundary in a private
        # database. Later shared-schema foreign keys must not be dropped or bypassed.
        conn.execute("BEGIN")
        try:
            conn.execute(migration_body(DOWN_SQL), prepare=False)
            conn.execute(
                "SELECT set_config('cosmaneura.practice_id', %s, true)",
                (practice_id,),
            )
            conn.execute(
                """INSERT INTO practices (
                       practice_id, entra_tenant_id, display_name
                   ) VALUES (%s, %s, 'Synthetic P4 migration')""",
                (practice_id, tenant_id),
            )
            for user_id in (admin_id, coder_id):
                conn.execute(
                    "INSERT INTO users (tenant_id, user_id) VALUES (%s, %s)",
                    (tenant_id, user_id),
                )
            conn.execute(
                """INSERT INTO practice_memberships (
                       practice_id, tenant_id, user_id, roles
                   ) VALUES (%s, %s, %s, ARRAY['Admin']::text[]),
                            (%s, %s, %s, ARRAY['Coder']::text[])""",
                (
                    practice_id, tenant_id, admin_id,
                    practice_id, tenant_id, coder_id,
                ),
            )
            conn.execute(
                """INSERT INTO chase_items (
                       practice_id, chase_id, patient_ref, trigger,
                       detected_at, state, bucket
                   ) VALUES (%s, %s, 'SYNTHETIC-P4', 'no_card_on_file',
                             '2026-08-18T12:00:00+00:00', 'ready', 'missing')""",
                (practice_id, chase_id),
            )
            conn.execute(
                """INSERT INTO chase_items (
                       practice_id, chase_id, patient_ref, trigger,
                       detected_at, state, bucket
                   ) VALUES (%s, %s, 'SYNTHETIC-P4-OTHER', 'no_card_on_file',
                             '2026-08-18T12:00:00+00:00', 'ready', 'missing')""",
                (practice_id, other_chase_id),
            )
            conn.execute(
                """INSERT INTO chase_attempts (
                       practice_id, chase_id, attempt_no, channel, template_id,
                       attempted_at, actor, outcome
                   ) VALUES (%s, %s, 1, 'phone', 'phone.call',
                             '2026-08-18T12:00:00+00:00', 'synthetic-staff',
                             'no_answer')""",
                (practice_id, chase_id),
            )

            conn.execute(migration_body(UP_SQL), prepare=False)

            assert conn.execute(
                "SELECT state FROM chase_items "
                "WHERE practice_id = %s AND chase_id = %s",
                (practice_id, chase_id),
            ).fetchone()[0] == "ready"
            assert conn.execute(
                "SELECT count(*) FROM chase_policy_approval_events "
                "WHERE practice_id = %s",
                (practice_id,),
            ).fetchone()[0] == 0
            assert conn.execute(
                "SELECT count(*) FROM chase_active_policies "
                "WHERE practice_id = %s",
                (practice_id,),
            ).fetchone()[0] == 0
            assert conn.execute(
                "SELECT count(*) FROM chase_cadence_states "
                "WHERE practice_id = %s",
                (practice_id,),
            ).fetchone()[0] == 0
            assert conn.execute(
                "SELECT count(*) FROM chase_delivery_outbox "
                "WHERE practice_id = %s",
                (practice_id,),
            ).fetchone()[0] == 0
            assert conn.execute(
                """SELECT delivery_id FROM chase_attempts
                    WHERE practice_id = %s AND chase_id = %s AND attempt_no = 1""",
                (practice_id, chase_id),
            ).fetchone()[0] is None

            assert conn.execute(
                "SELECT cosmaneura_chase_admin_allowed(%s, %s)",
                (tenant_id, admin_id),
            ).fetchone()[0] is True
            assert conn.execute(
                "SELECT cosmaneura_chase_admin_allowed(%s, %s)",
                (tenant_id, coder_id),
            ).fetchone()[0] is False

            forced = dict(conn.execute(
                """SELECT relname, relrowsecurity AND relforcerowsecurity
                     FROM pg_class
                    WHERE relname = ANY(%s)""",
                (list(DOMAIN_TABLES),),
            ).fetchall())
            assert forced == {table: True for table in DOMAIN_TABLES}

            route_columns = dict(conn.execute(
                """SELECT column_name, data_type
                     FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = 'chase_contact_routes'"""
            ).fetchall())
            assert route_columns["destination"] == "text"
            assert "json" not in route_columns.values()
            outbox_columns = set(conn.execute(
                """SELECT column_name FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = 'chase_delivery_outbox'"""
            ).fetchall())
            assert not outbox_columns.intersection({
                ("destination",), ("recipient",), ("subject",), ("body",),
                ("upload_url",),
            })

            conn.execute(
                """INSERT INTO chase_contact_snapshots (
                       practice_id, contact_snapshot_id, chase_id,
                       source_system, source_version, observed_at, verified_at,
                       verification_method, created_at
                   ) VALUES (
                       %s, %s, %s, 'synthetic-test', 'v1',
                       '2026-08-18T12:00:00+00:00',
                       '2026-08-18T12:01:00+00:00',
                       'authoritative_source',
                       '2026-08-18T12:01:00+00:00'
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
                       practice_id, policy_revision_id, policy_name,
                       revision_no, policy_fingerprint, approval_mode,
                       practice_name, practice_timezone, quiet_hours_start,
                       quiet_hours_end, max_touches,
                       created_by_tenant_id, created_by_user_id, created_at
                   ) VALUES (
                       %s, %s, 'insurance-chase.v1', 1, %s, 'per_policy',
                       'Synthetic Practice', 'America/New_York', '21:00', '08:00',
                       4, %s, %s, '2026-08-18T12:02:00+00:00'
                   )""",
                (practice_id, policy_id, "a" * 64, tenant_id, admin_id),
            )
            conn.execute(
                """INSERT INTO chase_policy_steps (
                       practice_id, policy_revision_id, step_ordinal, channel,
                       template_id, day_offset, performer, template_fingerprint
                   ) VALUES
                       (%s, %s, 0, 'email', 'email.first', 0, 'system', %s),
                       (%s, %s, 1, 'email', 'email.reminder', 3, 'system', %s)""",
                (
                    practice_id, policy_id, "b" * 64,
                    practice_id, policy_id, "c" * 64,
                ),
            )
            conn.execute(
                """INSERT INTO chase_policy_approval_events (
                       practice_id, approval_event_id, policy_revision_id,
                       policy_fingerprint, event_type, actor_tenant_id,
                       actor_user_id, occurred_at, reason, payload_hash
                   ) VALUES (%s, %s, %s, %s, 'approved', %s, %s,
                             '2026-08-18T12:03:00+00:00', '', %s)""",
                (
                    practice_id, approval_id, policy_id, "a" * 64,
                    tenant_id, admin_id, "e" * 64,
                ),
            )
            with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
                with conn.transaction():
                    conn.execute(
                        """INSERT INTO chase_policy_steps (
                               practice_id, policy_revision_id, step_ordinal,
                               channel, template_id, day_offset, performer,
                               template_fingerprint
                           ) VALUES (%s, %s, 2, 'email', 'email.late', 5,
                                     'system', %s)""",
                        (practice_id, policy_id, "1" * 64),
                    )
            conn.execute(
                """INSERT INTO chase_active_policies (
                       practice_id, policy_revision_id, policy_fingerprint,
                       approval_event_id, activated_at, updated_at
                   ) VALUES (%s, %s, %s, %s,
                             '2026-08-18T12:03:00+00:00',
                             '2026-08-18T12:03:00+00:00')""",
                (practice_id, policy_id, "a" * 64, approval_id),
            )
            conn.execute(
                """INSERT INTO chase_cadence_states (
                       practice_id, chase_id, policy_revision_id,
                       contact_snapshot_id, next_step_ordinal, next_due_at,
                       updated_at
                   ) VALUES (%s, %s, %s, %s, 0,
                             '2026-08-18T13:00:00+00:00',
                             '2026-08-18T12:03:00+00:00')""",
                (practice_id, chase_id, policy_id, snapshot_id),
            )
            conn.execute(
                """INSERT INTO chase_delivery_outbox (
                       practice_id, delivery_id, provider_operation_id,
                       chase_id, policy_revision_id, policy_step_ordinal,
                       approval_event_id, policy_fingerprint,
                       approval_mode, gate_fingerprint, gate_disposition,
                       contact_snapshot_id, channel, template_id, provider,
                       local_delivery_date, state, available_at,
                       created_at, updated_at
                   ) VALUES (%s, %s, %s, %s, %s, 0, %s, %s,
                             'per_policy', %s, 'clear', %s,
                             'email', 'email.first',
                             'azure_communication_services_email',
                             '2026-08-18', 'reserved',
                             '2026-08-18T13:00:00+00:00',
                             '2026-08-18T12:04:00+00:00',
                             '2026-08-18T12:04:00+00:00')""",
                (
                    practice_id, first_delivery_id, uuid4(), chase_id,
                    policy_id, approval_id, "a" * 64, "d" * 64, snapshot_id,
                ),
            )
            with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
                with conn.transaction():
                    conn.execute(
                        """INSERT INTO chase_attempts (
                               practice_id, chase_id, attempt_no, channel,
                               template_id, attempted_at, actor, outcome,
                               delivery_id
                           ) VALUES (%s, %s, 2, 'email', 'email.first',
                                     '2026-08-18T12:04:00+00:00', 'system',
                                     'accepted', %s)""",
                        (practice_id, chase_id, first_delivery_id),
                    )
            lease_token = uuid4()
            conn.execute(
                """UPDATE chase_delivery_outbox
                      SET state = 'submitting', lease_token = %s,
                          lease_expires_at = statement_timestamp()
                              - interval '1 second',
                          submission_started_at = statement_timestamp()
                              - interval '2 seconds',
                          updated_at = statement_timestamp()
                    WHERE practice_id = %s AND delivery_id = %s""",
                (lease_token, practice_id, first_delivery_id),
            )
            with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
                with conn.transaction():
                    conn.execute(
                        """UPDATE chase_delivery_outbox
                              SET state = 'submitting', lease_token = %s,
                                  lease_expires_at = statement_timestamp()
                                      + interval '10 minutes',
                                  updated_at = statement_timestamp()
                            WHERE practice_id = %s AND delivery_id = %s""",
                        (uuid4(), practice_id, first_delivery_id),
                    )
            with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
                with conn.transaction():
                    conn.execute(
                        """UPDATE chase_delivery_outbox
                              SET state = 'cancelled', lease_token = NULL,
                                  lease_expires_at = NULL,
                                  finalized_at = '2026-08-18T13:01:00+00:00',
                                  updated_at = '2026-08-18T13:01:00+00:00'
                            WHERE practice_id = %s AND delivery_id = %s""",
                        (practice_id, first_delivery_id),
                    )
            conn.execute(
                """UPDATE chase_delivery_outbox
                      SET state = 'ambiguous', lease_token = NULL,
                          lease_expires_at = NULL,
                          finalized_at = statement_timestamp(),
                          error_code = 'lease_expired_unreconciled',
                          updated_at = statement_timestamp()
                    WHERE practice_id = %s AND delivery_id = %s""",
                (practice_id, first_delivery_id),
            )
            with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
                with conn.transaction():
                    conn.execute(
                        """INSERT INTO chase_attempts (
                               practice_id, chase_id, attempt_no, channel,
                               template_id, attempted_at, actor, outcome,
                               delivery_id
                           ) VALUES (%s, %s, 2, 'email', 'email.first',
                                     '2026-08-18T13:01:00+00:00', 'system',
                                     'accepted', %s)""",
                        (practice_id, chase_id, first_delivery_id),
                    )
            with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
                with conn.transaction():
                    conn.execute(
                        """UPDATE chase_delivery_outbox
                              SET state = 'reserved', submission_started_at = NULL,
                                  finalized_at = NULL, error_code = NULL,
                                  updated_at = statement_timestamp()
                            WHERE practice_id = %s AND delivery_id = %s""",
                        (practice_id, first_delivery_id),
                    )
            with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
                with conn.transaction():
                    conn.execute(
                        """UPDATE chase_delivery_outbox
                              SET state = 'cancelled',
                                  submission_started_at = NULL,
                                  finalized_at = statement_timestamp(),
                                  updated_at = statement_timestamp()
                            WHERE practice_id = %s AND delivery_id = %s""",
                        (practice_id, first_delivery_id),
                    )
            conn.execute(
                """UPDATE chase_delivery_outbox
                      SET state = 'accepted',
                          finalized_at = statement_timestamp(),
                          provider_message_id = 'synthetic-acs-id',
                          error_code = NULL,
                          updated_at = statement_timestamp()
                    WHERE practice_id = %s AND delivery_id = %s""",
                (practice_id, first_delivery_id),
            )
            with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
                with conn.transaction():
                    conn.execute(
                        """INSERT INTO chase_attempts (
                               practice_id, chase_id, attempt_no, channel,
                               template_id, attempted_at, actor, outcome,
                               delivery_id
                           ) VALUES (%s, %s, 1, 'email', 'email.first',
                                     '2026-08-18T13:01:00+00:00', 'system',
                                     'accepted', %s)""",
                        (practice_id, other_chase_id, first_delivery_id),
                    )
            with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
                with conn.transaction():
                    conn.execute(
                        """INSERT INTO chase_attempts (
                               practice_id, chase_id, attempt_no, channel,
                               template_id, attempted_at, actor, outcome,
                               delivery_id
                           ) VALUES (%s, %s, 2, 'sms', 'sms.first',
                                     '2026-08-18T13:01:00+00:00', 'system',
                                     'accepted', %s)""",
                        (practice_id, chase_id, first_delivery_id),
                    )
            conn.execute(
                """INSERT INTO chase_attempts (
                       practice_id, chase_id, attempt_no, channel, template_id,
                       attempted_at, actor, outcome, delivery_id
                   ) VALUES (%s, %s, 2, 'email', 'email.first',
                             '2026-08-18T13:01:00+00:00', 'system', 'accepted',
                             %s)""",
                (practice_id, chase_id, first_delivery_id),
            )
            with pytest.raises(psycopg.errors.UniqueViolation):
                with conn.transaction():
                    conn.execute(
                        """INSERT INTO chase_attempts (
                               practice_id, chase_id, attempt_no, channel,
                               template_id, attempted_at, actor, outcome,
                               delivery_id
                           ) VALUES (%s, %s, 3, 'email', 'email.first',
                                     '2026-08-18T13:01:00+00:00', 'system',
                                     'accepted', %s)""",
                        (practice_id, chase_id, first_delivery_id),
                    )

            conn.execute(
                """INSERT INTO chase_policy_revisions (
                       practice_id, policy_revision_id, policy_name,
                       revision_no, policy_fingerprint, approval_mode,
                       practice_name, practice_timezone, quiet_hours_start,
                       quiet_hours_end, max_touches,
                       created_by_tenant_id, created_by_user_id, created_at
                   ) VALUES (
                       %s, %s, 'insurance-chase.v1', 2, %s, 'per_message',
                       'Synthetic Practice', 'America/New_York', '21:00', '08:00',
                       4, %s, %s, '2026-08-18T12:05:00+00:00'
                   )""",
                (
                    practice_id, per_message_policy_id, "2" * 64,
                    tenant_id, admin_id,
                ),
            )
            conn.execute(
                """INSERT INTO chase_policy_steps (
                       practice_id, policy_revision_id, step_ordinal, channel,
                       template_id, day_offset, performer, template_fingerprint
                   ) VALUES (%s, %s, 0, 'email', 'email.first', 0,
                             'system', %s)""",
                (practice_id, per_message_policy_id, "3" * 64),
            )
            conn.execute(
                """INSERT INTO chase_policy_approval_events (
                       practice_id, approval_event_id, policy_revision_id,
                       policy_fingerprint, event_type, actor_tenant_id,
                       actor_user_id, occurred_at, reason, payload_hash
                   ) VALUES (%s, %s, %s, %s, 'approved', %s, %s,
                             '2026-08-18T12:06:00+00:00', '', %s)""",
                (
                    practice_id, per_message_approval_id,
                    per_message_policy_id, "2" * 64, tenant_id, admin_id,
                    "4" * 64,
                ),
            )

            with pytest.raises(psycopg.errors.CheckViolation):
                with conn.transaction():
                    conn.execute(
                        """INSERT INTO chase_delivery_outbox (
                               practice_id, delivery_id,
                               provider_operation_id, chase_id,
                               policy_revision_id, policy_step_ordinal,
                               approval_event_id, policy_fingerprint,
                               approval_mode, gate_fingerprint,
                               gate_disposition, contact_snapshot_id, channel,
                               template_id, provider, local_delivery_date,
                               state, available_at, created_at, updated_at
                           ) VALUES (%s, %s, %s, %s, %s, 0, %s, %s,
                                     'per_message', %s, 'clear', %s, 'email',
                                     'email.first',
                                     'azure_communication_services_email',
                                     '2026-08-19', 'reserved', now(), now(), now())""",
                        (
                            practice_id, uuid4(), uuid4(), chase_id,
                            per_message_policy_id, per_message_approval_id,
                            "2" * 64, "7" * 64, snapshot_id,
                        ),
                    )

            conn.execute(
                """INSERT INTO chase_message_releases (
                       practice_id, release_id, chase_id, policy_revision_id,
                       policy_fingerprint, step_ordinal, contact_snapshot_id,
                       actor_tenant_id, actor_user_id, reason, released_at,
                       payload_hash
                   ) VALUES (%s, %s, %s, %s, %s, 0, %s, %s, %s,
                             'synthetic per-message release',
                             '2026-08-18T12:07:00+00:00', %s)""",
                (
                    practice_id, message_release_id, chase_id,
                    per_message_policy_id, "2" * 64, snapshot_id,
                    tenant_id, admin_id, "6" * 64,
                ),
            )
            conn.execute(
                """INSERT INTO chase_review_releases (
                       practice_id, review_release_id, chase_id,
                       gate_fingerprint, actor_tenant_id, actor_user_id,
                       reason, released_at, payload_hash
                   ) VALUES (%s, %s, %s, %s, %s, %s,
                             'synthetic direct-contract review',
                             '2026-08-18T12:07:00+00:00', %s)""",
                (
                    practice_id, review_release_id, chase_id, "7" * 64,
                    tenant_id, admin_id, "5" * 64,
                ),
            )
            conn.execute(
                """INSERT INTO chase_delivery_outbox (
                       practice_id, delivery_id, provider_operation_id,
                       chase_id, policy_revision_id, policy_step_ordinal,
                       approval_event_id, policy_fingerprint, approval_mode,
                       message_release_id, gate_fingerprint, gate_disposition,
                       review_release_id, contact_snapshot_id, channel,
                       template_id, provider, local_delivery_date, state,
                       available_at, created_at, updated_at
                   ) VALUES (%s, %s, %s, %s, %s, 0, %s, %s,
                             'per_message', %s, %s, 'released', %s, %s,
                             'email', 'email.first',
                             'azure_communication_services_email',
                             '2026-08-19', 'reserved', now(), now(), now())""",
                (
                    practice_id, uuid4(), uuid4(), chase_id,
                    per_message_policy_id, per_message_approval_id,
                    "2" * 64, message_release_id, "7" * 64,
                    review_release_id, snapshot_id,
                ),
            )

            with pytest.raises(psycopg.errors.UniqueViolation):
                with conn.transaction():
                    conn.execute(
                        """INSERT INTO chase_delivery_outbox (
                               practice_id, delivery_id, provider_operation_id,
                               chase_id, policy_revision_id,
                               policy_step_ordinal, approval_event_id,
                               policy_fingerprint, approval_mode,
                               gate_fingerprint, gate_disposition,
                               contact_snapshot_id, channel,
                               template_id, provider, local_delivery_date, state,
                               available_at, created_at, updated_at
                           ) VALUES (%s, %s, %s, %s, %s, 1, %s, %s,
                                     'per_policy', %s, 'clear', %s,
                                     'email', 'email.reminder',
                                     'azure_communication_services_email',
                                     '2026-08-18', 'reserved', now(), now(), now())""",
                        (
                            practice_id, uuid4(), uuid4(), chase_id, policy_id,
                            approval_id, "a" * 64, "d" * 64, snapshot_id,
                        ),
                    )

            conn.execute(
                """INSERT INTO chase_delivery_events (
                       practice_id, delivery_id, event_no, event_type, occurred_at
                   ) VALUES (%s, %s, 1, 'reserved',
                             '2026-08-18T12:04:00+00:00')""",
                (practice_id, first_delivery_id),
            )
            with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
                with conn.transaction():
                    conn.execute(
                        """UPDATE chase_delivery_events SET event_type = 'accepted'
                            WHERE practice_id = %s AND delivery_id = %s""",
                        (practice_id, first_delivery_id),
                    )

            # The checked event type plus exact composite FK prevents a
            # revocation event from becoming an active pointer.
            revoke_id = uuid4()
            conn.execute(
                """INSERT INTO chase_policy_approval_events (
                       practice_id, approval_event_id, policy_revision_id,
                       policy_fingerprint, event_type, actor_tenant_id,
                       actor_user_id, occurred_at, reason, payload_hash
                   ) VALUES (%s, %s, %s, %s, 'revoked', %s, %s,
                             '2026-08-18T12:05:00+00:00', 'synthetic revoke', %s)""",
                (
                    practice_id, revoke_id, policy_id, "a" * 64,
                    tenant_id, admin_id, "f" * 64,
                ),
            )
            with pytest.raises(psycopg.errors.ForeignKeyViolation):
                with conn.transaction():
                    conn.execute(
                        """UPDATE chase_active_policies
                              SET approval_event_id = %s, version = version + 1,
                                  updated_at = '2026-08-18T12:05:00+00:00'
                            WHERE practice_id = %s""",
                        (revoke_id, practice_id),
                    )

            # Rollback of 004 leaves every P3-owned row/table untouched.
            conn.execute(migration_body(DOWN_SQL), prepare=False)
            assert conn.execute(
                "SELECT state FROM chase_items "
                "WHERE practice_id = %s AND chase_id = %s",
                (practice_id, chase_id),
            ).fetchone()[0] == "ready"
            assert conn.execute(
                """SELECT count(*) FROM chase_attempts
                    WHERE practice_id = %s AND chase_id = %s""",
                (practice_id, chase_id),
            ).fetchone()[0] == 2
        finally:
            conn.execute("ROLLBACK")
