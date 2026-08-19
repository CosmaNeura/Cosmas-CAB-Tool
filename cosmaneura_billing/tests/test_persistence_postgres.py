"""PostgreSQL integration tests for pilot persistence and tenant isolation.

Set ``COSMANEURA_TEST_DATABASE_URL`` to a disposable database whose name
contains ``test``. CI provisions one; local runs skip this module when unset.
Every test uses generated practice, tenant, encounter, review, and actor IDs and
removes only those rows during teardown.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import uuid4

import pytest

psycopg = pytest.importorskip("psycopg")
from psycopg import sql
from psycopg.conninfo import make_conninfo

from cosmaneura_billing.access_audit import AccessAuditEvent
from cosmaneura_billing.auth import AuthPrincipal, PrincipalRoleError
from cosmaneura_billing.coder_feedback import (
    Action,
    CodeDecision,
    EncounterFeedback,
    FeedbackConflict,
    Reason,
)
from cosmaneura_billing.persistence import (
    PersistenceUnavailable,
    PostgresAccessAuditStore,
    PostgresContext,
    PostgresEncounterStore,
    PostgresFeedbackStore,
    PostgresMembershipStore,
    ReviewerNotAuthorized,
    StaleEncounter,
)
from cosmaneura_billing.preferences import PreferenceStore
from cosmaneura_billing.schemas import VisitBundle
from cosmaneura_billing.settings import Settings
from cosmaneura_billing.migrations import (
    _bootstrap_practice_admin,
    _provision_runtime_role,
)


TEST_DSN = os.environ.get("COSMANEURA_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(
    not TEST_DSN, reason="COSMANEURA_TEST_DATABASE_URL is not configured",
)


def _relation_exists(conn, name: str) -> bool:
    return conn.execute(
        "SELECT to_regclass(%s)", (f"public.{name}",),
    ).fetchone()[0] is not None


@pytest.fixture(scope="session", autouse=True)
def _pilot_schema():
    if not TEST_DSN:
        yield
        return
    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        if "test" not in (conn.info.dbname or "").lower():
            pytest.fail(
                "COSMANEURA_TEST_DATABASE_URL must name a disposable test database"
            )
        migrations = Path(__file__).resolve().parents[1] / "migrations"
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
    yield


@dataclass(frozen=True)
class PilotScope:
    practice_a: str
    tenant_a: str
    coder_a: str
    admin_a: str
    practice_b: str
    tenant_b: str
    coder_b: str

    def context_a(self) -> PostgresContext:
        return PostgresContext(TEST_DSN, practice_id=self.practice_a)

    def context_b(self) -> PostgresContext:
        return PostgresContext(TEST_DSN, practice_id=self.practice_b)


def _provision_practice(
    practice_id: str,
    tenant_id: str,
    memberships: dict[str, list[str]],
) -> None:
    with psycopg.connect(TEST_DSN) as conn:
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
        for user_id, roles in memberships.items():
            conn.execute(
                """INSERT INTO users (tenant_id, user_id)
                   VALUES (%s::uuid, %s::uuid)""",
                (tenant_id, user_id),
            )
            conn.execute(
                """INSERT INTO practice_memberships (
                       practice_id, tenant_id, user_id, roles
                   ) VALUES (%s, %s::uuid, %s::uuid, %s)""",
                (practice_id, tenant_id, user_id, roles),
            )


def _remove_practice(practice_id: str, tenant_id: str) -> None:
    assert practice_id.startswith("p3-test-")
    with psycopg.connect(TEST_DSN) as conn:
        conn.execute(
            "SELECT set_config('cosmaneura.practice_id', %s, true)",
            (practice_id,),
        )
        conn.execute(
            "DELETE FROM access_audit_completions WHERE practice_id = %s",
            (practice_id,),
        )
        conn.execute(
            "DELETE FROM access_audit_attempts WHERE practice_id = %s",
            (practice_id,),
        )
        conn.execute(
            "DELETE FROM coder_feedback_decisions WHERE practice_id = %s",
            (practice_id,),
        )
        conn.execute(
            "DELETE FROM coder_feedback WHERE practice_id = %s",
            (practice_id,),
        )
        conn.execute(
            "DELETE FROM encounters WHERE practice_id = %s", (practice_id,),
        )
        conn.execute(
            "DELETE FROM practice_memberships WHERE practice_id = %s",
            (practice_id,),
        )
        conn.execute(
            "DELETE FROM users WHERE tenant_id = %s::uuid", (tenant_id,),
        )
        conn.execute(
            "DELETE FROM practices WHERE practice_id = %s", (practice_id,),
        )


@pytest.fixture()
def pilot_scope():
    suffix = uuid4().hex
    scope = PilotScope(
        practice_a=f"p3-test-a-{suffix}",
        tenant_a=str(uuid4()),
        coder_a=str(uuid4()),
        admin_a=str(uuid4()),
        practice_b=f"p3-test-b-{suffix}",
        tenant_b=str(uuid4()),
        coder_b=str(uuid4()),
    )
    _provision_practice(
        scope.practice_a,
        scope.tenant_a,
        {
            scope.coder_a: ["Coder"],
            scope.admin_a: ["Admin"],
        },
    )
    _provision_practice(
        scope.practice_b,
        scope.tenant_b,
        {scope.coder_b: ["Coder"]},
    )
    yield scope
    _remove_practice(scope.practice_a, scope.tenant_a)
    _remove_practice(scope.practice_b, scope.tenant_b)


def _bundle(encounter_id: str, signed_note: str) -> VisitBundle:
    return VisitBundle(
        encounter_id=encounter_id,
        patient_age=44,
        patient_sex="female",
        encounter_type="office_established",
        place_of_service="11",
        intake_text="Synthetic intake only.",
        transcript="Synthetic transcript only.",
        signed_note=signed_note,
        provider_specialty="Family Medicine",
        submitted_em_code="99213",
        submitted_icd10=["K59.00"],
    )


def _feedback(
    scope: PilotScope,
    encounter_id: str,
    *,
    review_id: str | None = None,
    note: str = "synthetic review",
) -> EncounterFeedback:
    return EncounterFeedback(
        encounter_id=encounter_id,
        coder_id=scope.coder_a,
        engine_version="p3-integration-test",
        submitted_em="99213",
        engine_em="99213",
        final_em="99213",
        decisions=[
            CodeDecision(
                kind="ICD10",
                engine_code="K59.00",
                action=Action.REJECT,
                final_code=None,
                reason_code=Reason.DX_NOT_SUPPORTED,
                note=note,
            )
        ],
        time_spent_seconds=12.5,
        overall_note=note,
        review_id=review_id or str(uuid4()),
        coder_tenant_id=scope.tenant_a,
        encounter_version=1,
    )


@contextmanager
def _row_security_reader():
    """Yield a no-bypass role when the DSN identity itself can bypass RLS."""
    role_name: str | None = None
    with psycopg.connect(TEST_DSN) as conn:
        superuser, bypass_rls = conn.execute(
            """SELECT rolsuper, rolbypassrls
                 FROM pg_roles WHERE rolname = current_user"""
        ).fetchone()
    if superuser or bypass_rls:
        role_name = f"p3_rls_{uuid4().hex}"
        with psycopg.connect(TEST_DSN, autocommit=True) as conn:
            conn.execute(
                sql.SQL("CREATE ROLE {} NOLOGIN NOSUPERUSER NOBYPASSRLS").format(
                    sql.Identifier(role_name)
                )
            )
            conn.execute(
                sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(
                    sql.Identifier(role_name)
                )
            )
            conn.execute(
                sql.SQL(
                    "GRANT SELECT ON practices, encounters TO {}"
                ).format(sql.Identifier(role_name))
            )
            conn.execute(
                sql.SQL(
                    "GRANT EXECUTE ON FUNCTION "
                    "cosmaneura_practice_allowed(text) TO {}"
                ).format(sql.Identifier(role_name))
            )
    try:
        yield role_name
    finally:
        if role_name is not None:
            with psycopg.connect(TEST_DSN, autocommit=True) as conn:
                conn.execute(
                    sql.SQL(
                        "REVOKE EXECUTE ON FUNCTION "
                        "cosmaneura_practice_allowed(text) FROM {}"
                    ).format(sql.Identifier(role_name))
                )
                conn.execute(
                    sql.SQL("REVOKE ALL ON practices, encounters FROM {}").format(
                        sql.Identifier(role_name)
                    )
                )
                conn.execute(
                    sql.SQL("REVOKE USAGE ON SCHEMA public FROM {}").format(
                        sql.Identifier(role_name)
                    )
                )
                conn.execute(
                    sql.SQL("DROP ROLE {}").format(sql.Identifier(role_name))
                )


def test_encounters_round_trip_update_and_database_rls(pilot_scope):
    encounter_id = f"SYNTHETIC-{uuid4().hex}"
    store_a = PostgresEncounterStore(pilot_scope.context_a())
    store_b = PostgresEncounterStore(pilot_scope.context_b())

    original_a = _bundle(encounter_id, "Synthetic note for practice A.")
    original_b = _bundle(encounter_id, "Synthetic note for practice B.")
    first = store_a.upsert(
        original_a,
        source="synthetic",
        ingested_by_tenant_id=pilot_scope.tenant_a,
        ingested_by_user_id=pilot_scope.admin_a,
    )
    store_b.upsert(original_b, source="synthetic")

    assert first.bundle == original_a
    assert store_b.get(encounter_id).bundle == original_b
    assert store_a.all() == [first]

    replayed = store_a.upsert(
        original_a,
        source="synthetic",
        ingested_by_tenant_id=pilot_scope.tenant_a,
        ingested_by_user_id=pilot_scope.admin_a,
    )
    assert replayed == first

    updated = replace(
        original_a,
        signed_note="Updated synthetic note for practice A.",
        submitted_icd10=["K59.00", "R51.9"],
    )
    second = store_a.upsert(updated, source="synthetic")

    assert second.resource_id == first.resource_id
    assert second.version == first.version + 1
    assert second.bundle == updated
    assert store_b.get(encounter_id).bundle.signed_note == original_b.signed_note

    with _row_security_reader() as role_name:
        with psycopg.connect(TEST_DSN) as conn:
            if role_name is not None:
                conn.execute(
                    sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(role_name))
                )
            # Missing server-owned practice context is fail-closed.
            assert conn.execute("SELECT count(*) FROM encounters").fetchone()[0] == 0
            conn.execute(
                "SELECT set_config('cosmaneura.practice_id', %s, true)",
                (pilot_scope.practice_a,),
            )
            visible = conn.execute(
                "SELECT practice_id, signed_note FROM encounters"
            ).fetchall()
            assert visible == [
                (pilot_scope.practice_a, updated.signed_note),
            ]


def test_runtime_role_is_bound_to_one_practice_and_cannot_erase_history(
    pilot_scope,
):
    encounter_a = f"A-{uuid4().hex}"
    PostgresEncounterStore(pilot_scope.context_a()).upsert(
        _bundle(encounter_a, "Synthetic practice A."), source="synthetic",
    )
    PostgresEncounterStore(pilot_scope.context_b()).upsert(
        _bundle(f"B-{uuid4().hex}", "Synthetic practice B."), source="synthetic",
    )
    role = f"p3_app_{uuid4().hex[:20]}"
    password = f"P3-test-{uuid4().hex}"
    try:
        with psycopg.connect(TEST_DSN) as conn:
            conn.execute(
                "SELECT set_config('cosmaneura.practice_id', %s, true)",
                (pilot_scope.practice_a,),
            )
            _provision_runtime_role(conn, Settings(
                database_url=TEST_DSN,
                database_runtime_role=role,
                database_runtime_password=password,
                practice_id=pilot_scope.practice_a,
            ))

        with psycopg.connect(TEST_DSN, user=role, password=password) as conn:
            conn.execute(
                "SELECT set_config('cosmaneura.practice_id', %s, true)",
                (pilot_scope.practice_a,),
            )
            assert conn.execute("SELECT count(*) FROM encounters").fetchone()[0] == 1
            conn.execute(
                "SELECT set_config('cosmaneura.practice_id', %s, true)",
                (pilot_scope.practice_b,),
            )
            assert conn.execute("SELECT count(*) FROM encounters").fetchone()[0] == 0
            privileges = conn.execute(
                """SELECT
                    has_table_privilege(current_user, 'chase_items', 'DELETE'),
                    has_table_privilege(current_user, 'chase_attempts', 'UPDATE'),
                    has_table_privilege(current_user, 'chase_attempts', 'DELETE'),
                    has_table_privilege(
                        current_user, 'access_audit_attempts', 'SELECT'
                    ),
                    has_any_column_privilege(
                        current_user, 'access_audit_attempts', 'SELECT'
                    ),
                    has_table_privilege(
                        current_user, 'access_audit_attempts', 'UPDATE'
                    ),
                    has_table_privilege(
                        current_user, 'access_audit_attempts', 'DELETE'
                    ),
                    has_table_privilege(
                        current_user, 'access_audit_completions', 'SELECT'
                    ),
                    has_any_column_privilege(
                        current_user, 'access_audit_completions', 'SELECT'
                    ),
                    has_table_privilege(
                        current_user, 'access_audit_completions', 'UPDATE'
                    ),
                    has_table_privilege(
                        current_user, 'access_audit_completions', 'DELETE'
                    ),
                    has_table_privilege(
                        current_user, 'practice_memberships', 'UPDATE'
                    ),
                    has_table_privilege(current_user, 'users', 'UPDATE'
                    )"""
            ).fetchone()
            assert privileges == (False,) * 13

        runtime_context = PostgresContext(
            make_conninfo(TEST_DSN, user=role, password=password),
            practice_id=pilot_scope.practice_a,
        )
        assert PostgresFeedbackStore(runtime_context).append(
            _feedback(pilot_scope, encounter_a),
        ) is True
        assert PostgresMembershipStore(
            runtime_context, tenant_id=pilot_scope.tenant_a,
        ).validate_assignee(pilot_scope.admin_a) == pilot_scope.admin_a
        denied_event = AccessAuditEvent(
            event_id=str(uuid4()),
            occurred_at="2026-08-18T12:00:00+00:00",
            practice_id=pilot_scope.practice_a,
            actor_tenant_id="",
            actor_user_id="",
            actor_roles=(),
            action="api.access",
            resource_id="",
            route="unmatched_api_route",
            method="GET",
            status=401,
            outcome="denied",
            source_ip="127.0.0.1",
        )
        PostgresAccessAuditStore(runtime_context).append(denied_event)
        with pilot_scope.context_a().connection() as conn:
            audited = conn.execute(
                """SELECT
                       (SELECT count(*) FROM access_audit_attempts
                         WHERE practice_id = %s AND event_id = %s::uuid)
                           AS attempt_count,
                       (SELECT count(*) FROM access_audit_completions
                         WHERE practice_id = %s AND event_id = %s::uuid)
                           AS completion_count""",
                (
                    pilot_scope.practice_a,
                    denied_event.event_id,
                    pilot_scope.practice_a,
                    denied_event.event_id,
                ),
            ).fetchone()
        assert tuple(audited.values()) == (1, 1)

        with pytest.raises(RuntimeError, match="bound to another practice"):
            with psycopg.connect(TEST_DSN) as conn:
                conn.execute(
                    "SELECT set_config('cosmaneura.practice_id', %s, true)",
                    (pilot_scope.practice_b,),
                )
                _provision_runtime_role(conn, Settings(
                    database_url=TEST_DSN,
                    database_runtime_role=role,
                    database_runtime_password=password,
                    practice_id=pilot_scope.practice_b,
                ))
    finally:
        with psycopg.connect(TEST_DSN, autocommit=True) as conn:
            conn.execute(
                "DELETE FROM runtime_practice_bindings WHERE role_name = %s",
                (role,),
            )
            conn.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
            conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


def test_membership_is_explicit_and_intersects_asserted_roles(pilot_scope):
    memberships_a = PostgresMembershipStore(
        pilot_scope.context_a(), tenant_id=pilot_scope.tenant_a,
    )
    memberships_b = PostgresMembershipStore(
        pilot_scope.context_b(), tenant_id=pilot_scope.tenant_b,
    )
    asserted = AuthPrincipal(
        user_id=pilot_scope.coder_a,
        tenant_id=pilot_scope.tenant_a,
        practice_id=pilot_scope.practice_a,
        roles=frozenset({"Coder", "Admin"}),
    )

    authorized = memberships_a.authorize(asserted)

    assert authorized.practice_id == pilot_scope.practice_a
    assert authorized.roles == frozenset({"Coder"})
    memberships_a.healthcheck()
    assert memberships_a.validate_assignee(pilot_scope.admin_a) == pilot_scope.admin_a
    with pilot_scope.context_a().connection() as locked_connection:
        assert memberships_a.validate_assignee(
            pilot_scope.admin_a, connection=locked_connection,
        ) == pilot_scope.admin_a
        with psycopg.connect(TEST_DSN, autocommit=True) as concurrent:
            concurrent.execute(
                "SELECT set_config('cosmaneura.practice_id', %s, false)",
                (pilot_scope.practice_a,),
            )
            concurrent.execute("SET lock_timeout TO '100ms'")
            with pytest.raises(psycopg.errors.LockNotAvailable):
                concurrent.execute(
                    """UPDATE practice_memberships SET active = false
                        WHERE practice_id = %s AND user_id = %s::uuid""",
                    (pilot_scope.practice_a, pilot_scope.admin_a),
                )
    with pytest.raises(ValueError, match="not active billing staff"):
        memberships_a.validate_assignee(pilot_scope.coder_a)
    with pytest.raises(PrincipalRoleError):
        memberships_a.authorize(replace(asserted, roles=frozenset({"Provider"})))
    with pytest.raises(PrincipalRoleError):
        memberships_b.authorize(asserted)
    with pytest.raises(PersistenceUnavailable, match="administrator"):
        memberships_b.healthcheck()


def test_bootstrap_never_reactivates_or_rebinds_the_initial_admin(pilot_scope):
    settings = Settings(
        database_url=TEST_DSN,
        entra_tenant_id=pilot_scope.tenant_a,
        practice_id=pilot_scope.practice_a,
        practice_name="Synthetic practice A",
        bootstrap_admin_oid=pilot_scope.admin_a,
    )
    with psycopg.connect(TEST_DSN) as conn:
        conn.execute(
            "SELECT set_config('cosmaneura.practice_id', %s, true)",
            (pilot_scope.practice_a,),
        )
        conn.execute(
            """UPDATE practice_memberships SET active = false
                WHERE practice_id = %s AND user_id = %s::uuid""",
            (pilot_scope.practice_a, pilot_scope.admin_a),
        )
        conn.execute(
            """UPDATE users SET active = false
                WHERE tenant_id = %s::uuid AND user_id = %s::uuid""",
            (pilot_scope.tenant_a, pilot_scope.admin_a),
        )
        _bootstrap_practice_admin(conn, settings)
        active = conn.execute(
            """SELECT u.active, pm.active
                 FROM users u
                 JOIN practice_memberships pm
                   ON pm.tenant_id = u.tenant_id AND pm.user_id = u.user_id
                WHERE pm.practice_id = %s AND u.user_id = %s::uuid""",
            (pilot_scope.practice_a, pilot_scope.admin_a),
        ).fetchone()
        assert active == (False, False)

    with pytest.raises(RuntimeError, match="bound to another tenant"):
        with psycopg.connect(TEST_DSN) as conn:
            _bootstrap_practice_admin(conn, Settings(
                database_url=TEST_DSN,
                entra_tenant_id=pilot_scope.tenant_b,
                practice_id=pilot_scope.practice_a,
                bootstrap_admin_oid=pilot_scope.coder_b,
            ))


def test_feedback_round_trip_exact_replay_and_conflict(pilot_scope):
    encounter_id = f"SYNTHETIC-{uuid4().hex}"
    PostgresEncounterStore(pilot_scope.context_a()).upsert(
        _bundle(encounter_id, "Synthetic feedback encounter."), source="synthetic",
    )
    store = PostgresFeedbackStore(pilot_scope.context_a())
    review_id = str(uuid4())
    feedback = _feedback(pilot_scope, encounter_id, review_id=review_id)

    assert store.append(feedback) is True
    assert store.append(feedback) is False
    PostgresEncounterStore(pilot_scope.context_a()).upsert(
        _bundle(encounter_id, "Updated synthetic feedback encounter."),
        source="synthetic",
    )
    assert store.append(feedback) is False
    assert store.count() == 1
    loaded = store.load()
    assert len(loaded) == 1
    assert loaded[0].review_id == review_id
    assert loaded[0].encounter_id == encounter_id
    assert loaded[0].coder_id == pilot_scope.coder_a
    assert loaded[0].coder_tenant_id == pilot_scope.tenant_a
    assert loaded[0].decisions == feedback.decisions

    conflicting = _feedback(
        pilot_scope,
        encounter_id,
        review_id=review_id,
        note="different synthetic payload",
    )
    with pytest.raises(FeedbackConflict, match="review ID conflicts"):
        store.append(conflicting)
    assert store.count() == 1


def test_feedback_rejects_inactive_reviewer_and_stale_encounter(pilot_scope):
    context = pilot_scope.context_a()
    encounter_id = f"SYNTHETIC-{uuid4().hex}"
    encounters = PostgresEncounterStore(context)
    first = encounters.upsert(
        _bundle(encounter_id, "Synthetic reviewer-state encounter."),
        source="synthetic",
    )
    stale = _feedback(pilot_scope, encounter_id)
    store = PostgresFeedbackStore(context)
    assert store.append(stale) is True
    encounters.upsert(
        _bundle(encounter_id, "Updated synthetic reviewer-state encounter."),
        source="synthetic",
    )
    assert store.append(stale) is False
    with pytest.raises(StaleEncounter):
        store.append(replace(stale, review_id=str(uuid4())))

    with context.connection() as conn:
        conn.execute(
            """UPDATE practice_memberships SET active = false
                WHERE practice_id = %s AND user_id = %s::uuid""",
            (pilot_scope.practice_a, pilot_scope.coder_a),
        )
    current = replace(stale, review_id=str(uuid4()), encounter_version=first.version + 1)
    assert store.append(stale) is False
    with pytest.raises(ReviewerNotAuthorized):
        store.append(current)


def test_feedback_and_audit_completion_commit_or_roll_back_together(pilot_scope):
    context = pilot_scope.context_a()
    encounter_id = f"SYNTHETIC-{uuid4().hex}"
    encounter = PostgresEncounterStore(context).upsert(
        _bundle(encounter_id, "Synthetic atomic feedback encounter."),
        source="synthetic",
    )
    feedback_store = PostgresFeedbackStore(context)
    audit_store = PostgresAccessAuditStore(context)
    event_id = str(uuid4())
    attempt = AccessAuditEvent(
        event_id=event_id,
        occurred_at="2026-08-18T12:00:00+00:00",
        practice_id=pilot_scope.practice_a,
        actor_tenant_id=pilot_scope.tenant_a,
        actor_user_id=pilot_scope.coder_a,
        actor_roles=("Coder",),
        action="feedback.submit",
        resource_id="",
        route="/api/feedback",
        method="POST",
        status=0,
        outcome="attempted",
        source_ip="127.0.0.1",
    )
    completion = replace(
        attempt,
        status=200,
        outcome="allowed",
        resource_id=encounter.resource_id,
    )
    audit_store.append(attempt)

    assert feedback_store.append_with_audit(
        _feedback(pilot_scope, encounter_id), completion,
    ) is True

    with context.connection() as conn:
        counts = conn.execute(
            """SELECT
                   (SELECT count(*) FROM coder_feedback
                     WHERE practice_id = %s) AS feedback_count,
                   (SELECT count(*) FROM access_audit_attempts
                     WHERE practice_id = %s AND event_id = %s::uuid) AS attempts,
                   (SELECT count(*) FROM access_audit_completions
                     WHERE practice_id = %s AND event_id = %s::uuid) AS completions""",
            (
                pilot_scope.practice_a,
                pilot_scope.practice_a,
                event_id,
                pilot_scope.practice_a,
                event_id,
            ),
        ).fetchone()
    assert tuple(counts.values()) == (1, 1, 1)

    orphan_completion = replace(
        completion,
        event_id=str(uuid4()),
        occurred_at="2026-08-18T12:01:00+00:00",
    )
    with pytest.raises(PersistenceUnavailable):
        feedback_store.append_with_audit(
            _feedback(pilot_scope, encounter_id), orphan_completion,
        )
    # The completion foreign-key failure rolls the preceding feedback insert
    # back; an audit-less review cannot survive the transaction.
    assert feedback_store.count() == 1


def test_preferences_refresh_after_feedback_from_another_replica(pilot_scope):
    encounter_id = f"SYNTHETIC-{uuid4().hex}"
    context = pilot_scope.context_a()
    PostgresEncounterStore(context).upsert(
        _bundle(encounter_id, "Synthetic preference encounter."),
        source="synthetic",
    )
    reader_replica = PostgresFeedbackStore(context)
    writer_replica = PostgresFeedbackStore(pilot_scope.context_a())
    preferences = PreferenceStore(
        min_count=3,
        global_min_users=3,
        settings=Settings(),
        feedback_store=reader_replica,
    )

    assert preferences.for_user(pilot_scope.coder_a).empty
    for _ in range(2):
        writer_replica.append(_feedback(pilot_scope, encounter_id))
        assert preferences.for_user(pilot_scope.coder_a).empty

    writer_replica.append(_feedback(pilot_scope, encounter_id))
    resolved = preferences.for_user(pilot_scope.coder_a)

    assert resolved.suppress["K5900"].count == 3
    assert resolved.suppress["K5900"].scope == "you"
    assert not resolved.refine
