"""Migration discovery and deployment-runner safety checks."""
from pathlib import Path

import pytest

from cosmaneura_billing.migrations import (
    discover_migrations, migration_body, migration_directory,
)
from cosmaneura_billing.settings import Settings


def test_checked_in_up_migrations_are_ordered_and_down_files_are_excluded():
    paths = discover_migrations(migration_directory(Settings()))
    assert [path.name for path in paths] == [
        "001_init_pgvector.up.sql",
        "002_chase_persistence.up.sql",
        "003_pilot_persistence.up.sql",
        "004_chase_delivery_runtime.up.sql",
        "005_accepted_delivery_attempt.up.sql",
        "006_chase_policy_revocation.up.sql",
        "007_synthetic_demo_release_candidate_source.up.sql",
        "008_synthetic_demo_encounter_immutability.up.sql",
        "009_source_import_foundation.up.sql",
        "010_chase_communications.up.sql",
        "011_billing_headers.up.sql",
        "012_chase_communications_dispatch.up.sql",
        "013_billing_header_signoffs.up.sql",
        "014_athena_staff_review.up.sql",
        "015_chase_communications_cancellation.up.sql",
        "016_chase_communications_unknown_resolution.up.sql",
        "017_chase_communications_event_review.up.sql",
        "018_chase_unassigned_sms_review.up.sql",
        "019_provider_note_handoff.up.sql",
        "020_provider_source_admission.up.sql",
        "021_athena_staff_bindings.up.sql",
        "022_athena_configured_review.up.sql",
        "023_athena_read_admission.up.sql",
    ]


def test_explicit_missing_migration_directory_fails_closed(tmp_path):
    settings = Settings(migrations_dir=str(tmp_path / "missing"))
    with pytest.raises(RuntimeError, match="migration directory is unavailable"):
        discover_migrations(migration_directory(settings))


def test_runner_owns_transaction_without_weakening_manual_sql_files():
    sql = "-- comment\n\nBEGIN;\nCREATE TABLE example (id int);\nCOMMIT;\n"

    body = migration_body(sql)

    assert "CREATE TABLE example" in body
    assert "BEGIN;" not in body
    assert "COMMIT;" not in body


def test_nested_or_partial_transaction_control_is_rejected():
    with pytest.raises(RuntimeError, match="outer BEGIN/COMMIT"):
        migration_body("CREATE TABLE example (id int);\nCOMMIT;")
