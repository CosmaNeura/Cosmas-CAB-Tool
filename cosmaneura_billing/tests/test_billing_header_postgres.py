"""Disposable local PostgreSQL rehearsal; never use a staging/production DSN.

Explicit BILLING_HEADER_TEST_DSN is required. Synthetic rows are retained in the
disposable DB because append-only history is not removed as test cleanup.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import os
from pathlib import Path
import re
from uuid import uuid4

import pytest

psycopg = pytest.importorskip("psycopg")

from cosmaneura_billing import billing_header as h
from cosmaneura_billing.access_audit import AccessAuditEvent
from cosmaneura_billing.billing_header_store import PostgresBillingHeaderStore
from cosmaneura_billing.persistence import (
    PersistenceUnavailable, PostgresAccessAuditStore, PostgresContext, PostgresEncounterStore,
)
from cosmaneura_billing.schemas import VisitBundle
from test_billing_header import generated, NOW

DSN = os.environ.get("BILLING_HEADER_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="explicit disposable billing-header PostgreSQL is absent")


@pytest.fixture(scope="module", autouse=True)
def schema():
    if not DSN:
        return
    with psycopg.connect(DSN, autocommit=True) as connection:
        # Do not silently turn a generic configured database into this rehearsal.
        assert re.fullmatch(r"cab_header_test(?:_[a-f0-9]{8})?", connection.info.dbname)
        assert connection.info.host.startswith("/private/tmp")
        paths = Path(__file__).resolve().parents[1] / "migrations"
        for table, filename in (("chase_items", "002_chase_persistence.up.sql"),
                                ("encounters", "003_pilot_persistence.up.sql"),
                                ("billing_headers", "011_billing_headers.up.sql"),
                                ("billing_header_signoffs", "013_billing_header_signoffs.up.sql")):
            if connection.execute("SELECT to_regclass(%s)", ("public." + table,)).fetchone()[0] is None:
                connection.execute((paths / filename).read_text(), prepare=False)


@pytest.fixture
def setup():
    practice = "header-test-" + uuid4().hex
    tenant, provider, coder = str(uuid4()), str(uuid4()), str(uuid4())
    context = PostgresContext(DSN, practice_id=practice)
    with context.connection() as connection:
        connection.execute("INSERT INTO practices(practice_id,entra_tenant_id) VALUES(%s,%s::uuid)", (practice, tenant))
        for user, role in ((provider, "Provider"), (coder, "Coder")):
            connection.execute("INSERT INTO users(tenant_id,user_id) VALUES(%s::uuid,%s::uuid)", (tenant, user))
            connection.execute("INSERT INTO practice_memberships(practice_id,tenant_id,user_id,roles) VALUES(%s,%s::uuid,%s::uuid,%s)",
                               (practice, tenant, user, [role]))
    stored = PostgresEncounterStore(context).upsert(VisitBundle(
        "SYN-HEADER", 30, "female", "office_established", "11",
        signed_note="Headache synthetic test only.", transcript="Synthetic source."), source="synthetic")
    store = PostgresBillingHeaderStore(context, clock=lambda: NOW)
    actor = h.HeaderActor(tenant, provider, "Synthetic Provider", "Provider")
    coder_actor = h.HeaderActor(tenant, coder, "Synthetic Coder", "Coder")
    return store, context, stored, actor, coder_actor


def completion(setup, action, actor=None, *, reserve=True):
    _, context, stored, default_actor, _ = setup
    actor = actor or default_actor
    event = AccessAuditEvent(str(uuid4()), NOW, context.practice_id, actor.tenant_id, actor.user_id,
                             (actor.role,), action, stored.resource_id,
                             "/api/billing-header/" + action.rsplit(".", 1)[-1], "POST", 200, "allowed", "127.0.0.1")
    if reserve:
        PostgresAccessAuditStore(context).append(replace(event, status=0, outcome="attempted"))
    return event


def create(setup, **kwargs):
    store, _, stored, actor, _ = setup
    event = kwargs["audit_completion"] if "audit_completion" in kwargs else completion(setup, "billing_header.compute")
    return store.create(stored.bundle.encounter_id, expected_source=h.source_snapshot(stored),
                        generated=generated(stored), actor=actor, operation_id=str(uuid4()),
                        audit_completion=event)


def save(setup, current, *, actor=None, operation=None, reason="Human verification", reserve=True):
    store, _, stored, default_actor, _ = setup
    actor = actor or default_actor
    draft = h.create_draft(current["header"], current["revision"])
    draft["facts"]["consent"] += "Human entry pending verification."
    return store.save(stored.bundle.encounter_id, header_id=current["header"]["headerId"],
                      expected_source=h.source_snapshot(stored), expected_revision=current["revisionNumber"],
                      draft=draft, reason=reason, actor=actor, operation_id=operation or str(uuid4()),
                      audit_completion=completion(setup, "billing_header.save", actor, reserve=reserve))


def test_pg_both_roles_history_exact_replay_and_immutable_note(setup):
    store, context, stored, _, coder = setup
    first, created = create(setup)
    assert created and first["revisionNumber"] == 0
    operation = str(uuid4())
    revised, created = save(setup, first, actor=coder, operation=operation)
    assert created and revised["revision"]["actor"]["role"] == "coder"
    second, _ = save(setup, revised)
    replay, created = save(setup, first, actor=coder, operation=operation)
    assert not created and replay == second
    with pytest.raises(h.BillingHeaderError, match="operation_conflict"):
        save(setup, first, actor=coder, operation=operation, reason="Changed request")
    assert PostgresEncounterStore(context).get(stored.bundle.encounter_id).bundle == stored.bundle
    latest = store.latest(stored.bundle.encounter_id, expected_source=h.source_snapshot(stored))
    assert latest["revisionNumber"] == 2 and latest["header"] == first["header"]


def test_pg_missing_audit_rolls_back_header_revision_and_receipt(setup):
    store, context, stored, _, _ = setup
    with pytest.raises(h.BillingHeaderError, match="audit_required"):
        create(setup, audit_completion=None)
    assert store.latest(stored.bundle.encounter_id, expected_source=h.source_snapshot(stored)) is None
    first, _ = create(setup)
    with pytest.raises(PersistenceUnavailable):
        save(setup, first, reserve=False)
    assert store.latest(stored.bundle.encounter_id, expected_source=h.source_snapshot(stored)) == first
    with context.connection() as connection:
        assert connection.execute("SELECT count(*) AS n FROM billing_header_operations WHERE practice_id=%s", (context.practice_id,)).fetchone()["n"] == 1


def test_pg_revoked_actor_and_changed_source_are_rejected(setup):
    store, context, stored, actor, _ = setup
    first, _ = create(setup)
    with context.connection() as connection:
        connection.execute("UPDATE practice_memberships SET active=false WHERE practice_id=%s AND user_id=%s::uuid", (context.practice_id, actor.user_id))
    with pytest.raises(h.BillingHeaderError, match="not_authorized"):
        save(setup, first)
    with context.connection() as connection:
        connection.execute("UPDATE practice_memberships SET active=true WHERE practice_id=%s AND user_id=%s::uuid", (context.practice_id, actor.user_id))
    PostgresEncounterStore(context).upsert(replace(stored.bundle, transcript="New version of synthetic context."))
    with pytest.raises(h.BillingHeaderError, match="source_changed"):
        store.latest(stored.bundle.encounter_id, expected_source=h.source_snapshot(stored))
    with pytest.raises(h.BillingHeaderError, match="source_changed"):
        save(setup, first)


def test_pg_concurrent_expected_revision_is_compare_and_swap(setup):
    first, _ = create(setup)
    def attempt(_):
        try:
            return save(setup, first)[0]["revisionNumber"]
        except h.BillingHeaderError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, range(2)))
    assert sorted(results, key=str) == [1, "revision_conflict"]


def test_pg_database_forbids_rewrites_and_keeps_forced_rls(setup):
    _, context, _, actor, _ = setup
    first, _ = create(setup)
    save(setup, first)
    for table in ("billing_headers", "billing_header_revisions", "billing_header_operations"):
        with pytest.raises(PersistenceUnavailable):
            with context.connection() as connection:
                connection.execute(f"DELETE FROM {table} WHERE practice_id=%s", (context.practice_id,))
        with context.connection() as connection:
            row = connection.execute("SELECT relrowsecurity,relforcerowsecurity FROM pg_class WHERE oid=%s::regclass", (table,)).fetchone()
            assert row["relrowsecurity"] and row["relforcerowsecurity"]
    with context.connection() as connection:
        allowed = connection.execute("SELECT cosmaneura_billing_header_editor_allowed(%s::uuid,%s::uuid,'Coder') AS ok", (actor.tenant_id, actor.user_id)).fetchone()
        assert allowed["ok"] is False


def test_pg_other_practice_cannot_discover_header_or_use_actor(setup):
    store, context, stored, actor, _ = setup
    create(setup)
    other = PostgresBillingHeaderStore(PostgresContext(DSN, practice_id="absent-test-" + uuid4().hex))
    with pytest.raises(h.BillingHeaderError, match="header_not_found"):
        other.latest(stored.bundle.encounter_id, expected_source=h.source_snapshot(stored))
    with pytest.raises(h.BillingHeaderError, match="not_authorized"):
        other.generation_replay(stored.bundle.encounter_id, expected_source=h.source_snapshot(stored),
                                operation_id=str(uuid4()), actor=actor)


def test_pg_replayed_generation_keeps_current_head(setup):
    store, _, stored, actor, _ = setup
    operation = str(uuid4())
    assert store.generation_replay(stored.bundle.encounter_id, expected_source=h.source_snapshot(stored),
                                   operation_id=operation, actor=actor) is None
    first, _ = store.create(stored.bundle.encounter_id, expected_source=h.source_snapshot(stored),
                             generated=generated(stored), actor=actor, operation_id=operation,
                             audit_completion=completion(setup, "billing_header.compute"))
    current, _ = save(setup, first)
    replay = store.generation_replay(stored.bundle.encounter_id, expected_source=h.source_snapshot(stored),
                                     operation_id=operation, actor=actor,
                                     audit_completion=completion(setup, "billing_header.compute"))
    assert replay == current and replay["revisionNumber"] == 1


@pytest.mark.parametrize("target", ["baseline", "revision"])
def test_pg_constraint_rejects_incomplete_write_even_when_python_finish_is_bypassed(setup, target):
    """A future adapter bug cannot commit history without its operation/audit link."""
    store, _, stored, actor, _ = setup
    initial = create(setup)[0] if target == "revision" else None
    action = "billing_header.save" if initial else "billing_header.compute"
    event = completion(setup, action)
    with pytest.raises(PersistenceUnavailable):
        with store._unit(stored.bundle.encounter_id, actor, str(uuid4())) as unit:
            if initial:
                draft = h.create_draft(initial["header"])
                draft["note"] = "Synthetic missing-receipt regression"
                revision = h.make_revision(initial["header"], None, draft, actor, "Test", 0, NOW)
                unit.insert_revision(initial["header"]["headerId"], revision, actor)
                header_id = initial["header"]["headerId"]
            else:
                header = h.make_header(generated(stored), stored, actor, str(uuid4()), NOW)
                header_id = header["headerId"]
                unit.insert_header({"headerId": header_id, "header": header,
                                    "sourceDigest": h.digest(h.source_snapshot(stored))}, actor)
            unit.audit(event, actor, action, header_id)
            # Deliberately no receipt. SQL deferred constraint must reject.
    assert store.latest(stored.bundle.encounter_id, expected_source=h.source_snapshot(stored)) == initial


@pytest.mark.parametrize("prior_use", [False, True])
def test_pg_completion_cannot_be_reused_or_precommitted_for_new_operation(setup, prior_use):
    store, context, stored, actor, _ = setup
    event = completion(setup, "billing_header.compute")
    if prior_use:
        first, _ = create(setup, audit_completion=event)
    else:
        first, _ = create(setup)
        with context.connection() as connection:
            PostgresAccessAuditStore._insert_completion(connection, event)
    with pytest.raises(PersistenceUnavailable):
        with store._unit(stored.bundle.encounter_id, actor, str(uuid4())) as unit:
            unit.insert_receipt(str(uuid4()), "0" * 64, first["header"]["headerId"], 0, actor, event)
            # No new completion; unique linkage or same-transaction check fails.
    with context.connection() as connection:
        assert connection.execute("SELECT count(*) AS n FROM billing_header_operations WHERE practice_id=%s",
                                  (context.practice_id,)).fetchone()["n"] == 1


def test_pg_cannot_pre_authorize_a_future_revision_with_a_receipt(setup):
    store, _, stored, actor, _ = setup
    first, _ = create(setup)
    event = completion(setup, "billing_header.save")
    with pytest.raises(PersistenceUnavailable):
        with store._unit(stored.bundle.encounter_id, actor, str(uuid4())) as unit:
            unit.insert_receipt(str(uuid4()), "0" * 64, first["header"]["headerId"], 1, actor, event)
            unit.audit(event, actor, "billing_header.save", first["header"]["headerId"])


def test_pg_restricted_login_cannot_switch_practice_or_rewrite_history(setup):
    """Use an actual session user: SET ROLE would not exercise practice binding."""
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    _, context, stored, actor, coder = setup
    role = "header_runtime_" + uuid4().hex
    ident = sql.Identifier(role)
    with psycopg.connect(DSN) as connection:
        connection.execute(sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS").format(ident))
        connection.execute("INSERT INTO runtime_practice_bindings(role_name,practice_id) VALUES(%s,%s)", (role, context.practice_id))
        connection.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(ident))
        connection.execute(sql.SQL("GRANT SELECT,UPDATE ON encounters TO {}").format(ident))
        connection.execute(sql.SQL("GRANT INSERT ON access_audit_attempts,access_audit_completions TO {}").format(ident))
        connection.execute(sql.SQL("GRANT SELECT,INSERT ON billing_headers,billing_header_revisions,billing_header_operations,billing_header_signoffs TO {}").format(ident))
        connection.execute(sql.SQL("GRANT EXECUTE ON FUNCTION cosmaneura_practice_allowed(text),cosmaneura_billing_header_editor_allowed(uuid,uuid,text) TO {}").format(ident))
    limited_dsn = make_conninfo(**{**conninfo_to_dict(DSN), "user": role})
    limited_context = PostgresContext(limited_dsn, practice_id=context.practice_id)
    limited_store = PostgresBillingHeaderStore(limited_context, clock=lambda: NOW)
    limited_setup = (limited_store, limited_context, stored, actor, coder)
    try:
        limited_store.healthcheck()
        first, _ = create(limited_setup)
        second, _ = save(limited_setup, first, actor=coder)
        assert second["revisionNumber"] == 1
        for table in ("billing_headers", "billing_header_revisions", "billing_header_operations"):
            with pytest.raises(PersistenceUnavailable):
                with limited_context.connection() as connection:
                    connection.execute(f"DELETE FROM {table}")
        with limited_context.connection() as connection:
            connection.execute("SELECT set_config('cosmaneura.practice_id','different-practice',true)")
            assert connection.execute("SELECT count(*) AS n FROM billing_headers").fetchone()["n"] == 0
            assert connection.execute("SELECT cosmaneura_billing_header_editor_allowed(%s::uuid,%s::uuid,%s) AS allowed",
                                      (actor.tenant_id, actor.user_id, actor.role)).fetchone()["allowed"] is False
        with psycopg.connect(DSN) as connection:
            connection.execute(sql.SQL("REVOKE INSERT ON billing_headers FROM {}").format(ident))
        with pytest.raises(h.BillingHeaderError, match="store_invalid"):
            limited_store.healthcheck()  # SELECT alone is not a ready write store.
    finally:
        with psycopg.connect(DSN) as connection:
            connection.execute("DELETE FROM runtime_practice_bindings WHERE role_name=%s", (role,))
            connection.execute(sql.SQL("DROP OWNED BY {}").format(ident))
            connection.execute(sql.SQL("DROP ROLE {}").format(ident))
