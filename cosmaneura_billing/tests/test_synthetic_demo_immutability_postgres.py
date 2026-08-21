"""Live PostgreSQL proof for reserved synthetic-demo encounter immutability."""
from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest

psycopg = pytest.importorskip("psycopg")
from psycopg import sql
from psycopg.rows import dict_row

from cosmaneura_billing.seed_demo_release_candidate import (
    _validate_database_seed_authority,
)


TEST_DSN = os.environ.get("COSMANEURA_TEST_DATABASE_URL", "")
ROOT = Path(__file__).resolve().parents[1]
RESERVED_SOURCE = "synthetic_demo_release_candidate"
FUNCTION = "public.cosmaneura_guard_synthetic_demo_encounter()"
ROW_TRIGGER = "encounters_synthetic_demo_immutability"
TRUNCATE_TRIGGER = "encounters_synthetic_demo_truncate_immutability"

pytestmark = pytest.mark.skipif(
    not TEST_DSN, reason="COSMANEURA_TEST_DATABASE_URL is not configured",
)


def _relation_exists(connection, name: str) -> bool:
    return connection.execute(
        "SELECT pg_catalog.to_regclass(%s)", (f"public.{name}",),
    ).fetchone()[0] is not None


@pytest.fixture(scope="module", autouse=True)
def _immutability_schema():
    if not TEST_DSN:
        yield
        return
    migrations = ROOT / "migrations"
    with psycopg.connect(TEST_DSN, autocommit=True) as connection:
        if "test" not in (connection.info.dbname or "").lower():
            pytest.fail(
                "COSMANEURA_TEST_DATABASE_URL must name a disposable test database"
            )
        if not _relation_exists(connection, "chase_items"):
            connection.execute(
                (migrations / "002_chase_persistence.up.sql").read_text(
                    encoding="utf-8"
                ),
                prepare=False,
            )
        if not _relation_exists(connection, "encounters"):
            connection.execute(
                (migrations / "003_pilot_persistence.up.sql").read_text(
                    encoding="utf-8"
                ),
                prepare=False,
            )
        # 007 is safely repeatable and 008 deliberately uses a drop/create
        # trigger sequence, so this module also exercises its reapply posture.
        for name in (
            "007_synthetic_demo_release_candidate_source.up.sql",
            "008_synthetic_demo_encounter_immutability.up.sql",
        ):
            connection.execute(
                (migrations / name).read_text(encoding="utf-8"),
                prepare=False,
            )
    yield


def _insert_encounter(connection, practice_id: str, external_id: str, source: str):
    return connection.execute(
        """INSERT INTO public.encounters (
               practice_id, external_id, patient_age, patient_sex,
               encounter_type, place_of_service, intake_text, transcript,
               signed_note, provider_specialty, submitted_em_code,
               submitted_icd10, source
           ) VALUES (
               %s, %s, 44, 'unknown', 'office_established', '11',
               'Synthetic intake.', 'Synthetic transcript.',
               'Synthetic signed note.', 'Family Medicine', NULL,
               ARRAY[]::text[], %s
           )""",
        (practice_id, external_id, source),
    )


def _expect_reserved_rejection(connection, statement: str, parameters=()):
    with pytest.raises(
        psycopg.errors.CheckViolation,
        match="reserved synthetic demo encounters require the table owner",
    ) as error:
        with connection.transaction(force_rollback=True):
            connection.execute(statement, parameters)
    assert error.value.sqlstate == "23514"


def _visible_encounter_fingerprint(connection) -> tuple[tuple[object, ...], ...]:
    """Return a stable, non-PHI test-row snapshot around TRUNCATE attempts."""
    return tuple(connection.execute(
        """SELECT external_id, patient_age, patient_sex, encounter_type,
                  place_of_service, intake_text, transcript, signed_note,
                  provider_specialty, submitted_em_code, submitted_icd10,
                  source, version
             FROM public.encounters
            ORDER BY external_id"""
    ).fetchall())


def _revoke_test_role_grants(
    connection,
    role: str,
    *,
    replica_parameter_granted: bool,
) -> None:
    """Undo only grants created by this test; the role owns no objects."""
    database_name = connection.execute(
        "SELECT current_database()",
    ).fetchone()[0]
    identifier = sql.Identifier(role)
    connection.execute(
        sql.SQL("REVOKE CONNECT ON DATABASE {} FROM {}").format(
            sql.Identifier(database_name), identifier,
        )
    )
    connection.execute(
        sql.SQL("REVOKE USAGE ON SCHEMA public FROM {}").format(identifier)
    )
    connection.execute(
        sql.SQL(
            "REVOKE SELECT, INSERT, UPDATE, DELETE "
            "ON public.encounters FROM {}"
        ).format(identifier)
    )
    connection.execute(
        sql.SQL(
            "REVOKE TRUNCATE ON ALL TABLES IN SCHEMA public FROM {}"
        ).format(identifier)
    )
    connection.execute(
        sql.SQL(
            "REVOKE EXECUTE ON FUNCTION "
            "public.cosmaneura_practice_allowed(text) FROM {}"
        ).format(identifier)
    )
    if replica_parameter_granted:
        connection.execute(
            sql.SQL(
                "REVOKE SET ON PARAMETER session_replication_role FROM {}"
            ).format(identifier)
        )


def test_only_table_owner_can_mutate_reserved_source_even_with_full_table_grant():
    suffix = uuid4().hex
    practice_id = f"synthetic-guard-{suffix}"
    tenant_id = str(uuid4())
    roles = (
        f"synthetic_runtime_{suffix[:16]}",
        f"synthetic_worker_{suffix[:16]}",
    )
    password = f"Synthetic-{uuid4().hex}"
    owner_seed_id = f"owner-seed-{suffix}"
    owner_cleanup_id = f"owner-cleanup-{suffix}"
    created_roles: list[str] = []
    replica_parameter_roles: set[str] = set()
    body_error: BaseException | None = None

    try:
        with psycopg.connect(TEST_DSN) as owner:
            owner_name, table_owner = owner.execute(
                """SELECT current_user::text,
                          pg_catalog.pg_get_userbyid(relation.relowner)::text
                     FROM pg_catalog.pg_class AS relation
                    WHERE relation.oid =
                          pg_catalog.to_regclass('public.encounters')"""
            ).fetchone()
            assert owner_name == table_owner
            owner.execute(
                "SELECT set_config('cosmaneura.practice_id', %s, true)",
                (practice_id,),
            )
            owner.execute(
                """INSERT INTO public.practices (
                       practice_id, entra_tenant_id, display_name
                   ) VALUES (%s, %s::uuid, 'Synthetic guard test')""",
                (practice_id, tenant_id),
            )

            database_name = owner.execute(
                "SELECT current_database()",
            ).fetchone()[0]
            owner_is_superuser = owner.execute(
                """SELECT role.rolsuper
                     FROM pg_catalog.pg_roles AS role
                    WHERE role.rolname = current_user"""
            ).fetchone()[0]
            for role in roles:
                owner.execute(
                    sql.SQL(
                        "CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER "
                        "NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS"
                    ).format(sql.Identifier(role), sql.Literal(password))
                )
                created_roles.append(role)
                owner.execute(
                    sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                        sql.Identifier(database_name), sql.Identifier(role),
                    )
                )
                owner.execute(
                    sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(
                        sql.Identifier(role)
                    )
                )
                # Deliberately grant more than either provisioned role has.
                # The trigger must remain the defense even after grant drift.
                owner.execute(
                    sql.SQL(
                        "GRANT SELECT, INSERT, UPDATE, DELETE "
                        "ON public.encounters TO {}"
                    ).format(sql.Identifier(role))
                )
                # Simulate a future table-grant regression. The trigger must
                # block TRUNCATE before any cascading table is touched.
                owner.execute(
                    sql.SQL(
                        "GRANT TRUNCATE ON ALL TABLES IN SCHEMA public TO {}"
                    ).format(sql.Identifier(role))
                )
                owner.execute(
                    sql.SQL(
                        "GRANT EXECUTE ON FUNCTION "
                        "public.cosmaneura_practice_allowed(text) TO {}"
                    ).format(sql.Identifier(role))
                )
                owner.execute(
                    """INSERT INTO public.runtime_practice_bindings (
                           role_name, practice_id
                       ) VALUES (%s, %s)""",
                    (role, practice_id),
                )
                assert owner.execute(
                    """SELECT pg_catalog.has_table_privilege(
                                   %s, 'public.encounters', 'TRIGGER'
                               ),
                              pg_catalog.has_schema_privilege(
                                   %s, 'public', 'CREATE'
                               ),
                              pg_catalog.has_parameter_privilege(
                                   %s, 'session_replication_role', 'SET'
                               )""",
                    (role, role, role),
                ).fetchone() == (False, False, False)
                if owner_is_superuser:
                    # CI's disposable postgres role can simulate parameter
                    # grant drift. ENABLE ALWAYS must keep both guards active.
                    owner.execute(
                        sql.SQL(
                            "GRANT SET ON PARAMETER "
                            "session_replication_role TO {}"
                        ).format(sql.Identifier(role))
                    )
                    replica_parameter_roles.add(role)

            _insert_encounter(owner, practice_id, owner_seed_id, RESERVED_SOURCE)
            assert owner.execute(
                """UPDATE public.encounters
                      SET signed_note = 'Owner-reviewed synthetic note.'
                    WHERE practice_id = %s AND external_id = %s""",
                (practice_id, owner_seed_id),
            ).rowcount == 1
            _insert_encounter(
                owner, practice_id, owner_cleanup_id, RESERVED_SOURCE,
            )
            assert owner.execute(
                """DELETE FROM public.encounters
                    WHERE practice_id = %s AND external_id = %s""",
                (practice_id, owner_cleanup_id),
            ).rowcount == 1

            function_owner, security_definer, function_config = owner.execute(
                """SELECT pg_catalog.pg_get_userbyid(proc.proowner)::text,
                          proc.prosecdef,
                          proc.proconfig
                     FROM pg_catalog.pg_proc AS proc
                    WHERE proc.oid = pg_catalog.to_regprocedure(%s)""",
                (FUNCTION,),
            ).fetchone()
            assert function_owner == table_owner
            assert security_definer is False
            assert function_config == ["search_path=pg_catalog"]
            trigger_metadata = {
                name: (enabled, trigger_type, condition, columns, arguments)
                for (
                    name,
                    enabled,
                    trigger_type,
                    condition,
                    columns,
                    arguments,
                ) in owner.execute(
                    """SELECT trigger.tgname,
                              trigger.tgenabled,
                              trigger.tgtype,
                              trigger.tgqual,
                              trigger.tgattr::text,
                              trigger.tgnargs
                         FROM pg_catalog.pg_trigger AS trigger
                        WHERE trigger.tgrelid =
                              pg_catalog.to_regclass('public.encounters')
                          AND trigger.tgname = ANY(%s)
                          AND NOT trigger.tgisinternal""",
                    ([ROW_TRIGGER, TRUNCATE_TRIGGER],),
                ).fetchall()
            }
            assert trigger_metadata == {
                ROW_TRIGGER: ("A", 31, None, "", 0),
                TRUNCATE_TRIGGER: ("A", 34, None, "", 0),
            }
            for role in roles:
                assert owner.execute(
                    "SELECT pg_catalog.has_function_privilege(%s, %s, 'EXECUTE')",
                    (role, FUNCTION),
                ).fetchone()[0] is False

        with psycopg.connect(TEST_DSN, row_factory=dict_row) as owner:
            _validate_database_seed_authority(owner)

        for index, role in enumerate(roles):
            ordinary_id = f"ordinary-{index}-{suffix}"
            forbidden_id = f"forbidden-{index}-{suffix}"
            with psycopg.connect(
                TEST_DSN, user=role, password=password,
            ) as nonowner:
                nonowner.execute(
                    "SELECT set_config('cosmaneura.practice_id', %s, true)",
                    (practice_id,),
                )
                if role in replica_parameter_roles:
                    nonowner.execute("SET session_replication_role = replica")
                    assert nonowner.execute(
                        "SHOW session_replication_role"
                    ).fetchone()[0] == "replica"
                else:
                    with pytest.raises(psycopg.errors.InsufficientPrivilege):
                        with nonowner.transaction(force_rollback=True):
                            nonowner.execute(
                                "SET session_replication_role = replica"
                            )
                _insert_encounter(nonowner, practice_id, ordinary_id, "manual")
                assert nonowner.execute(
                    """UPDATE public.encounters
                          SET signed_note = 'Ordinary update.'
                        WHERE practice_id = %s AND external_id = %s""",
                    (practice_id, ordinary_id),
                ).rowcount == 1

                _expect_reserved_rejection(
                    nonowner,
                    """INSERT INTO public.encounters (
                           practice_id, external_id, patient_age, patient_sex,
                           encounter_type, place_of_service, source
                       ) VALUES (%s, %s, 44, 'unknown',
                                 'office_established', '11', %s)""",
                    (practice_id, forbidden_id, RESERVED_SOURCE),
                )
                _expect_reserved_rejection(
                    nonowner,
                    """UPDATE public.encounters SET source = %s
                        WHERE practice_id = %s AND external_id = %s""",
                    (RESERVED_SOURCE, practice_id, ordinary_id),
                )
                _expect_reserved_rejection(
                    nonowner,
                    """UPDATE public.encounters SET source = 'manual'
                        WHERE practice_id = %s AND external_id = %s""",
                    (practice_id, owner_seed_id),
                )
                _expect_reserved_rejection(
                    nonowner,
                    """UPDATE public.encounters
                          SET signed_note = 'Forbidden update.'
                        WHERE practice_id = %s AND external_id = %s""",
                    (practice_id, owner_seed_id),
                )
                _expect_reserved_rejection(
                    nonowner,
                    """DELETE FROM public.encounters
                        WHERE practice_id = %s AND external_id = %s""",
                    (practice_id, owner_seed_id),
                )
                before_truncate = _visible_encounter_fingerprint(nonowner)
                _expect_reserved_rejection(
                    nonowner,
                    "TRUNCATE TABLE public.encounters CASCADE",
                )
                assert _visible_encounter_fingerprint(nonowner) == before_truncate

                assert nonowner.execute(
                    """SELECT source, signed_note FROM public.encounters
                        WHERE practice_id = %s AND external_id = %s""",
                    (practice_id, owner_seed_id),
                ).fetchone() == (
                    RESERVED_SOURCE,
                    "Owner-reviewed synthetic note.",
                )
                assert nonowner.execute(
                    """DELETE FROM public.encounters
                        WHERE practice_id = %s AND external_id = %s""",
                    (practice_id, ordinary_id),
                ).rowcount == 1
    except BaseException as exc:
        body_error = exc
        raise
    finally:
        if TEST_DSN:
            try:
                with psycopg.connect(TEST_DSN, autocommit=True) as owner:
                    owner.execute(
                        "SELECT set_config('cosmaneura.practice_id', %s, false)",
                        (practice_id,),
                    )
                    owner.execute(
                        "DELETE FROM public.encounters WHERE practice_id = %s",
                        (practice_id,),
                    )
                    owner.execute(
                        """DELETE FROM public.runtime_practice_bindings
                            WHERE role_name = ANY(%s)""",
                        (list(roles),),
                    )
                    owner.execute(
                        "DELETE FROM public.practices WHERE practice_id = %s",
                        (practice_id,),
                    )
                    for role in created_roles:
                        _revoke_test_role_grants(
                            owner,
                            role,
                            replica_parameter_granted=(
                                role in replica_parameter_roles
                            ),
                        )
                        owner.execute(
                            sql.SQL("DROP ROLE IF EXISTS {}").format(
                                sql.Identifier(role)
                            )
                        )
            except Exception:
                # Keep the test's body failure primary; a cleanup error must
                # never replace the assertion or database exception under test.
                if body_error is None:
                    raise
