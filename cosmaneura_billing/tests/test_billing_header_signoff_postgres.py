"""Real PG13 signoff rung, with feedback/audit and current-head serialization."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from cosmaneura_billing import billing_header as h
from cosmaneura_billing.persistence import PersistenceUnavailable, PostgresContext, PostgresFeedbackStore, PostgresEncounterStore
from cosmaneura_billing.billing_header_store import PostgresBillingHeaderStore
from test_billing_header_postgres import schema, setup, create, save, completion, DSN
from test_billing_header_signoff import options, drift_record

pytestmark = pytest.mark.skipif(not DSN, reason="explicit disposable header PostgreSQL is absent")


def sign(setup, view, **patch):
    store, context, stored, _, coder = setup
    request = options(stored, coder, view, PostgresFeedbackStore(context))
    request["audit_completion"] = completion(setup, "billing_header.signoff", coder)
    request.update(patch)
    return store.signoff(stored.bundle.encounter_id, **request)


def counts(context):
    with context.connection() as connection:
        return tuple(connection.execute(f"SELECT count(*) AS n FROM {table} WHERE practice_id=%s",
                                         (context.practice_id,)).fetchone()["n"]
                     for table in ("coder_feedback", "billing_header_signoffs"))


@pytest.mark.parametrize("retain", [False, True])
def test_pg_drift_reconciliation_feedback_and_audit_are_atomic(setup, retain):
    _, context, stored, _, coder = setup
    baseline, _ = create(setup)
    view, _ = save(setup, baseline)
    request = options(stored, coder, view, PostgresFeedbackStore(context),
                      drift_record(stored, coder, view, retain=retain))
    with pytest.raises(h.BillingHeaderError, match="invalid_signoff"):
        sign(setup, view, **request)
    assert counts(context) == (0, 0)
    request["baseline_decisions"] = [{"id": "code-1", "decision": "retain" if retain else "reject",
                                      "reason": "Earlier evidence explicitly reviewed"}]
    with pytest.raises((h.BillingHeaderError, PersistenceUnavailable)):
        sign(setup, view, **request, audit_completion=None)
    assert counts(context) == (0, 0)
    event = completion(setup, "billing_header.signoff", coder)
    reviewed, created = sign(setup, view, **request, audit_completion=event)
    assert created and counts(context) == (1, 1)
    assert reviewed["header"] == baseline["header"] and reviewed["revision"] == view["revision"]
    assert reviewed["signoff"]["baselineDecisions"] == request["baseline_decisions"]
    assert sign(setup, view, **request)[1] is False
    with context.connection() as connection:
        audit = connection.execute("SELECT status FROM access_audit_completions WHERE practice_id=%s AND event_id=%s::uuid AND status=200",
                                   (context.practice_id, event.event_id)).fetchone()
        assert audit is not None
    assert PostgresEncounterStore(context).get(stored.bundle.encounter_id).bundle == stored.bundle


def test_pg_signoff_receipt_feedback_and_edit_invalidation(setup):
    store, context, stored, _, coder = setup
    view, _ = create(setup)
    request = options(stored, coder, view, PostgresFeedbackStore(context))
    reviewed, created = sign(setup, view, **request)
    assert created and counts(context) == (1, 1)
    assert reviewed["signoff"]["headerRevision"] == 0
    replay, created = sign(setup, view, **request)
    assert replay == reviewed and not created and counts(context) == (1, 1)
    changed, _ = save(setup, view)
    assert changed["signoff"] is None and changed["previousSignoff"]["reviewId"] == request["feedback"].review_id
    with pytest.raises(h.BillingHeaderError, match="revision_conflict"):
        sign(setup, view, **request)
    assert PostgresEncounterStore(context).get(stored.bundle.encounter_id).bundle == stored.bundle
    again, _ = sign(setup, changed)
    assert again["signoff"]["headerRevision"] == 1 and counts(context) == (2, 2)


@pytest.mark.parametrize("failure", ["missing_completion", "missing_attempt", "wrong_action", "old_feedback"])
def test_pg_signoff_failures_do_not_partially_persist_feedback(setup, failure):
    _, context, _, _, coder = setup
    view, _ = create(setup)
    patch = {}
    if failure == "missing_completion": patch["audit_completion"] = None
    elif failure == "missing_attempt": patch["audit_completion"] = completion(setup, "billing_header.signoff", coder, reserve=False)
    elif failure == "wrong_action": patch["audit_completion"] = completion(setup, "billing_header.save", coder)
    else:
        request = options(setup[2], coder, view, PostgresFeedbackStore(context))
        request["feedback_store"].append(request["feedback"])
        patch = request
    with pytest.raises((h.BillingHeaderError, PersistenceUnavailable)):
        sign(setup, view, **patch)
    assert counts(context) == ((1, 0) if failure == "old_feedback" else (0, 0))


def test_pg_concurrent_reviews_have_one_winner_and_source_edit_invalidates(setup):
    _, context, _, _, _ = setup
    view, _ = create(setup)
    def attempt(_):
        try:
            sign(setup, view)
            return "recorded"
        except h.BillingHeaderError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(attempt, range(2))) == ["recorded", "review_conflict"]
    assert counts(context) == (1, 1)


def test_pg_concurrent_edit_never_leaves_a_current_receipt_for_an_old_header(setup):
    store, context, stored, _, _ = setup
    view, _ = create(setup)
    def sign_current():
        try:
            return sign(setup, view)[0]["signoff"]["status"]
        except h.BillingHeaderError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        signed = pool.submit(sign_current)
        edited = pool.submit(save, setup, view)
        assert signed.result() in {"recorded", "revision_conflict"}
        assert edited.result()[0]["revisionNumber"] == 1
    current = store.latest(stored.bundle.encounter_id, expected_source=h.source_snapshot(stored))
    assert current["signoff"] is None
    assert counts(context) in {(0, 0), (1, 1)}


def test_pg_source_change_and_revoked_coder_reject_signoff(setup):
    _, context, stored, _, coder = setup
    view, _ = create(setup)
    with context.connection() as connection:
        connection.execute("UPDATE practice_memberships SET active=false WHERE practice_id=%s AND user_id=%s::uuid", (context.practice_id, coder.user_id))
    with pytest.raises(h.BillingHeaderError, match="not_authorized"):
        sign(setup, view)
    with context.connection() as connection:
        connection.execute("UPDATE practice_memberships SET active=true WHERE practice_id=%s AND user_id=%s::uuid", (context.practice_id, coder.user_id))
    PostgresEncounterStore(context).upsert(replace(stored.bundle, signed_note="Changed synthetic source."))
    with pytest.raises(h.BillingHeaderError, match="source_changed"):
        sign(setup, view)
    assert counts(context) == (0, 0)


def test_pg_constraint_rejects_old_feedback_even_if_store_check_is_bypassed(setup):
    store, context, stored, _, coder = setup
    view, _ = create(setup)
    request = options(stored, coder, view, PostgresFeedbackStore(context))
    record = request["feedback"]
    request["feedback_store"].append(record)
    event = completion(setup, "billing_header.signoff", coder)
    receipt = {"id": str(uuid4()), "reviewId": record.review_id, "headerId": view["header"]["headerId"],
        "headerRevision": 0, "expectedSource": h.source_snapshot(stored), "actor": coder.to_api(),
        "createdAt": view["header"]["generatedAt"], "status": "recorded",
        "recommendationDigest": request["verified_context"]["recommendationDigest"],
        "humanProposalDecisions": [], "codeDecisions": [], "reviewerNote": ""}
    with pytest.raises(PersistenceUnavailable):
        with store._unit(stored.bundle.encounter_id, coder, record.review_id) as unit:
            unit.insert_signoff(receipt, "0" * 64, coder, record, event)
            unit.audit(event, coder, "billing_header.signoff", view["header"]["headerId"])
    assert counts(context) == (1, 0)


def test_pg_signed_history_cannot_be_mutated_or_dropped(setup):
    _, context, _, _, _ = setup
    view, _ = create(setup)
    sign(setup, view)
    for statement in ("DELETE FROM billing_header_signoffs", "TRUNCATE billing_header_signoffs",
                      "UPDATE billing_header_signoffs SET receipt='{}'::jsonb"):
        with pytest.raises(PersistenceUnavailable):
            with context.connection() as connection:
                connection.execute(statement)
    with pytest.raises(PersistenceUnavailable):
        with context.connection() as connection:
            connection.execute((Path(__file__).resolve().parents[1] / "migrations/013_billing_header_signoffs.down.sql").read_text(), prepare=False)


def test_pg_restricted_login_signoff_and_practice_isolation(setup):
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo
    _, owner_context, stored, provider, coder = setup
    view, _ = create(setup)
    role = "signoff_" + uuid4().hex
    ident = sql.Identifier(role)
    with psycopg.connect(DSN) as connection:
        connection.execute(sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS").format(ident))
        connection.execute("INSERT INTO runtime_practice_bindings(role_name,practice_id) VALUES(%s,%s)", (role, owner_context.practice_id))
        connection.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(ident))
        connection.execute(sql.SQL("GRANT SELECT,UPDATE ON encounters TO {}").format(ident))
        connection.execute(sql.SQL("GRANT SELECT,INSERT ON billing_headers,billing_header_revisions,billing_header_operations,billing_header_signoffs,coder_feedback,coder_feedback_decisions TO {}").format(ident))
        connection.execute(sql.SQL("GRANT INSERT ON access_audit_attempts,access_audit_completions TO {}").format(ident))
        connection.execute(sql.SQL("GRANT USAGE,SELECT ON SEQUENCE coder_feedback_feedback_id_seq TO {}").format(ident))
        connection.execute(sql.SQL("GRANT EXECUTE ON FUNCTION cosmaneura_practice_allowed(text),cosmaneura_billing_header_editor_allowed(uuid,uuid,text),cosmaneura_feedback_reviewer_allowed(uuid,uuid) TO {}").format(ident))
    context = PostgresContext(make_conninfo(DSN, user=role), practice_id=owner_context.practice_id)
    store = PostgresBillingHeaderStore(context)
    try:
        reviewed, created = sign((store, context, stored, provider, coder), view)
        assert created and reviewed["signoff"]["actor"]["id"] == coder.user_id
        with context.connection() as connection:
            assert connection.execute("SELECT count(*) AS n FROM billing_header_signoffs").fetchone()["n"] == 1
            connection.execute("SELECT set_config('cosmaneura.practice_id','wrong-practice',true)")
            assert connection.execute("SELECT count(*) AS n FROM billing_header_signoffs").fetchone()["n"] == 0
        with pytest.raises(PersistenceUnavailable):
            with context.connection() as connection:
                connection.execute("DELETE FROM billing_header_signoffs")
    finally:
        with psycopg.connect(DSN) as connection:
            connection.execute("DELETE FROM runtime_practice_bindings WHERE role_name=%s", (role,))
            connection.execute(sql.SQL("DROP OWNED BY {}").format(ident))
            connection.execute(sql.SQL("DROP ROLE {}").format(ident))
