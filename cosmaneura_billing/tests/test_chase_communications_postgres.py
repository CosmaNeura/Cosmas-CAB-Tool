"""Real PostgreSQL isolation/replay tests; disposable synthetic database only."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields, replace
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

psycopg = pytest.importorskip("psycopg")
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from cosmaneura_billing.chase.communications import CommunicationsError, Eligibility
from cosmaneura_billing.chase.communications.postgres import PostgresCommunicationsStore


DSN = os.environ.get("COSMANEURA_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not DSN, reason="disposable PostgreSQL is not configured")
TABLES = ["chase_communication_" + name for name in
          ("senders", "bindings", "operations", "events", "suppressions")]
PHONE, SENDER = "+12025550100", "+12025550199"
ALLOWED = Eligibility(**{field.name: True for field in fields(Eligibility)})


@pytest.fixture(scope="module", autouse=True)
def schema():
    if not DSN:
        return
    with psycopg.connect(DSN, autocommit=True) as db:
        if "test" not in db.info.dbname.lower():
            pytest.fail("communications tests require a disposable test database")
        root = Path(__file__).resolve().parents[1] / "migrations"
        for name, table in [("002_chase_persistence", "chase_items"),
                            ("003_pilot_persistence", "practices"),
                            ("004_chase_delivery_runtime", "chase_contact_snapshots"),
                            ("010_chase_communications", "chase_communication_events"),
                            ("012_chase_communications_dispatch", "chase_communication_provider_events"),
                            ("015_chase_communications_cancellation", "chase_communication_cancellations"),
                            ("016_chase_communications_unknown_resolution", "chase_communication_unknown_resolutions"),
                            ("017_chase_communications_event_review", "chase_communication_event_reviews"),
                            ("018_chase_unassigned_sms_review", "chase_communication_unassigned_sources")]:
            if name == "012_chase_communications_dispatch" and db.execute(
                "SELECT to_regprocedure('cosmaneura_record_accepted_delivery_attempt(uuid)')"
            ).fetchone()[0] is None:
                db.execute((root / "005_accepted_delivery_attempt.up.sql").read_text(), prepare=False)
            if db.execute("SELECT to_regclass(%s)", ("public." + table,)).fetchone()[0] is None:
                db.execute((root / (name + ".up.sql")).read_text(), prepare=False)
        if not db.execute("""SELECT 1 FROM information_schema.columns
                WHERE table_schema='public' AND table_name='chase_communication_provider_events'
                  AND column_name='error_code'""").fetchone():
            db.execute((root / "027_communications_delivery_errors.up.sql").read_text(), prepare=False)


@pytest.fixture
def lane():
    return new_lane()


def new_lane():
    suffix = uuid4().hex
    practice, tenant, actor = "comms-" + suffix, str(uuid4()), str(uuid4())
    role, account = "comms_" + suffix[:20], "AC" + suffix
    password = uuid4().hex  # Disposable role credential, never the owner password.
    with psycopg.connect(DSN) as db:
        db.execute("INSERT INTO practices(practice_id,entra_tenant_id) VALUES (%s,%s)", (practice, tenant))
        db.execute("INSERT INTO users(tenant_id,user_id) VALUES (%s,%s)", (tenant, actor))
        db.execute("""INSERT INTO practice_memberships(practice_id,tenant_id,user_id,roles)
            VALUES (%s,%s,%s,ARRAY['BillingStaff'])""", (practice, tenant, actor))
        db.execute(sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD {}").format(
            sql.Identifier(role), sql.Literal(password)))
        db.execute("INSERT INTO runtime_practice_bindings(role_name,practice_id) VALUES (%s,%s)", (role, practice))
        db.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(role)))
        db.execute(sql.SQL("GRANT SELECT,INSERT ON {} TO {}").format(
            sql.SQL(",").join(map(sql.Identifier, TABLES)), sql.Identifier(role)))
        db.execute(sql.SQL("GRANT USAGE,SELECT ON SEQUENCE chase_communication_events_sequence_no_seq TO {}").format(sql.Identifier(role)))
        db.execute(sql.SQL("GRANT SELECT ON chase_items,chase_contact_snapshots,chase_contact_routes TO {}").format(sql.Identifier(role)))
        db.execute(sql.SQL("GRANT EXECUTE ON FUNCTION cosmaneura_practice_allowed(text),cosmaneura_staff_assignee_allowed(uuid,uuid) TO {}").format(sql.Identifier(role)))
    result = SimpleNamespace(practice=practice, tenant=tenant, actor=actor, role=role,
        account=account, dsn=make_conninfo(DSN, user=role, password=password), case="case-a", snapshot=None)
    result.store = PostgresCommunicationsStore(result.dsn, practice_id=practice)
    result.snapshot = seed_case(result)
    return result


def seed_case(lane, case="case-a", phone=PHONE, consent="granted", observed="2020-01-01T00:00:00Z"):
    snapshot = str(uuid4())
    with psycopg.connect(DSN) as db:
        db.execute("""INSERT INTO chase_items(practice_id,chase_id,patient_ref,trigger,detected_at,state,bucket,direct_contract)
            VALUES (%s,%s,%s,'no_card_on_file','2020-01-01','needs_review','missing',%s) ON CONFLICT DO NOTHING""",
            (lane.practice, case, "SYNTHETIC-" + case, Jsonb({"is_direct_contract": False, "source": "test"})))
        db.execute("""INSERT INTO chase_contact_snapshots(practice_id,contact_snapshot_id,chase_id,source_system,
            source_version,observed_at,verified_at,verification_method)
            VALUES (%s,%s,%s,'synthetic-test','1',%s,%s,'authoritative_source')""",
            (lane.practice, snapshot, case, observed, observed))
        for channel in ("sms", "phone"):
            db.execute("""INSERT INTO chase_contact_routes(practice_id,contact_snapshot_id,channel,destination,
                consent_status,consent_source,consent_recorded_at) VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                (lane.practice, snapshot, channel, phone, consent,
                 "synthetic-consent" if consent in ("granted", "denied") else None,
                 observed if consent in ("granted", "denied") else None))
    return snapshot


def bind(lane, channel="sms", **changes):
    args = dict(case_id=lane.case, channel=channel, contact_snapshot_id=lane.snapshot,
                sender=SENDER, account_sid=lane.account, actor_tenant_id=lane.tenant, actor_id=lane.actor)
    args.update(changes)
    return lane.store.bind_case(**args)["bindingId"]


def prepare(lane, binding, **changes):
    args = dict(case_id=lane.case, binding_id=binding, operation_id=str(uuid4()), channel="sms",
                text="Synthetic reminder. Reply STOP to stop.", actor_tenant_id=lane.tenant,
                actor_id=lane.actor, eligibility=ALLOWED)
    args.update(changes)
    return lane.store.prepare(**args)


def receive(lane, text="Synthetic reply", **changes):
    args = dict(account_sid=lane.account, provider_sid="SM" + uuid4().hex,
                sender=PHONE, recipient=SENDER, text=text, event_id=str(uuid4()))
    args.update(changes)
    return lane.store.receive_sms(**args)


def fails(code, call):
    with pytest.raises(CommunicationsError) as exc:
        call()
    assert exc.value.code == code


def test_preparation_survives_restart_with_attribution_and_no_send_or_attempt(lane):
    assert lane.store.case_view(lane.case)["operations"] == []
    binding = bind(lane)
    op = prepare(lane, binding)
    restored = PostgresCommunicationsStore(lane.dsn, practice_id=lane.practice).case_view(lane.case)
    assert restored["operations"] == [op]
    assert op["status"] == "prepared" and op["providerSid"] is None
    assert op["actorId"] == lane.actor and op["actorTenantId"] == lane.tenant
    assert restored["mode"] == "prepared" and len(restored["events"]) == 1
    assert not hasattr(lane.store, "send") and not hasattr(lane.store, "dispatch")
    with psycopg.connect(DSN) as db:
        assert db.execute("SELECT count(*) FROM chase_attempts WHERE practice_id=%s", (lane.practice,)).fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM chase_delivery_outbox WHERE practice_id=%s", (lane.practice,)).fetchone()[0] == 0


def test_concurrent_replicas_reserve_exactly_once_and_reject_changed_text(lane):
    binding, operation = bind(lane), str(uuid4())
    with ThreadPoolExecutor(max_workers=6) as pool:
        rows = list(pool.map(lambda _: prepare(lane, binding, operation_id=operation), range(6)))
    assert all(row == rows[0] for row in rows)
    assert len(lane.store.case_view(lane.case)["events"]) == 1
    fails("communications_idempotency_conflict", lambda: prepare(lane, binding, operation_id=operation, text="Changed"))


@pytest.mark.parametrize("gate", [field.name for field in fields(Eligibility)])
def test_each_server_gate_fails_closed(lane, gate):
    fails("communications_policy_blocked", lambda: prepare(lane, bind(lane), eligibility=replace(ALLOWED, **{gate: False})))
    assert lane.store.case_view(lane.case)["operations"] == []


def test_request_dict_and_truthy_flag_are_not_server_authority(lane):
    binding = bind(lane)
    fails("communications_policy_blocked", lambda: prepare(lane, binding, eligibility=vars(ALLOWED)))
    fails("communications_policy_blocked", lambda: prepare(lane, binding, eligibility=replace(ALLOWED, consent_valid=1)))


@pytest.mark.parametrize("column,value", [("state", "verified_closed"), ("direct_contract", {}),
    ("direct_contract", {"is_direct_contract": True}), ("direct_contract", {"is_direct_contract": None})])
def test_current_case_gate_is_not_replaced_by_eligibility_booleans(lane, column, value):
    binding = bind(lane)
    with psycopg.connect(DSN) as db:
        db.execute(sql.SQL("UPDATE chase_items SET {}=%s WHERE practice_id=%s").format(sql.Identifier(column)),
                   (Jsonb(value) if column == "direct_contract" else value, lane.practice))
    fails("communications_policy_blocked", lambda: prepare(lane, binding))


def test_membership_revocation_blocks_mutation(lane):
    binding = bind(lane)
    with psycopg.connect(DSN) as db:
        db.execute("UPDATE practice_memberships SET active=false WHERE practice_id=%s", (lane.practice,))
    fails("communications_staff_required", lambda: prepare(lane, binding))


def test_snapshot_provenance_stale_contact_and_unknown_consent(lane):
    binding = bind(lane)
    fresh = seed_case(lane, observed="2020-01-02T00:00:00Z", consent="unknown")
    fails("communications_contact_stale", lambda: prepare(lane, binding))
    new_binding = bind(lane, contact_snapshot_id=fresh)
    fails("communications_consent_unavailable", lambda: prepare(lane, new_binding))


def test_wrong_actor_and_cross_practice_setting_cannot_acquire_authority(lane):
    binding = bind(lane)
    prepare(lane, binding)
    fails("communications_staff_required", lambda: prepare(lane, binding, actor_id=str(uuid4())))
    with psycopg.connect(lane.dsn) as db:
        assert db.execute("SELECT count(*) FROM chase_communication_operations").fetchone()[0] == 0
        db.execute("SELECT set_config('cosmaneura.practice_id','different-practice',true)")
        assert db.execute("SELECT count(*) FROM chase_communication_operations").fetchone()[0] == 0
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db.execute("INSERT INTO chase_communication_senders VALUES ('different-practice',%s,%s,now())", (lane.account, SENDER))
    other = PostgresCommunicationsStore(lane.dsn, practice_id="different-practice")
    fails("communications_case_not_found", lambda: other.case_view(lane.case))


def test_two_real_runtime_practices_can_reuse_case_and_operation_ids_without_leaks(lane):
    other, operation = new_lane(), str(uuid4())
    first = prepare(lane, bind(lane), operation_id=operation)
    second = prepare(other, bind(other), operation_id=operation, text="Other synthetic practice")
    assert lane.store.case_view(lane.case)["operations"] == [first]
    assert other.store.case_view(other.case)["operations"] == [second]
    with psycopg.connect(lane.dsn) as db:
        db.execute("SELECT set_config('cosmaneura.practice_id',%s,true)", (other.practice,))
        assert db.execute("SELECT count(*) FROM chase_communication_operations").fetchone()[0] == 0


def test_account_and_sender_cannot_be_bound_by_a_second_practice(lane):
    bind(lane)
    other = new_lane()
    fails("communications_sender_unavailable", lambda: bind(other, account_sid=lane.account))


def test_stop_and_start_are_durable_with_one_inbound_per_provider_sid(lane):
    binding = bind(lane)
    sid, event_id = "SM" + uuid4().hex, str(uuid4())
    first = receive(lane, "STOP", provider_sid=sid, event_id=event_id)
    assert receive(lane, "STOP", provider_sid=sid, event_id=event_id) == first
    assert receive(lane, "STOP", provider_sid=sid) == first
    receive(lane, "START")
    view = PostgresCommunicationsStore(lane.dsn, practice_id=lane.practice).case_view(lane.case)
    assert view["suppression"] == {"sms": True, "automatedVoice": False}
    assert len(view["events"]) == 2
    fails("communications_contact_suppressed", lambda: prepare(lane, binding))
    fails("communications_provider_message_conflict", lambda: receive(lane, "Different", provider_sid=sid))
    fails("communications_event_conflict", lambda: receive(lane, event_id=event_id))


def test_ambiguous_inbound_held_and_suppresses_all_matching_cases(lane):
    first = bind(lane)
    snapshot = seed_case(lane, case="case-b")
    second = bind(lane, case_id="case-b", contact_snapshot_id=snapshot)
    event = receive(lane, "STOP")
    assert event["held"] is True
    for case, binding in [(lane.case, first), ("case-b", second)]:
        view = lane.store.case_view(case)
        assert view["heldInboundCount"] == 1 and view["events"] == []
        assert view["suppression"]["sms"] is True
        fails("communications_contact_suppressed", lambda: prepare(lane, binding, case_id=case))


def test_reply_to_superseded_number_is_held_without_suppressing_new_number(lane):
    bind(lane)
    snapshot = seed_case(lane, phone="+12025550101", observed="2020-01-02T00:00:00Z")
    current = bind(lane, contact_snapshot_id=snapshot)
    assert receive(lane, "STOP")["held"] is True
    view = lane.store.case_view(lane.case)
    assert view["events"] == [] and view["heldInboundCount"] == 1
    assert view["suppression"]["sms"] is False
    assert prepare(lane, current)["status"] == "prepared"


def test_inbound_route_mismatch_is_rejected(lane):
    bind(lane)
    fails("communications_callback_binding_mismatch", lambda: receive(lane, sender="+12025550101"))
    fails("communications_callback_binding_mismatch", lambda: receive(lane, account_sid="AC" + "f" * 32))
    assert lane.store.case_view(lane.case)["events"] == []


@pytest.mark.parametrize("occurred_at", [
    "2026-09-29T16:00:00+00:99", "2026-09-29T16:00:00-00:00",
    "2026-09-29X16:00:00Z", "2026-09-29 16:00:00Z", "2026-09-29T16:00:00",
    "2026-09-29T16:00:00+24:00", "2026-09-29T16:00:00-04:60",
    "2026-02-30T16:00:00Z", "2026-09-29T25:00:00Z", "2026-09-29T16:00:60Z",
    "2026-09-29T16:00:00.1234567Z", False,
])
def test_inbound_invalid_event_time_rejects_before_event_or_stop_is_recorded(lane, occurred_at):
    bind(lane)
    fails("invalid_event_time", lambda: receive(lane, "STOP", occurred_at=occurred_at))
    view = lane.store.case_view(lane.case)
    assert view["events"] == [] and view["suppression"]["sms"] is False


@pytest.mark.parametrize("occurred_at", ["2026-09-29T16:00:00Z", "2026-09-29T12:00:00-04:00"])
def test_inbound_exact_instants_normalize_to_utc_and_replay(lane, occurred_at):
    bind(lane)
    sid, event = "SM" + uuid4().hex, str(uuid4())
    first = receive(lane, occurred_at=occurred_at, provider_sid=sid, event_id=event)
    assert first["occurredAt"] == "2026-09-29T16:00:00.000000+00:00"
    assert receive(lane, occurred_at="2026-09-29T16:00:00+00:00", provider_sid=sid, event_id=event) == first


def test_missing_provider_time_uses_server_time_and_replays_without_fabricated_time(lane):
    bind(lane)
    sid, event = "SM" + uuid4().hex, str(uuid4())
    first = receive(lane, occurred_at=None, provider_sid=sid, event_id=event)
    assert first["occurredAt"].endswith("+00:00")
    assert receive(lane, occurred_at=None, provider_sid=sid, event_id=event) == first


def test_staff_voice_opt_out_is_distinct_from_sms_and_manual_phone(lane):
    lane.store = PostgresCommunicationsStore(lane.dsn, practice_id=lane.practice,
                                            enabled_channels=("sms", "automated_voice"))
    sms, voice = bind(lane), bind(lane, "automated_voice")
    event = lane.store.suppress(case_id=lane.case, binding_id=voice, channel="automated_voice",
        event_id=str(uuid4()), actor_tenant_id=lane.tenant, actor_id=lane.actor)
    assert event["disposition"] == "stop_calls" and event["actorId"] == lane.actor
    fails("communications_contact_suppressed", lambda: prepare(lane, voice, channel="automated_voice"))
    assert prepare(lane, sms)["status"] == "prepared"
    for method in (lane.store.receive_status, lane.store.receive_dtmf):
        fails("communications_dispatch_not_implemented", lambda: method(provider_sid="CA" + uuid4().hex))


def test_concurrent_stop_and_preparation_never_lose_suppression(lane):
    binding = bind(lane)
    def reserve():
        try:
            return prepare(lane, binding)
        except CommunicationsError as exc:
            assert exc.code == "communications_contact_suppressed"
    with ThreadPoolExecutor(max_workers=2) as pool:
        prepared, stop = list(pool.map(lambda f: f(), [reserve, lambda: receive(lane, "STOP")]))
    assert stop["disposition"] == "sms_opt_out"
    assert prepared is None or prepared["status"] == "prepared"
    fails("communications_contact_suppressed", lambda: prepare(lane, binding))
    assert lane.store.case_view(lane.case)["suppression"]["sms"] is True


def test_evidence_is_append_only_even_for_owner_and_down_refuses_data(lane):
    binding = bind(lane)
    prepare(lane, binding)
    receive(lane, "STOP")
    for table in TABLES:
        with psycopg.connect(DSN) as db:
            with pytest.raises(psycopg.errors.CheckViolation):
                db.execute(sql.SQL("DELETE FROM {} WHERE practice_id=%s").format(sql.Identifier(table)), (lane.practice,))
        with psycopg.connect(DSN) as db:
            with pytest.raises(psycopg.errors.CheckViolation):
                db.execute(sql.SQL("TRUNCATE {} CASCADE").format(sql.Identifier(table)))
        with psycopg.connect(DSN) as db:
            with pytest.raises(psycopg.errors.CheckViolation):
                db.execute(sql.SQL("UPDATE {} SET practice_id=practice_id WHERE practice_id=%s").format(sql.Identifier(table)), (lane.practice,))
    down = Path(__file__).resolve().parents[1] / "migrations/010_chase_communications.down.sql"
    with psycopg.connect(DSN, autocommit=True) as db:
        with pytest.raises(psycopg.errors.RaiseException, match="evidence exists"):
            db.execute(down.read_text(), prepare=False)


def test_database_rejects_fabricated_contact_and_suppression_evidence(lane):
    binding = bind(lane)
    event = receive(lane)
    with psycopg.connect(DSN) as db:
        with pytest.raises(psycopg.errors.CheckViolation):
            db.execute("""INSERT INTO chase_communication_suppressions
                (practice_id,account_sid,sender,phone,channel,event_id) VALUES (%s,%s,%s,%s,'sms',%s)""",
                (lane.practice, lane.account, SENDER, PHONE, event["eventId"]))
    with psycopg.connect(DSN) as db:
        with pytest.raises(psycopg.errors.CheckViolation):
            db.execute("""INSERT INTO chase_communication_bindings
                (practice_id,case_id,channel,contact_snapshot_id,route_channel,account_sid,sender,phone,
                 patient_ref,actor_tenant_id,actor_id)
                SELECT practice_id,case_id,channel,contact_snapshot_id,route_channel,account_sid,sender,
                 '+12025550101',patient_ref,actor_tenant_id,actor_id
                FROM chase_communication_bindings WHERE practice_id=%s AND binding_id=%s""", (lane.practice, binding))


def test_store_failure_and_repr_do_not_echo_connection_or_contact_data(lane):
    bad = PostgresCommunicationsStore("host=/does-not-exist dbname=test password=SYNTHETIC_SECRET", practice_id=lane.practice)
    assert "SYNTHETIC_SECRET" not in repr(bad)
    fails("communications_store_unavailable", lambda: bad.case_view("case-a"))
