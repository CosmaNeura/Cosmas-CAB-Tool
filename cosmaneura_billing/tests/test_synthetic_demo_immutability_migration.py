"""Static release gates for PostgreSQL synthetic-demo immutability."""
from __future__ import annotations

import re
from pathlib import Path

from cosmaneura_billing.migrations import migration_body


ROOT = Path(__file__).resolve().parents[1]
UP_SQL = (
    ROOT / "migrations" / "008_synthetic_demo_encounter_immutability.up.sql"
).read_text(encoding="utf-8")
DOWN_SQL = (
    ROOT / "migrations" / "008_synthetic_demo_encounter_immutability.down.sql"
).read_text(encoding="utf-8")
FUNCTION = "public.cosmaneura_guard_synthetic_demo_encounter()"
TRIGGER = "encounters_synthetic_demo_immutability"
TRUNCATE_TRIGGER = "encounters_synthetic_demo_truncate_immutability"
RESERVED_SOURCE = "synthetic_demo_release_candidate"


def _normalized(sql: str) -> str:
    return " ".join(migration_body(sql).split())


def test_up_uses_invoker_identity_and_exact_table_owner_from_pg_catalog():
    normalized = _normalized(UP_SQL)
    assert f"CREATE OR REPLACE FUNCTION {FUNCTION}" in normalized
    assert "SECURITY INVOKER" in normalized
    assert "SECURITY DEFINER" not in normalized
    assert "SET search_path = pg_catalog" in normalized
    assert "FROM pg_catalog.pg_class AS relation" in normalized
    assert "pg_catalog.pg_get_userbyid(relation.relowner)" in normalized
    assert "relation.oid = TG_RELID" in normalized
    assert "current_user IS DISTINCT FROM table_owner" in normalized
    assert "session_user" not in normalized


def test_up_blocks_every_reserved_source_mutation_for_nonowners():
    normalized = _normalized(UP_SQL)
    assert RESERVED_SOURCE in normalized
    assert "TG_OP = 'INSERT'" in normalized
    assert "NEW.source IS DISTINCT FROM reserved_source" in normalized
    assert "TG_OP = 'UPDATE'" in normalized
    assert "OLD.source IS DISTINCT FROM reserved_source" in normalized
    assert "AND NEW.source IS DISTINCT FROM reserved_source" in normalized
    assert "TG_OP = 'DELETE'" in normalized
    assert "TG_OP = 'TRUNCATE'" in normalized
    assert "reserved synthetic demo encounters require the table owner" in normalized
    assert "ERRCODE = 'check_violation'" in normalized
    assert (
        "BEFORE INSERT OR UPDATE OR DELETE ON public.encounters" in normalized
    )
    assert f"EXECUTE FUNCTION {FUNCTION}" in normalized
    assert "BEFORE TRUNCATE ON public.encounters" in normalized
    assert "FOR EACH STATEMENT" in normalized


def test_up_is_idempotent_always_enabled_and_not_callable_by_public():
    normalized = _normalized(UP_SQL)
    for trigger in (TRIGGER, TRUNCATE_TRIGGER):
        drop_position = normalized.index(f"DROP TRIGGER IF EXISTS {trigger}")
        create_position = normalized.index(f"CREATE TRIGGER {trigger}")
        assert drop_position < create_position
        assert (
            f"ALTER TABLE public.encounters ENABLE ALWAYS TRIGGER {trigger}"
            in normalized
        )
    assert f"REVOKE ALL ON FUNCTION {FUNCTION} FROM PUBLIC" in normalized


def test_migration_does_not_touch_rows_rls_or_table_grants():
    for sql in (UP_SQL, DOWN_SQL):
        body = migration_body(sql)
        assert not re.search(r"\b(?:INSERT\s+INTO|DELETE\s+FROM)\b", body, re.I)
        assert not re.search(r"\bUPDATE\s+(?:public\.)?encounters\b", body, re.I)
        assert "CREATE POLICY" not in body.upper()
        assert "DROP POLICY" not in body.upper()
        assert "ROW LEVEL SECURITY" not in body.upper()
        assert not re.search(r"\bGRANT\b", body, re.I)
    assert [
        " ".join(statement.split()).removesuffix(";")
        for statement in re.findall(
            r"\bALTER\s+TABLE\b[^;]*;",
            migration_body(UP_SQL),
            re.I | re.S,
        )
    ] == [
        f"ALTER TABLE public.encounters ENABLE ALWAYS TRIGGER {TRIGGER}",
        f"ALTER TABLE public.encounters ENABLE ALWAYS TRIGGER {TRUNCATE_TRIGGER}",
    ]
    assert not re.search(r"\bALTER\s+TABLE\b", migration_body(DOWN_SQL), re.I)


def test_down_removes_only_the_trigger_and_function_idempotently():
    normalized = _normalized(DOWN_SQL)
    assert f"DROP TRIGGER IF EXISTS {TRIGGER} ON public.encounters" in normalized
    assert (
        f"DROP TRIGGER IF EXISTS {TRUNCATE_TRIGGER} ON public.encounters"
        in normalized
    )
    assert f"DROP FUNCTION IF EXISTS {FUNCTION}" in normalized
    assert RESERVED_SOURCE not in normalized


def test_down_documents_the_forced_rls_rollback_precondition():
    assert "close API/worker/sync ingress" in DOWN_SQL
    assert "every bound practice" in DOWN_SQL
    assert "Keep ingress closed through rollback of 007" in DOWN_SQL


def test_both_directions_are_runner_owned_atomic_migrations():
    for sql in (UP_SQL, DOWN_SQL):
        body = migration_body(sql)
        assert "BEGIN;" not in body
        assert "COMMIT;" not in body
