"""Live PostgreSQL proof for source-import roles, RLS, replay, and atomicity.

The configured database must be disposable and include ``test`` in its name.
"""
from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

psycopg = pytest.importorskip("psycopg")
from psycopg import sql
from psycopg.conninfo import make_conninfo

from cosmaneura_billing.athena_client import MockAthenaClient
from cosmaneura_billing.migrations import (
    _prepare_bound_login,
    _provision_athena_import_role,
)
from cosmaneura_billing.settings import Settings
from cosmaneura_billing.source_import import (
    PostgresSourceImportStore,
    SourceImportConflict,
    source_import_external_id,
    source_import_idempotency_key,
)


TEST_DSN = os.environ.get("COSMANEURA_TEST_DATABASE_URL", "")
ROOT = Path(__file__).resolve().parents[1]
SOURCE_SYSTEM = "athena_preview"
PROVENANCE = "athena_preview_synthetic"

pytestmark = pytest.mark.skipif(
    not TEST_DSN, reason="COSMANEURA_TEST_DATABASE_URL is not configured",
)


def _relation_exists(connection, name: str) -> bool:
    return connection.execute(
        "SELECT pg_catalog.to_regclass(%s)", (f"public.{name}",),
    ).fetchone()[0] is not None


@pytest.fixture(scope="module", autouse=True)
def _source_import_schema():
    if not TEST_DSN:
        yield
        return
    migrations = ROOT / "migrations"
    with psycopg.connect(TEST_DSN, autocommit=True) as connection:
        if "test" not in (connection.info.dbname or "").casefold():
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
        source_values = connection.execute(
            """SELECT pg_catalog.pg_get_constraintdef(constraint_row.oid)
                 FROM pg_catalog.pg_constraint AS constraint_row
                WHERE constraint_row.conrelid =
                      pg_catalog.to_regclass('public.encounters')
                  AND constraint_row.conname = 'encounters_source_valid'"""
        ).fetchone()[0]
        if "synthetic_demo_release_candidate" not in source_values:
            connection.execute(
                (
                    migrations
                    / "007_synthetic_demo_release_candidate_source.up.sql"
                ).read_text(encoding="utf-8"),
                prepare=False,
            )
        if connection.execute(
            "SELECT pg_catalog.to_regprocedure("
            "'public.cosmaneura_guard_synthetic_demo_encounter()')"
        ).fetchone()[0] is None:
            connection.execute(
                (
                    migrations
                    / "008_synthetic_demo_encounter_immutability.up.sql"
                ).read_text(encoding="utf-8"),
                prepare=False,
            )
        if not _relation_exists(connection, "source_import_receipts"):
            connection.execute(
                (migrations / "009_source_import_foundation.up.sql").read_text(
                    encoding="utf-8"
                ),
                prepare=False,
            )
    yield


def _insert_encounter(
    connection,
    *,
    practice_id: str,
    external_id: str,
    source: str | None = None,
):
    columns = "practice_id, external_id, patient_age, patient_sex, " \
        "encounter_type, place_of_service, intake_text, transcript, " \
        "signed_note, provider_specialty, submitted_icd10"
    placeholders = "%s, %s, 44, 'unknown', 'office_established', '11', " \
        "'Synthetic intake.', 'Synthetic transcript.', " \
        "'Synthetic signed note.', 'Family Medicine', ARRAY[]::text[]"
    parameters: tuple[object, ...] = (practice_id, external_id)
    if source is not None:
        columns += ", source"
        placeholders += ", %s"
        parameters += (source,)
    return connection.execute(
        f"""INSERT INTO public.encounters ({columns})
            VALUES ({placeholders})
            RETURNING encounter_uid, practice_id, source""",
        parameters,
    ).fetchone()


def _drop_role(connection, role: str) -> None:
    if connection.execute(
        "SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = %s", (role,),
    ).fetchone():
        connection.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
        connection.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


def _external_id(record) -> str:
    return source_import_external_id(
        practice_id=record.target_practice_id,
        source_system=SOURCE_SYSTEM,
        feed="encounters",
        source_record_id=record.source_record_id,
    )


def test_athena_import_role_is_exactly_scoped_and_page_commit_is_atomic():
    suffix = uuid4().hex
    practice_a = f"source-import-a-{suffix}"
    practice_b = f"source-import-b-{suffix}"
    tenant_a = str(uuid4())
    tenant_b = str(uuid4())
    import_role = f"athena_import_{suffix[:18]}"
    ordinary_role = f"ordinary_app_{suffix[:19]}"
    import_password = f"Synthetic-import-{suffix}"
    ordinary_password = f"Synthetic-app-{suffix}"
    manual_external_id = f"manual-encounter-{suffix}"
    page = MockAthenaClient(server_practice_id=practice_a).read_page()
    colliding_demo_external_id = page.records[0].visit.encounter_id

    try:
        with psycopg.connect(TEST_DSN) as owner:
            for practice_id, tenant_id in (
                (practice_a, tenant_a),
                (practice_b, tenant_b),
            ):
                owner.execute(
                    "SELECT set_config('cosmaneura.practice_id', %s, true)",
                    (practice_id,),
                )
                owner.execute(
                    """INSERT INTO public.practices (
                           practice_id, entra_tenant_id, display_name
                       ) VALUES (%s, %s::uuid, 'Synthetic import test')""",
                    (practice_id, tenant_id),
                )
            owner.execute(
                "SELECT set_config('cosmaneura.practice_id', %s, false)",
                (practice_a,),
            )
            _insert_encounter(
                owner,
                practice_id=practice_a,
                external_id=manual_external_id,
                source="manual",
            )
            _insert_encounter(
                owner,
                practice_id=practice_a,
                external_id=colliding_demo_external_id,
                source="synthetic_demo_release_candidate",
            )

            settings = Settings(
                database_url=TEST_DSN,
                practice_id=practice_a,
                athena_import_database_role=import_role,
                athena_import_database_password=import_password,
            )
            _provision_athena_import_role(owner, settings)
            _provision_athena_import_role(owner, settings)

            sql_module, ordinary_identifier = _prepare_bound_login(
                owner,
                role=ordinary_role,
                password_value=ordinary_password,
                practice_id=practice_a,
                description="synthetic ordinary role",
            )
            owner.execute(sql_module.SQL(
                "GRANT SELECT, INSERT, UPDATE ON public.encounters TO {}"
            ).format(ordinary_identifier))
            owner.execute(sql_module.SQL(
                "GRANT EXECUTE ON FUNCTION "
                "public.cosmaneura_practice_allowed(text), "
                "public.cosmaneura_source_import_session(), "
                "public.cosmaneura_source_import_allowed(text, text, text) TO {}"
            ).format(ordinary_identifier))

        with psycopg.connect(TEST_DSN) as owner:
            role_flags = owner.execute(
                """SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole,
                          rolinherit, rolreplication, rolbypassrls
                     FROM pg_catalog.pg_roles WHERE rolname = %s""",
                (import_role,),
            ).fetchone()
            assert role_flags == (True, False, False, False, False, False, False)
            assert owner.execute(
                """SELECT practice_id, source_system, provenance
                     FROM public.source_import_role_bindings
                    WHERE role_name = %s""",
                (import_role,),
            ).fetchone() == (practice_a, SOURCE_SYSTEM, PROVENANCE)
            role_oid = owner.execute(
                "SELECT oid FROM pg_catalog.pg_roles WHERE rolname = %s",
                (import_role,),
            ).fetchone()[0]
            assert owner.execute(
                """SELECT count(*) FROM pg_catalog.pg_auth_members
                    WHERE member = %s OR roleid = %s""",
                (role_oid, role_oid),
            ).fetchone()[0] == 0
            assert owner.execute(
                """SELECT has_table_privilege(%s, 'public.encounters', 'SELECT'),
                          has_table_privilege(%s, 'public.encounters', 'INSERT'),
                          has_table_privilege(%s, 'public.encounters', 'UPDATE'),
                          has_table_privilege(
                              %s, 'public.source_import_receipts', 'SELECT'
                          ),
                          has_table_privilege(
                              %s, 'public.source_import_receipts', 'INSERT'
                          ),
                          has_table_privilege(
                              %s, 'public.source_import_checkpoints', 'SELECT'
                          ),
                          has_table_privilege(
                              %s, 'public.source_import_checkpoints', 'INSERT'
                          ),
                          has_function_privilege(
                              %s,
                              'public.cosmaneura_commit_source_encounter_page('
                              'text,text,text,text,text,bigint,jsonb,text,text)',
                              'EXECUTE'
                          )""",
                (import_role,) * 8,
            ).fetchone() == (
                False, False, False, False, False, True, False, True,
            )

        import_dsn = make_conninfo(
            TEST_DSN, user=import_role, password=import_password,
        )
        store = PostgresSourceImportStore(import_dsn, practice_id=practice_a)
        arguments = {
            "source_system": SOURCE_SYSTEM,
            "feed": "encounters",
            "provenance": PROVENANCE,
            "mapping_version": page.records[0].mapping_version,
            "records": page.records,
            "checkpoint": page.checkpoint,
            "next_cursor": page.next_cursor,
        }
        with pytest.raises(SourceImportConflict):
            store.import_encounter_page(
                **{**arguments, "provenance": "unregistered_import_source"}
            )
        assert store.load_checkpoint(
            source_system=SOURCE_SYSTEM, feed="encounters",
        ) is None
        with psycopg.connect(TEST_DSN) as owner:
            assert owner.execute(
                """SELECT count(*) FROM public.source_import_receipts
                    WHERE practice_id = %s""",
                (practice_a,),
            ).fetchone()[0] == 0
            assert owner.execute(
                """SELECT count(*) FROM public.encounters
                    WHERE practice_id = %s AND external_id = %s""",
                (practice_a, _external_id(page.records[0])),
            ).fetchone()[0] == 0

        first = store.import_encounter_page(**arguments)
        assert (first.imported_count, first.replayed_count) == (2, 0)
        first_checkpoint = store.load_checkpoint(
            source_system=SOURCE_SYSTEM, feed="encounters",
        )
        assert first_checkpoint is not None
        assert first_checkpoint.version == 1

        with psycopg.connect(TEST_DSN) as owner:
            owner.execute(
                "SELECT set_config('cosmaneura.practice_id', %s, false)",
                (practice_a,),
            )
            assert owner.execute(
                """SELECT source FROM public.encounters
                    WHERE external_id = %s""",
                (colliding_demo_external_id,),
            ).fetchone()[0] == "synthetic_demo_release_candidate"
            assert owner.execute(
                """SELECT source FROM public.encounters
                    WHERE external_id = %s""",
                (_external_id(page.records[0]),),
            ).fetchone()[0] == PROVENANCE

        with psycopg.connect(TEST_DSN) as owner:
            owner.execute(
                "SELECT set_config('cosmaneura.practice_id', %s, false)",
                (practice_a,),
            )
            original_versions = dict(owner.execute(
                "SELECT external_id, version FROM public.encounters"
            ).fetchall())
            assert {
                _external_id(record) for record in page.records
            }.issubset(original_versions)
            original_versions = {
                external_id: original_versions[external_id]
                for external_id in {
                    _external_id(record) for record in page.records
                }
            }

        replayed = store.import_encounter_page(
            **{
                **arguments,
                "expected_checkpoint_version": first_checkpoint.version,
            }
        )
        assert (replayed.imported_count, replayed.replayed_count) == (0, 2)
        assert store.load_checkpoint(
            source_system=SOURCE_SYSTEM, feed="encounters",
        ) == first_checkpoint

        original = page.records[0]
        stale_store = PostgresSourceImportStore(
            import_dsn,
            practice_id=practice_a,
        )
        stale_checkpoint = stale_store.load_checkpoint(
            source_system=SOURCE_SYSTEM,
            feed="encounters",
        )
        assert stale_checkpoint == first_checkpoint
        changed = replace(
            original,
            source_version="source-version-2",
            idempotency_key=source_import_idempotency_key(
                practice_id=practice_a,
                source_system=SOURCE_SYSTEM,
                feed="encounters",
                source_record_id=original.source_record_id,
                source_version="source-version-2",
                mapping_version=original.mapping_version,
            ),
            visit=replace(
                original.visit,
                signed_note=original.visit.signed_note + " Corrected.",
            ),
        )
        updated = store.import_encounter_page(
            source_system=SOURCE_SYSTEM,
            feed="encounters",
            provenance=PROVENANCE,
            mapping_version=changed.mapping_version,
            records=(changed,),
            checkpoint="mock-checkpoint-updated",
            next_cursor=None,
            expected_checkpoint_version=first_checkpoint.version,
        )
        assert (updated.imported_count, updated.replayed_count) == (1, 0)
        updated_checkpoint = store.load_checkpoint(
            source_system=SOURCE_SYSTEM, feed="encounters",
        )
        assert updated_checkpoint is not None
        assert updated_checkpoint.version == 2
        with pytest.raises(SourceImportConflict):
            stale_store.import_encounter_page(
                source_system=SOURCE_SYSTEM,
                feed="encounters",
                provenance=PROVENANCE,
                mapping_version=changed.mapping_version,
                records=(changed,),
                checkpoint="stale-writer-must-not-win",
                next_cursor=None,
                expected_checkpoint_version=stale_checkpoint.version,
            )
        assert store.load_checkpoint(
            source_system=SOURCE_SYSTEM,
            feed="encounters",
        ) == updated_checkpoint

        with psycopg.connect(TEST_DSN) as owner:
            owner.execute(
                "SELECT set_config('cosmaneura.practice_id', %s, false)",
                (practice_a,),
            )
            updated_versions = dict(owner.execute(
                "SELECT external_id, version FROM public.encounters"
            ).fetchall())
            original_external_id = _external_id(original)
            assert updated_versions[original_external_id] == (
                original_versions[original_external_id] + 1
            )
            untouched_id = _external_id(page.records[1])
            assert updated_versions[untouched_id] == original_versions[untouched_id]

        def fail_before_commit(_connection) -> None:
            raise RuntimeError("synthetic page transaction failure")

        failed_record = replace(
            changed,
            source_version="source-version-3",
            idempotency_key=source_import_idempotency_key(
                practice_id=practice_a,
                source_system=SOURCE_SYSTEM,
                feed="encounters",
                source_record_id=changed.source_record_id,
                source_version="source-version-3",
                mapping_version=changed.mapping_version,
            ),
            visit=replace(
                changed.visit,
                signed_note=changed.visit.signed_note + " Must roll back.",
            ),
        )
        failing_store = PostgresSourceImportStore(
            import_dsn,
            practice_id=practice_a,
            transaction_check=fail_before_commit,
        )
        with pytest.raises(RuntimeError, match="synthetic page transaction failure"):
            failing_store.import_encounter_page(
                source_system=SOURCE_SYSTEM,
                feed="encounters",
                provenance=PROVENANCE,
                mapping_version=failed_record.mapping_version,
                records=(failed_record,),
                checkpoint="must-not-commit",
                next_cursor="must-not-commit",
                expected_checkpoint_version=updated_checkpoint.version,
            )
        assert store.load_checkpoint(
            source_system=SOURCE_SYSTEM, feed="encounters",
        ) == updated_checkpoint

        with psycopg.connect(TEST_DSN) as owner:
            owner.execute(
                "SELECT set_config('cosmaneura.practice_id', %s, false)",
                (practice_a,),
            )
            imported_uid, stored_source, stored_note, stored_version = (
                owner.execute(
                    """SELECT encounter_uid, source, signed_note, version
                         FROM public.encounters WHERE external_id = %s""",
                    (_external_id(original),),
                ).fetchone()
            )
            assert stored_source == PROVENANCE
            assert imported_uid is not None
            assert stored_note == changed.visit.signed_note
            assert stored_version == updated_versions[_external_id(original)]
            assert owner.execute(
                """SELECT count(*) FROM public.source_import_receipts
                    WHERE practice_id = %s""",
                (practice_a,),
            ).fetchone()[0] == 3

        # The credential has no direct data-table path: the reviewed atomic
        # function is its only mutation capability.
        with psycopg.connect(import_dsn) as importer:
            importer.execute(
                "SELECT set_config('cosmaneura.practice_id', %s, false)",
                (practice_a,),
            )
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with importer.transaction(force_rollback=True):
                    importer.execute(
                        "SELECT external_id FROM public.encounters"
                    )
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with importer.transaction(force_rollback=True):
                    _insert_encounter(
                        importer,
                        practice_id=practice_a,
                        external_id=f"forged-{suffix}",
                        source="manual",
                    )
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with importer.transaction(force_rollback=True):
                    importer.execute(
                        """UPDATE public.encounters
                              SET signed_note = 'Forbidden direct update.'
                            WHERE external_id = %s""",
                        (_external_id(original),),
                    )
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with importer.transaction(force_rollback=True):
                    importer.execute(
                        "DELETE FROM public.encounters WHERE external_id = %s",
                        (_external_id(original),),
                    )
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with importer.transaction(force_rollback=True):
                    importer.execute(
                        "SELECT * FROM public.source_import_receipts"
                    )
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with importer.transaction(force_rollback=True):
                    importer.execute(
                        """INSERT INTO public.source_import_receipts (
                               practice_id, source_system, feed,
                               idempotency_key, mapping_version, payload_sha256
                           ) VALUES (%s, %s, 'coverage.eligibility', %s, %s, %s)""",
                        (
                            practice_a,
                            SOURCE_SYSTEM,
                            "source-import-v1:" + "f" * 64,
                            "mcd-normalizer-v1",
                            "a" * 64,
                        ),
                    )
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with importer.transaction(force_rollback=True):
                    importer.execute(
                        """UPDATE public.source_import_checkpoints
                              SET checkpoint = 'forged'
                            WHERE practice_id = %s""",
                        (practice_a,),
                    )
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with importer.transaction(force_rollback=True):
                    importer.execute(
                        "SELECT * FROM public.practice_memberships"
                    )
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with importer.transaction(force_rollback=True):
                    importer.execute(
                        "SELECT * FROM public.source_import_role_bindings"
                    )

            record = page.records[0]
            malformed_record = {
                "target_external_id": "raw-patient-identifier",
                "idempotency_key": record.idempotency_key,
                "mapping_version": record.mapping_version,
                "payload_sha256": "a" * 64,
                "patient_age": record.visit.patient_age,
                "patient_sex": record.visit.patient_sex,
                "encounter_type": record.visit.encounter_type,
                "place_of_service": record.visit.place_of_service,
                "intake_text": record.visit.intake_text,
                "transcript": record.visit.transcript,
                "signed_note": record.visit.signed_note,
                "provider_specialty": record.visit.provider_specialty,
                "submitted_em_code": record.visit.submitted_em_code,
                "submitted_icd10": record.visit.submitted_icd10,
            }
            with pytest.raises(
                psycopg.errors.CheckViolation,
                match="source-import record mapping is invalid",
            ):
                with importer.transaction(force_rollback=True):
                    importer.execute(
                        """SELECT * FROM
                               public.cosmaneura_commit_source_encounter_page(
                                   %s, %s, 'encounters', %s, %s, %s,
                                   %s::jsonb, 'forged', NULL
                               )""",
                        (
                            practice_a,
                            SOURCE_SYSTEM,
                            PROVENANCE,
                            record.mapping_version,
                            updated_checkpoint.version,
                            psycopg.types.json.Jsonb([malformed_record]),
                        ),
                    )

            importer.execute(
                "SELECT set_config('cosmaneura.practice_id', %s, false)",
                (practice_b,),
            )
            assert importer.execute(
                "SELECT count(*) FROM public.source_import_checkpoints"
            ).fetchone()[0] == 0

        ordinary_dsn = make_conninfo(
            TEST_DSN, user=ordinary_role, password=ordinary_password,
        )
        with psycopg.connect(ordinary_dsn) as ordinary:
            ordinary.execute(
                "SELECT set_config('cosmaneura.practice_id', %s, false)",
                (practice_a,),
            )
            assert ordinary.execute(
                "SELECT source FROM public.encounters WHERE external_id = %s",
                (_external_id(original),),
            ).fetchone()[0] == PROVENANCE
            with pytest.raises(
                psycopg.errors.CheckViolation,
                match="reserved source-import provenance requires its importer",
            ):
                with ordinary.transaction(force_rollback=True):
                    ordinary.execute(
                        """UPDATE public.encounters
                              SET signed_note = 'Forbidden API update.'
                            WHERE external_id = %s""",
                        (_external_id(original),),
                    )
            with pytest.raises(
                psycopg.errors.CheckViolation,
                match="reserved source-import provenance requires its importer",
            ):
                with ordinary.transaction(force_rollback=True):
                    _insert_encounter(
                        ordinary,
                        practice_id=practice_a,
                        external_id=f"forged-api-{suffix}",
                        source=PROVENANCE,
                    )

        # Losing the registration cannot turn an importer with residual table
        # grants into an ordinary practice-wide encounter reader.
        with psycopg.connect(TEST_DSN) as owner:
            owner.execute(
                "DELETE FROM public.source_import_receipts WHERE practice_id = %s",
                (practice_a,),
            )
            owner.execute(
                "DELETE FROM public.source_import_checkpoints WHERE practice_id = %s",
                (practice_a,),
            )
            with pytest.raises(
                psycopg.errors.CheckViolation,
                match="source-import binding has reserved encounters",
            ):
                with owner.transaction(force_rollback=True):
                    owner.execute(
                        """DELETE FROM public.source_import_role_bindings
                            WHERE role_name = %s""",
                        (import_role,),
                    )
        # Simulate accidental owner-level trigger removal to prove the
        # restrictive encounter policy independently remains fail-closed.
        with psycopg.connect(TEST_DSN) as owner:
            owner.execute(
                """ALTER TABLE public.source_import_role_bindings
                    DISABLE TRIGGER source_import_role_bindings_guard"""
            )
            owner.execute(
                "DELETE FROM public.source_import_role_bindings WHERE role_name = %s",
                (import_role,),
            )
            owner.execute(
                """ALTER TABLE public.source_import_role_bindings
                    ENABLE ALWAYS TRIGGER source_import_role_bindings_guard"""
            )
            owner.execute(sql.SQL(
                "GRANT SELECT, UPDATE ON public.encounters TO {}"
            ).format(sql.Identifier(import_role)))
        with psycopg.connect(import_dsn) as importer:
            importer.execute(
                "SELECT set_config('cosmaneura.practice_id', %s, false)",
                (practice_a,),
            )
            assert importer.execute(
                "SELECT count(*) FROM public.encounters"
            ).fetchone()[0] == 0
            assert importer.execute(
                """UPDATE public.encounters SET signed_note = 'must not update'
                    WHERE practice_id = %s""",
                (practice_a,),
            ).rowcount == 0
    finally:
        with psycopg.connect(TEST_DSN, autocommit=True) as owner:
            owner.execute(
                "SELECT set_config('cosmaneura.practice_id', %s, false)",
                (practice_a,),
            )
            owner.execute(
                "DELETE FROM public.source_import_receipts WHERE practice_id = %s",
                (practice_a,),
            )
            owner.execute(
                "DELETE FROM public.source_import_checkpoints WHERE practice_id = %s",
                (practice_a,),
            )
            owner.execute(
                "DELETE FROM public.encounters WHERE practice_id = %s",
                (practice_a,),
            )
            owner.execute(
                "DELETE FROM public.source_import_role_bindings WHERE role_name = %s",
                (import_role,),
            )
            for role in (import_role, ordinary_role):
                owner.execute(
                    "DELETE FROM public.runtime_practice_bindings WHERE role_name = %s",
                    (role,),
                )
                _drop_role(owner, role)
            for practice_id in (practice_a, practice_b):
                owner.execute(
                    "SELECT set_config('cosmaneura.practice_id', %s, false)",
                    (practice_id,),
                )
                owner.execute(
                    "DELETE FROM public.practices WHERE practice_id = %s",
                    (practice_id,),
                )


def test_z_source_import_down_fails_with_rows_then_retires_role_and_reapplies():
    suffix = uuid4().hex
    practice_id = f"source-import-down-{suffix}"
    role = f"athena_down_{suffix[:20]}"
    orphan_role = f"athena_orphan_{suffix[:18]}"
    reporting_role = f"reporting_{suffix[:20]}"
    password = f"Synthetic-down-{suffix}"
    migrations = ROOT / "migrations"
    up_sql = (migrations / "009_source_import_foundation.up.sql").read_text(
        encoding="utf-8",
    )
    down_sql = (migrations / "009_source_import_foundation.down.sql").read_text(
        encoding="utf-8",
    )
    page = MockAthenaClient(server_practice_id=practice_id).read_page()

    try:
        with psycopg.connect(TEST_DSN) as owner:
            owner.execute(
                """INSERT INTO public.practices (
                       practice_id, entra_tenant_id, display_name
                   ) VALUES (%s, %s::uuid, 'Synthetic down test')""",
                (practice_id, str(uuid4())),
            )
            _provision_athena_import_role(
                owner,
                Settings(
                    database_url=TEST_DSN,
                    practice_id=practice_id,
                    athena_import_database_role=role,
                    athena_import_database_password=password,
                ),
            )
            sql_module, orphan_identifier = _prepare_bound_login(
                owner,
                role=orphan_role,
                password_value=f"Synthetic-orphan-{suffix}",
                practice_id=practice_id,
                description="synthetic orphan import role",
            )
            owner.execute(sql_module.SQL(
                "GRANT SELECT ON public.source_import_receipts TO {}"
            ).format(orphan_identifier))
            owner.execute(sql_module.SQL(
                "GRANT SELECT ON public.encounters TO {}"
            ).format(orphan_identifier))
            sql_module, reporting_identifier = _prepare_bound_login(
                owner,
                role=reporting_role,
                password_value=f"Synthetic-reporting-{suffix}",
                practice_id=practice_id,
                description="synthetic encounter reporting role",
            )
            owner.execute(sql_module.SQL(
                "GRANT SELECT (external_id) ON public.encounters TO {}"
            ).format(reporting_identifier))
        import_dsn = make_conninfo(TEST_DSN, user=role, password=password)
        PostgresSourceImportStore(import_dsn, practice_id=practice_id).import_encounter_page(
            source_system=SOURCE_SYSTEM,
            feed="encounters",
            provenance=PROVENANCE,
            mapping_version=page.records[0].mapping_version,
            records=(page.records[0],),
            checkpoint=page.checkpoint,
            next_cursor=page.next_cursor,
        )

        with psycopg.connect(TEST_DSN, autocommit=True) as owner:
            with pytest.raises(
                psycopg.errors.CheckViolation,
                match="source-import binding has reserved encounters",
            ):
                owner.execute(down_sql, prepare=False)
            owner.execute("ROLLBACK")
            assert _relation_exists(owner, "source_import_receipts")

            owner.execute(
                "SELECT set_config('cosmaneura.practice_id', %s, false)",
                (practice_id,),
            )
            owner.execute(
                "DELETE FROM public.source_import_receipts WHERE practice_id = %s",
                (practice_id,),
            )
            owner.execute(
                "DELETE FROM public.source_import_checkpoints WHERE practice_id = %s",
                (practice_id,),
            )
            owner.execute(
                "DELETE FROM public.encounters WHERE practice_id = %s",
                (practice_id,),
            )
            owner.execute(down_sql, prepare=False)
            assert not _relation_exists(owner, "source_import_receipts")
            for retired_role in (role, orphan_role):
                assert owner.execute(
                    """SELECT rolcanlogin FROM pg_catalog.pg_roles
                        WHERE rolname = %s""",
                    (retired_role,),
                ).fetchone()[0] is False
                assert owner.execute(
                    """SELECT has_table_privilege(
                               %s, 'public.encounters', 'SELECT'
                           )""",
                    (retired_role,),
                ).fetchone()[0] is False
            assert owner.execute(
                """SELECT rolcanlogin FROM pg_catalog.pg_roles
                    WHERE rolname = %s""",
                (reporting_role,),
            ).fetchone()[0] is True
            assert owner.execute(
                """SELECT has_column_privilege(
                           %s, 'public.encounters', 'external_id', 'SELECT'
                       )""",
                (reporting_role,),
            ).fetchone()[0] is True
            assert owner.execute(
                """SELECT practice_id FROM public.runtime_practice_bindings
                    WHERE role_name = %s""",
                (reporting_role,),
            ).fetchone()[0] == practice_id
            owner.execute(up_sql, prepare=False)
            assert _relation_exists(owner, "source_import_receipts")
    finally:
        with psycopg.connect(TEST_DSN, autocommit=True) as owner:
            if not _relation_exists(owner, "source_import_receipts"):
                owner.execute(up_sql, prepare=False)
            for retired_role in (role, orphan_role, reporting_role):
                owner.execute(
                    """DELETE FROM public.runtime_practice_bindings
                        WHERE role_name = %s""",
                    (retired_role,),
                )
                _drop_role(owner, retired_role)
            owner.execute(
                "SELECT set_config('cosmaneura.practice_id', %s, false)",
                (practice_id,),
            )
            owner.execute(
                "DELETE FROM public.practices WHERE practice_id = %s",
                (practice_id,),
            )
