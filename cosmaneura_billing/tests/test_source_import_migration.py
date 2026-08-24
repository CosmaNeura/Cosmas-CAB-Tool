"""Static release gates for the shared source-import database substrate."""
from __future__ import annotations

from pathlib import Path

from cosmaneura_billing.migrations import migration_body


ROOT = Path(__file__).resolve().parents[1]
UP = (ROOT / "migrations" / "009_source_import_foundation.up.sql").read_text(
    encoding="utf-8"
)
DOWN = (ROOT / "migrations" / "009_source_import_foundation.down.sql").read_text(
    encoding="utf-8"
)
PROVISIONER = (ROOT / "cosmaneura_billing" / "migrations.py").read_text(
    encoding="utf-8"
)


def _normalized(value: str) -> str:
    return " ".join(migration_body(value).split())


def test_tables_are_source_agnostic_and_keys_capture_replay_identity():
    normalized = _normalized(UP)
    assert "CREATE TABLE public.source_import_checkpoints" in normalized
    assert "CREATE TABLE public.source_import_receipts" in normalized
    assert "CREATE TABLE public.source_import_role_bindings" in normalized
    assert "CREATE TABLE public.athena" not in normalized
    assert "UNIQUE (practice_id, source_system)" in normalized
    assert "UNIQUE (practice_id, provenance)" in normalized
    assert (
        "PRIMARY KEY ( practice_id, source_system, feed, idempotency_key )"
        in normalized
    )
    assert "payload_sha256 ~ '^[0-9a-f]{64}$'" in normalized
    assert (
        "idempotency_key ~ '^[A-Za-z0-9][A-Za-z0-9:._-]{0,255}$'"
        in normalized
    )
    assert "checkpoint text" in normalized
    assert "next_cursor text" in normalized
    assert "cursor_history text[]" in normalized
    assert "cardinality(cursor_history) <= 100" in normalized
    assert "octet_length(array_to_string(cursor_history, '')) <= 51200" in normalized
    assert "source-import cursor cycle is not allowed" in normalized
    assert "encounter_uid uuid," in normalized
    assert "mapping_version text NOT NULL" in normalized
    assert "source_import_receipts_mapping_version_valid" in normalized
    assert (
        "feed <> 'encounters' OR encounter_uid IS NOT NULL"
        in normalized
    )


def test_exact_athena_provenance_is_constrained_and_server_guarded():
    normalized = _normalized(UP)
    assert "'athena_preview_synthetic'" in normalized
    assert "encounters_source_valid CHECK" in normalized
    assert "source-import provenance is server-owned" in normalized
    assert "reserved source-import provenance requires its importer" in normalized
    assert "NEW.source := binding.provenance" in normalized
    assert "NEW.provenance := binding.provenance" in normalized
    assert "encounters_uid_source_unique" in normalized
    assert "source_import_receipts_encounter_fk" in normalized


def test_import_state_is_forced_rls_and_encounters_have_restrictive_policy():
    normalized = _normalized(UP)
    for table in ("source_import_checkpoints", "source_import_receipts"):
        assert f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY" in normalized
        assert f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY" in normalized
    assert "CREATE POLICY encounters_source_import_restriction" in normalized
    assert "AS RESTRICTIVE" in normalized
    assert "cosmaneura_source_import_session()" in normalized
    assert "has_table_privilege" in normalized
    assert "REVOKE ALL ON public.source_import_role_bindings FROM PUBLIC" in normalized
    assert "source_import_role_bindings_guard" in normalized
    assert "source-import binding has reserved encounters" in normalized


def test_atomic_function_is_the_importers_only_write_capability():
    normalized = _normalized(UP)
    assert (
        "CREATE FUNCTION public.cosmaneura_commit_source_encounter_page"
        in normalized
    )
    assert "SECURITY DEFINER" in normalized
    assert "SET search_path = pg_catalog, public" not in normalized
    assert "pg_advisory_xact_lock" in normalized
    assert "expected_checkpoint_version" in normalized
    assert "candidate_feed IS DISTINCT FROM 'encounters'" in normalized
    assert "source-import encounter feed is invalid" in normalized
    assert "octet_length(candidate_records::text) > 16777216" in normalized
    assert "source-encounter-v1:[0-9a-f]{64}" in normalized
    assert "source-import-v1:[0-9a-f]{64}" in normalized
    assert (
        "REVOKE ALL ON FUNCTION "
        "public.cosmaneura_commit_source_encounter_page"
    ) in normalized

    provisioner = PROVISIONER.split(
        "def _provision_athena_import_role", 1
    )[1].split("def _bootstrap_practice_admin", 1)[0]
    assert "GRANT SELECT ON public.source_import_checkpoints" in provisioner
    assert "cosmaneura_commit_source_encounter_page(" in provisioner
    assert "GRANT INSERT" not in provisioner
    assert "GRANT UPDATE" not in provisioner
    assert "GRANT SELECT ON encounters" not in provisioner


def test_receipts_are_append_only_and_checkpoint_identity_is_immutable():
    normalized = _normalized(UP)
    assert "source-import receipts are append-only" in normalized
    assert "source-import checkpoint identity is immutable" in normalized
    assert (
        "ROW(NEW.checkpoint, NEW.next_cursor) IS DISTINCT FROM "
        "ROW(OLD.checkpoint, OLD.next_cursor)"
        in normalized
    )
    assert "position_changed OR NEW.version = OLD.version + 1" in normalized
    assert "CASE WHEN imported_total > 0 THEN 1 ELSE 0 END" in normalized
    for trigger in (
        "encounters_source_import_guard",
        "encounters_z_source_import_truncate_guard",
        "source_import_checkpoints_guard",
        "source_import_checkpoints_truncate_guard",
        "source_import_receipts_guard",
        "source_import_receipts_truncate_guard",
        "source_import_role_bindings_guard",
        "source_import_role_bindings_truncate_guard",
    ):
        assert f"ENABLE ALWAYS TRIGGER {trigger}" in normalized


def test_down_is_explicit_atomic_and_restores_pre_import_source_allowlist():
    normalized = _normalized(DOWN)
    assert "close every importer" in DOWN.lower()
    assert "REVOKE ALL PRIVILEGES ON TABLE public.encounters" in normalized
    assert "source_import_roles_to_revoke" in normalized
    assert "aclexplode(relation.relacl)" in normalized
    assert "aclexplode(attribute.attacl)" in normalized
    assert "aclexplode(procedure.proacl)" in normalized
    assert "role_row.rolname !~ '^pg_'" in normalized
    assert "ALTER ROLE %I NOLOGIN" in normalized
    assert "DELETE FROM public.runtime_practice_bindings" in normalized
    assert "DROP TABLE IF EXISTS public.source_import_receipts" in normalized
    assert "DROP TABLE IF EXISTS public.source_import_checkpoints" in normalized
    assert "DROP TABLE IF EXISTS public.source_import_role_bindings" in normalized
    assert "DROP FUNCTION IF EXISTS public.cosmaneura_commit_source_encounter_page" in normalized
    assert "'athena_preview_synthetic'" not in normalized
    for sql in (UP, DOWN):
        body = migration_body(sql)
        assert "BEGIN;" not in body
        assert "COMMIT;" not in body
