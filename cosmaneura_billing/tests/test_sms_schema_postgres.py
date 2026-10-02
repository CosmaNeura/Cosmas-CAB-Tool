"""Canonical dormant SMS expansion on one explicitly owned local PostgreSQL DB."""
from dataclasses import replace
import hashlib
import os
import re
import shutil
from uuid import uuid4

import pytest

from cosmaneura_billing.migrations import apply_migrations, discover_migrations, migration_body, migration_directory
from cosmaneura_billing.seed_mcd_training import seed_mcd_training
from cosmaneura_billing.settings import Settings

RUNG_IDS = {"010", "012", "015", "016", "017", "018"}
TABLES = tuple("chase_communication_" + name for name in (
    "senders", "bindings", "operations", "events", "suppressions",
    "policy_decisions", "consent_decisions", "authorizations", "dispatch_starts",
    "dispatch_checks", "dispatch_receipts", "provider_events", "cancellations",
    "unknown_resolutions", "event_reviews",
))
VIEWS = ("chase_communication_review_sources", "chase_communication_unassigned_sources")
FUNCTIONS = (
    "cosmaneura_reject_communication_mutation",
    "cosmaneura_validate_communication_evidence",
    "cosmaneura_validate_communication_dispatch",
    "cosmaneura_guard_communication_cancellation",
    "cosmaneura_guard_unknown_communication",
    "cosmaneura_validate_communication_review",
)
# Final reviewed source 8d0caca; no rewritten or renumbered migration SQL.
CANONICAL = {
    "010_chase_communications.down.sql": "9721a28bb3612a537f828da45df8b7d875252ba0951a7918526b16bf30cfcd4a",
    "010_chase_communications.up.sql": "aab2965c0d4b4016ff4afdfe3bcaf8894e036a5c99a0859a627ff4847dc80b41",
    "012_chase_communications_dispatch.down.sql": "f66ebb4b80ff59dcef617ac5a6d3b0de3eb388ac95bd1f938bfe3e5fba779410",
    "012_chase_communications_dispatch.up.sql": "143ae751cc376404100fa2c13ae77422fa799fade1e45949995f32de3d34b1cb",
    "015_chase_communications_cancellation.down.sql": "e12590e20ad3bb2f17e5518a42f634a1348c835d22426b56ddd22d0bf8aaf683",
    "015_chase_communications_cancellation.up.sql": "8522ab95370c17306bdcec274c1b6ce390dbb787d076a78ac6a8a27cd284f0d2",
    "016_chase_communications_unknown_resolution.down.sql": "9e810598c7ab44fcc7614e825b93f3efd182f0ee641690f14efa05486f66b884",
    "016_chase_communications_unknown_resolution.up.sql": "7e5f880b408150acddcaf9b7a71df184a39baf6665e5eec5beaf5147051ff409",
    "017_chase_communications_event_review.down.sql": "649b9bfc4bd4f336a6ec4106127ca86adf5ee9446f98d2fd5f6177073e5eb40e",
    "017_chase_communications_event_review.up.sql": "9b99a89c89eb58165b9a7092cda644d876b69731c0d3d437a7d1182ce6b7e6e9",
    "018_chase_unassigned_sms_review.down.sql": "714b3031e00a85b8bc4bc6cd16e6e99eaf7427d10a5065f08c70c4934e59b580",
    "018_chase_unassigned_sms_review.up.sql": "5086e8fab9794f4b1d731e3d657a5e36be182183782d84b3219f92261ad3ac44"
}


def test_communications_migrations_are_exact_canonical_pairs():
    directory = migration_directory(Settings())
    assert {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in directory.glob("*.sql") if p.name[:3] in RUNG_IDS} == CANONICAL


@pytest.fixture(scope="module")
def schema(tmp_path_factory):
    psycopg = pytest.importorskip("psycopg")
    from psycopg.conninfo import conninfo_to_dict
    dsn = os.environ.get("SMS_SCHEMA_TEST_DSN")
    if not dsn:
        pytest.skip("requires an owned disposable loopback SMS schema database")
    parts = conninfo_to_dict(dsn)
    assert parts.get("host") == "127.0.0.1"
    assert parts.get("user") == "cab_sms_owner"
    assert re.fullmatch(r"cab_sms_schema_[0-9a-f]{8}", parts.get("dbname", ""))
    assert parts.get("port", "").isdigit()
    with psycopg.connect(dsn) as conn:
        assert conn.execute("SELECT to_regclass('public.cosmaneura_schema_migrations')").fetchone()[0] is None
    tag = uuid4().hex[:8]
    config = Settings(
        database_url=dsn, practice_id="sms-schema-" + tag,
        entra_tenant_id=str(uuid4()), bootstrap_admin_oid=str(uuid4()),
        database_runtime_role="sms_app_" + tag, database_runtime_password=uuid4().hex,
        chase_worker_database_role="sms_worker_" + tag, chase_worker_database_password=uuid4().hex,
        athena_import_database_role="sms_import_" + tag, athena_import_database_password=uuid4().hex,
    )
    paths = discover_migrations(migration_directory(config))
    baseline = tmp_path_factory.mktemp("sms-baseline")
    old_paths = [p for p in paths if p.name[:3] not in RUNG_IDS]
    for path in old_paths:
        shutil.copyfile(path, baseline / path.name)
    assert apply_migrations(replace(config, migrations_dir=str(baseline))) == [p.name for p in old_paths]
    assert seed_mcd_training(config)["inserted_count"] == 12
    with psycopg.connect(dsn) as conn:
        before = conn.execute("SELECT row_to_json(e)::text FROM encounters e ORDER BY external_id").fetchall()
    assert len(before) == 12
    assert apply_migrations(config) == [p.name for p in paths if p.name[:3] in RUNG_IDS]
    assert apply_migrations(config) == []
    with psycopg.connect(dsn) as conn:
        assert conn.execute("SELECT row_to_json(e)::text FROM encounters e ORDER BY external_id").fetchall() == before
        ledger = conn.execute("SELECT name,checksum_sha256 FROM cosmaneura_schema_migrations ORDER BY name").fetchall()
    assert ledger == [(p.name, hashlib.sha256(p.read_bytes()).hexdigest()) for p in paths]
    assert seed_mcd_training(config)["replayed_count"] == 12
    return config, paths


def test_upgrade_retains_twelve_sources_and_exact_sparse_ledger(schema):
    _, paths = schema
    assert [p.name[:3] for p in paths] == [f"{n:03}" for n in range(1, 14)] + ["015", "016", "017", "018"]


def test_tables_force_rls_and_always_guards_views_remain_invoker(schema):
    import psycopg
    config, _ = schema
    with psycopg.connect(config.database_url) as conn:
        rows = conn.execute("SELECT relname,relrowsecurity,relforcerowsecurity FROM pg_class WHERE relname=ANY(%s)", (list(TABLES),)).fetchall()
        assert sorted(rows) == sorted((name, True, True) for name in TABLES)
        for table in TABLES:
            triggers = dict(conn.execute("SELECT tgname,tgenabled FROM pg_trigger WHERE tgrelid=%s::regclass AND NOT tgisinternal", (table,)).fetchall())
            assert triggers["immutable_rows"] == triggers["immutable_table"] == "A"
            assert set(triggers.values()) == {"A"}
            policy = conn.execute("SELECT qual,with_check FROM pg_policies WHERE tablename=%s", (table,)).fetchone()
            assert policy == ("cosmaneura_practice_allowed(practice_id)",) * 2
        for view in VIEWS:
            assert "security_invoker=true" in conn.execute("SELECT reloptions FROM pg_class WHERE oid=%s::regclass", (view,)).fetchone()[0]
        assert conn.execute("SELECT tgname FROM pg_trigger WHERE tgrelid='chase_communication_dispatch_starts'::regclass AND tgname IN ('start_unless_canceled','unknown_hold') ORDER BY tgname").fetchall() == [("start_unless_canceled",), ("unknown_hold",)]


@pytest.mark.parametrize("role_field", ["database_runtime_role", "chase_worker_database_role", "athena_import_database_role"])
def test_actual_provisioned_roles_have_no_communications_authority(schema, role_field):
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo
    config, _ = schema
    role = getattr(config, role_field)
    with psycopg.connect(make_conninfo(config.database_url, user=role), autocommit=True) as conn:
        assert conn.execute("SELECT session_user").fetchone()[0] == role
        conn.execute("SELECT set_config('cosmaneura.practice_id',%s,false)", (config.practice_id,))
        for table in TABLES + VIEWS:
            assert not conn.execute("SELECT has_table_privilege(current_user,%s,'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')", (table,)).fetchone()[0]
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(sql.SQL("SELECT * FROM {} LIMIT 1").format(sql.Identifier(table)))
        for function in FUNCTIONS:
            assert not conn.execute("SELECT has_function_privilege(current_user,%s,'EXECUTE')", (function + "()",)).fetchone()[0]
        assert conn.execute("SELECT rolsuper,rolcreatedb,rolcreaterole,rolbypassrls FROM pg_roles WHERE rolname=current_user").fetchone() == (False,) * 4
        if role_field == "database_runtime_role":
            for table in ("billing_headers", "billing_header_signoffs"):
                for privilege in ("SELECT", "INSERT"):
                    assert conn.execute("SELECT has_table_privilege(current_user,%s,%s)", (table, privilege)).fetchone()[0]


def insert_inbound(conn, practice, event="synthetic-stop"):
    conn.execute("""INSERT INTO chase_communication_events
        (practice_id,event_id,account_sid,provider_sid,sender,recipient,channel,kind,
         direction,text,status,disposition,occurred_at,body_hash)
        VALUES (%s,%s,%s,%s,'+12025550101','+12025550102','sms','inbound_sms',
                'inbound','STOP','received','sms_opt_out',now(),%s)""",
        (practice, event, "AC" + "1"*32, "SM" + uuid4().hex, "a"*64))


def test_review_is_append_only_and_recovery_preserves_evidence(schema):
    import psycopg
    config, _ = schema
    with psycopg.connect(config.database_url, autocommit=True) as conn:
        with conn.transaction(force_rollback=True):
            conn.execute("SELECT set_config('cosmaneura.practice_id',%s,true)", (config.practice_id,))
            insert_inbound(conn, config.practice_id)
            source = conn.execute("SELECT source_ref,source_hash,review_state FROM chase_communication_unassigned_sources WHERE practice_id=%s", (config.practice_id,)).fetchone()
            assert source[2] == "pending"
            conn.execute("""INSERT INTO chase_communication_event_reviews
                (practice_id,action_id,source_ref,source_hash,decision,actor_tenant_id,actor_id,body_hash)
                VALUES (%s,%s,%s,%s,'leave_unassigned',%s,%s,%s)""",
                (config.practice_id, uuid4(), source[0], source[1], config.entra_tenant_id, config.bootstrap_admin_oid, "b"*64))
            assert conn.execute("SELECT review_state FROM chase_communication_unassigned_sources WHERE source_ref=%s", (source[0],)).fetchone()[0] == "left_unassigned"
            for statement in (
                "UPDATE chase_communication_events SET text='START'",
                "DELETE FROM chase_communication_event_reviews",
                "TRUNCATE chase_communication_events CASCADE",
            ):
                with pytest.raises(psycopg.errors.CheckViolation, match="append-only"):
                    with conn.transaction():
                        conn.execute(statement)
            for name in ("010_chase_communications.down.sql", "018_chase_unassigned_sms_review.down.sql"):
                with pytest.raises(psycopg.errors.RaiseException, match="evidence exists|review history exists"):
                    with conn.transaction():
                        conn.execute(migration_body((migration_directory(config) / name).read_text()), prepare=False)
            assert conn.execute("SELECT text,disposition FROM chase_communication_events").fetchall() == [("STOP", "sms_opt_out")]


def test_forced_tenant_boundary_survives_a_future_read_grant(schema):
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo
    config, _ = schema
    # A disposable grant proves actual session-bound RLS; finally removes it.
    with psycopg.connect(config.database_url, autocommit=True) as owner:
        other = "sms-other-" + uuid4().hex[:8]
        owner.execute("INSERT INTO practices(practice_id) VALUES (%s)", (other,))
        insert_inbound(owner, config.practice_id, "rls-local")
        insert_inbound(owner, other, "rls-other")
        owner.execute(sql.SQL("GRANT SELECT ON chase_communication_events TO {}").format(sql.Identifier(config.database_runtime_role)))
        try:
            with psycopg.connect(make_conninfo(config.database_url, user=config.database_runtime_role), autocommit=True) as conn:
                conn.execute("SELECT set_config('cosmaneura.practice_id',%s,false)", (config.practice_id,))
                assert conn.execute("SELECT event_id FROM chase_communication_events").fetchall() == [("rls-local",)]
                conn.execute("SELECT set_config('cosmaneura.practice_id',%s,false)", (other,))
                assert conn.execute("SELECT event_id FROM chase_communication_events").fetchall() == []
        finally:
            owner.execute(sql.SQL("REVOKE SELECT ON chase_communication_events FROM {}").format(sql.Identifier(config.database_runtime_role)))
    assert apply_migrations(config) == []


def test_checksum_drift_stops_runner_without_changing_ledger(schema, tmp_path):
    import psycopg
    config, paths = schema
    for path in paths:
        shutil.copyfile(path, tmp_path / path.name)
    changed = next(p for p in paths if p.name.startswith("010_"))
    (tmp_path / changed.name).write_bytes(changed.read_bytes() + b"\n-- changed\n")
    with psycopg.connect(config.database_url) as conn:
        before = conn.execute("SELECT name,checksum_sha256 FROM cosmaneura_schema_migrations ORDER BY name").fetchall()
    with pytest.raises(RuntimeError, match="applied migration checksum changed"):
        apply_migrations(replace(config, migrations_dir=str(tmp_path)))
    with psycopg.connect(config.database_url) as conn:
        assert conn.execute("SELECT name,checksum_sha256 FROM cosmaneura_schema_migrations ORDER BY name").fetchall() == before
        assert conn.execute("SELECT pg_try_advisory_lock(4836623987031721441)").fetchone()[0]
        conn.execute("SELECT pg_advisory_unlock(4836623987031721441)")
