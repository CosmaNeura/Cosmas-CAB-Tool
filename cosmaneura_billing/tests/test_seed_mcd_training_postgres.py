"""Real owner/RLS/atomicity proof for the fixed MCD source in disposable PG."""
from dataclasses import replace
from uuid import uuid4

import pytest

psycopg = pytest.importorskip("psycopg")
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row

from cosmaneura_billing import seed_demo_release_candidate as existing
from cosmaneura_billing import seed_mcd_training as seeder
from cosmaneura_billing.settings import Settings
from test_synthetic_demo_immutability_postgres import TEST_DSN, _immutability_schema


pytestmark = pytest.mark.skipif(not TEST_DSN, reason="COSMANEURA_TEST_DATABASE_URL is not configured")


@pytest.fixture
def practice(_immutability_schema):
    settings = Settings(
        auth_mode="container_apps", state_store="postgres", database_url=TEST_DSN,
        practice_id=f"mcd-training-{uuid4().hex}", entra_tenant_id=str(uuid4()),
    )
    with psycopg.connect(TEST_DSN) as owner:
        owner.execute("INSERT INTO practices(practice_id, entra_tenant_id) VALUES (%s,%s)",
                      (settings.practice_id, settings.entra_tenant_id))
    try:
        yield settings
    finally:
        with existing.PostgresContext(TEST_DSN, practice_id=settings.practice_id).connection() as owner:
            owner.execute("DELETE FROM encounters WHERE practice_id=%s", (settings.practice_id,))
            owner.execute("DELETE FROM practices WHERE practice_id=%s", (settings.practice_id,))


def rows(settings):
    with existing.PostgresContext(TEST_DSN, practice_id=settings.practice_id).connection() as owner:
        return owner.execute("SELECT * FROM encounters WHERE practice_id=%s ORDER BY external_id",
                             (settings.practice_id,)).fetchall()


def test_exact_source_persists_with_stable_versions_uids_and_timestamps(practice):
    old_result = existing.seed_synthetic_demo_release_candidate(practice)
    before = rows(practice)
    assert seeder.seed_mcd_training(practice)["inserted_count"] == 12
    seeded = rows(practice)
    assert len(seeded) == 24
    assert seeder.seed_mcd_training(practice)["replayed_count"] == 12
    assert rows(practice) == seeded
    assert existing.seed_synthetic_demo_release_candidate(practice) == {
        **old_result, "inserted_count": 0, "replayed_count": 12, "status": "replayed",
    }
    assert [row for row in seeded if row["external_id"] in existing.SEED_CASE_IDS] == before
    with existing.PostgresContext(TEST_DSN, practice_id=practice.practice_id).connection() as owner:
        for case in seeder.prepare_mcd_training_seed().cases:
            assert existing._load_persisted_case(owner, practice, case.case_id) == existing._expected_persisted_values(case)


@pytest.mark.parametrize(("field", "value"), [
    ("signed_note", "Conflicting synthetic note."), ("source", "manual"), ("version", 2),
])
def test_final_case_conflict_rolls_back_all_eleven_earlier_inserts(practice, field, value):
    final = seeder.prepare_mcd_training_seed().cases[-1]
    with existing.PostgresContext(TEST_DSN, practice_id=practice.practice_id).connection() as owner:
        existing._insert_case(owner, practice, final)
        owner.execute(sql.SQL("UPDATE encounters SET {}=%s WHERE practice_id=%s AND external_id=%s")
                      .format(sql.Identifier(field)), (value, practice.practice_id, final.case_id))
    before = rows(practice)
    with pytest.raises(existing.SyntheticDemoConflict):
        seeder.seed_mcd_training(practice)
    assert rows(practice) == before
    assert len(before) == 1


@pytest.mark.parametrize("failure", ["wrong_tenant", "inactive", "missing_practice"])
def test_unproven_practice_cannot_seed(practice, failure):
    settings = practice
    if failure == "wrong_tenant":
        settings = replace(practice, entra_tenant_id=str(uuid4()))
    elif failure == "inactive":
        with psycopg.connect(TEST_DSN) as owner:
            owner.execute("UPDATE practices SET active=false WHERE practice_id=%s", (practice.practice_id,))
    else:
        settings = replace(practice, practice_id=f"missing-{uuid4().hex}")
    with pytest.raises(existing.SyntheticDemoPracticeBindingError):
        seeder.seed_mcd_training(settings)
    assert rows(practice) == []


def test_runtime_cannot_seed_or_mutate_mcd_source_and_cannot_switch_practice(practice):
    seeder.seed_mcd_training(practice)
    before = rows(practice)
    role = f"mcd_runtime_{uuid4().hex[:16]}"
    password = uuid4().hex
    other_practice = f"mcd-other-{uuid4().hex}"
    with psycopg.connect(TEST_DSN) as owner:
        owner.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS")
                      .format(sql.Identifier(role), sql.Literal(password)))
        owner.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}")
                      .format(sql.Identifier(owner.info.dbname), sql.Identifier(role)))
        owner.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(role)))
        owner.execute(sql.SQL("GRANT SELECT,INSERT,UPDATE,DELETE ON encounters TO {}").format(sql.Identifier(role)))
        owner.execute(sql.SQL("GRANT EXECUTE ON FUNCTION cosmaneura_practice_allowed(text) TO {}").format(sql.Identifier(role)))
        owner.execute("INSERT INTO runtime_practice_bindings(role_name,practice_id) VALUES (%s,%s)", (role, practice.practice_id))
        owner.execute("INSERT INTO practices(practice_id,entra_tenant_id) VALUES (%s,%s)", (other_practice, str(uuid4())))
    runtime_dsn = make_conninfo(TEST_DSN, user=role, password=password)
    try:
        with pytest.raises(existing.SyntheticDemoDatabaseAuthorityError):
            seeder.seed_mcd_training(replace(practice, database_url=runtime_dsn))
        with psycopg.connect(runtime_dsn, row_factory=dict_row) as runtime:
            runtime.execute("SELECT set_config('cosmaneura.practice_id',%s,true)", (practice.practice_id,))
            assert runtime.execute("SELECT count(*) AS n FROM encounters").fetchone()["n"] == 12
            for statement, args in [
                ("UPDATE encounters SET signed_note='Forbidden synthetic edit' WHERE external_id=%s", (seeder.CASE_IDS[-1],)),
                ("UPDATE encounters SET source='manual' WHERE external_id=%s", (seeder.CASE_IDS[-1],)),
                ("DELETE FROM encounters WHERE external_id=%s", (seeder.CASE_IDS[-1],)),
            ]:
                with pytest.raises(psycopg.errors.CheckViolation):
                    with runtime.transaction(force_rollback=True):
                        runtime.execute(statement, args)
            with pytest.raises(psycopg.errors.CheckViolation):
                with runtime.transaction(force_rollback=True):
                    case = seeder.prepare_mcd_training_seed().cases[-1]
                    altered = existing.PreparedSyntheticDemoCase("SYN-CAB-UNAPPROVED", case.visit, case.digest)
                    existing._insert_case(runtime, practice, altered)
            runtime.execute("SELECT set_config('cosmaneura.practice_id',%s,true)", (other_practice,))
            assert runtime.execute("SELECT count(*) AS n FROM encounters WHERE practice_id=%s", (practice.practice_id,)).fetchone()["n"] == 0
            assert runtime.execute("SELECT cosmaneura_practice_allowed(%s) AS allowed", (other_practice,)).fetchone()["allowed"] is False
        assert rows(practice) == before
    finally:
        with psycopg.connect(TEST_DSN) as owner:
            owner.execute("DELETE FROM runtime_practice_bindings WHERE role_name=%s", (role,))
            owner.execute("DELETE FROM practices WHERE practice_id=%s", (other_practice,))
            owner.execute(sql.SQL("REVOKE SELECT,INSERT,UPDATE,DELETE ON encounters FROM {}").format(sql.Identifier(role)))
            owner.execute(sql.SQL("REVOKE EXECUTE ON FUNCTION cosmaneura_practice_allowed(text) FROM {}").format(sql.Identifier(role)))
            owner.execute(sql.SQL("REVOKE USAGE ON SCHEMA public FROM {}").format(sql.Identifier(role)))
            owner.execute(sql.SQL("REVOKE CONNECT ON DATABASE {} FROM {}")
                          .format(sql.Identifier(owner.info.dbname), sql.Identifier(role)))
            owner.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
