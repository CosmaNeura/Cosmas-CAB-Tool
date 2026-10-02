"""Small, locked SQL migration runner for deployment jobs.

Migrations stay as reviewable SQL files in the repository.  This module only
orders them, records which files committed successfully, and serializes runners
with a PostgreSQL advisory lock.  It never prints a DSN or SQL parameter.
"""
from __future__ import annotations

import re
import hashlib
from pathlib import Path
from typing import Callable

from .settings import Settings

_UP_MIGRATION = re.compile(r"^[0-9]{3}_[a-z0-9_]+\.up\.sql$")
_MIGRATION_LOCK = 4_836_623_987_031_721_441


def _checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _membership_rows_are_unsafe(
    rows,
    *,
    role_oid: int,
    current_user_oid: int,
) -> bool:
    """Reject authority edges except PostgreSQL 16's creator-admin tuple.

    PostgreSQL 16 gives a non-superuser ``CREATEROLE`` principal one inbound
    ADMIN membership on every role it creates.  That edge is required for the
    migration owner to rotate the login later, but it cannot inherit or
    ``SET ROLE`` to the runtime login.  Every other inbound edge, every edge
    where the runtime login is itself a member, and duplicate creator edges
    fail closed.
    """
    safe_creator_edges = 0
    for granted_role_oid, member_oid, admin, inherit, can_set in rows:
        if member_oid == role_oid:
            return True
        if (
            granted_role_oid != role_oid
            or member_oid != current_user_oid
            or not bool(admin)
            or bool(inherit)
            or bool(can_set)
        ):
            return True
        safe_creator_edges += 1
    return safe_creator_edges > 1


def _role_has_unsafe_membership(
    conn,
    *,
    role_oid: int,
    current_user: str,
) -> bool:
    current_user_row = conn.execute(
        "SELECT oid FROM pg_roles WHERE rolname = %s",
        (current_user,),
    ).fetchone()
    if current_user_row is None:
        return True
    rows = conn.execute(
        """SELECT roleid, member, admin_option, inherit_option, set_option
             FROM pg_auth_members
            WHERE member = %s OR roleid = %s""",
        (role_oid, role_oid),
    ).fetchall()
    return _membership_rows_are_unsafe(
        rows,
        role_oid=role_oid,
        current_user_oid=current_user_row[0],
    )


def _prepare_bound_login(
    conn,
    *,
    role: str,
    password_value: str,
    practice_id: str,
    description: str,
):
    """Create/rotate one unprivileged login and reset all inherited grants."""
    try:
        from psycopg import sql
    except ImportError as exc:  # pragma: no cover - deployment wiring
        raise RuntimeError(f"{description} provisioning requires psycopg") from exc
    if not practice_id:
        raise RuntimeError(f"{description} provisioning requires a practice ID")
    current_user = conn.execute("SELECT current_user").fetchone()[0]
    blocked_names = {"postgres", "azure_pg_admin"}
    if (
        role.casefold() in blocked_names
        or role.casefold().startswith("pg_")
        or role.casefold() == str(current_user).casefold()
    ):
        raise RuntimeError(
            f"{description} must be distinct from database administrators"
        )
    identifier = sql.Identifier(role)
    password = sql.Literal(password_value)
    existing = conn.execute(
        """SELECT oid, rolsuper, rolcreatedb, rolcreaterole,
                  rolreplication, rolbypassrls
             FROM pg_roles WHERE rolname = %s""",
        (role,),
    ).fetchone()
    if existing and any(bool(value) for value in existing[1:]):
        raise RuntimeError(f"configured {description} is already privileged")
    if existing:
        unsafe_membership = _role_has_unsafe_membership(
            conn,
            role_oid=existing[0],
            current_user=str(current_user),
        )
        ownership = conn.execute(
            """SELECT 1
                 FROM pg_class c
                 JOIN pg_roles r ON r.oid = c.relowner
                WHERE r.rolname = %s
                UNION ALL
               SELECT 1
                 FROM pg_namespace n
                 JOIN pg_roles r ON r.oid = n.nspowner
                WHERE r.rolname = %s
                UNION ALL
               SELECT 1
                 FROM pg_database d
                 JOIN pg_roles r ON r.oid = d.datdba
                WHERE r.rolname = %s
                UNION ALL
               SELECT 1
                 FROM pg_proc p
                 JOIN pg_roles r ON r.oid = p.proowner
                WHERE r.rolname = %s
                LIMIT 1""",
            (role, role, role, role),
        ).fetchone()
        if unsafe_membership or ownership:
            raise RuntimeError(
                f"configured {description} has inherited authority or owns objects"
            )
        conn.execute(sql.SQL(
            "ALTER ROLE {} LOGIN PASSWORD {} NOCREATEROLE NOINHERIT"
        ).format(identifier, password))
    else:
        conn.execute(sql.SQL(
            "CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB "
            "NOCREATEROLE NOINHERIT NOBYPASSRLS"
        ).format(identifier, password))
    final_role = conn.execute(
        """SELECT oid, rolcanlogin, rolsuper, rolcreatedb, rolcreaterole,
                  rolinherit, rolreplication, rolbypassrls
             FROM pg_roles WHERE rolname = %s""",
        (role,),
    ).fetchone()
    expected_flags = (True, False, False, False, False, False, False)
    if (
        final_role is None
        or tuple(bool(value) for value in final_role[1:]) != expected_flags
    ):
        raise RuntimeError(f"configured {description} did not remain unprivileged")
    if _role_has_unsafe_membership(
        conn,
        role_oid=final_role[0],
        current_user=str(current_user),
    ):
        raise RuntimeError(f"configured {description} has unsafe role membership")
    database_name = conn.execute("SELECT current_database()").fetchone()[0]
    conn.execute(sql.SQL("REVOKE ALL PRIVILEGES ON DATABASE {} FROM {}").format(
        sql.Identifier(database_name), identifier,
    ))
    conn.execute(sql.SQL("REVOKE ALL PRIVILEGES ON SCHEMA public FROM {}").format(
        identifier,
    ))
    conn.execute(sql.SQL(
        "REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM {}"
    ).format(identifier))
    conn.execute(sql.SQL(
        "REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM {}"
    ).format(identifier))
    conn.execute(sql.SQL(
        "REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA public FROM {}"
    ).format(identifier))
    conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
        sql.Identifier(database_name), identifier,
    ))
    conn.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(identifier))
    existing_binding = conn.execute(
        "SELECT practice_id FROM runtime_practice_bindings WHERE role_name = %s",
        (role,),
    ).fetchone()
    if existing_binding and existing_binding[0] != practice_id:
        raise RuntimeError(f"{description} is already bound to another practice")
    conn.execute(
        """INSERT INTO runtime_practice_bindings (role_name, practice_id)
           VALUES (%s, %s) ON CONFLICT (role_name) DO NOTHING""",
        (role, practice_id),
    )
    return sql, identifier


def _provision_runtime_role(conn, settings: Settings) -> None:
    """Create/rotate the least-privilege API login after schema changes."""
    if not settings.database_runtime_password:
        return
    sql, identifier = _prepare_bound_login(
        conn,
        role=settings.database_runtime_role,
        password_value=settings.database_runtime_password,
        practice_id=settings.practice_id,
        description="runtime role",
    )
    conn.execute(sql.SQL(
        "GRANT SELECT ON practices, users, practice_memberships TO {}"
    ).format(identifier))
    conn.execute(sql.SQL(
        "GRANT SELECT, INSERT, UPDATE ON encounters TO {}"
    ).format(identifier))
    conn.execute(sql.SQL(
        "GRANT SELECT, INSERT ON coder_feedback, coder_feedback_decisions TO {}"
    ).format(identifier))
    conn.execute(sql.SQL(
        "GRANT INSERT ON access_audit_attempts, access_audit_completions TO {}"
    ).format(identifier))
    conn.execute(sql.SQL(
        "GRANT SELECT, INSERT, UPDATE ON chase_items TO {}"
    ).format(identifier))
    conn.execute(sql.SQL(
        "GRANT SELECT, INSERT ON chase_attempts, chase_action_receipts TO {}"
    ).format(identifier))
    conn.execute(sql.SQL(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON chase_pending_tasks, "
        "chase_resolutions TO {}"
    ).format(identifier))
    conn.execute(sql.SQL(
        "GRANT USAGE, SELECT ON SEQUENCE coder_feedback_feedback_id_seq TO {}"
    ).format(identifier))
    conn.execute(sql.SQL(
        "GRANT EXECUTE ON FUNCTION cosmaneura_practice_allowed(text), "
        "cosmaneura_feedback_reviewer_allowed(uuid, uuid), "
        "cosmaneura_staff_assignee_allowed(uuid, uuid), "
        "cosmaneura_chase_admin_allowed(uuid, uuid), "
        "cosmaneura_cancel_revoked_chase_policy(uuid, text), "
        "cosmaneura_cancel_stale_chase_work(text) TO {}"
    ).format(identifier))
    # Authenticated staff create immutable inputs through the API. They may
    # advance/delete an existing cadence when completing a logged phone task,
    # but cannot create cadence or access outbox/delivery transport effects.
    conn.execute(sql.SQL(
        "GRANT SELECT, INSERT ON chase_contact_snapshots, "
        "chase_contact_routes, chase_policy_revisions, chase_policy_steps, "
        "chase_policy_approval_events, chase_message_releases, "
        "chase_review_releases TO {}"
    ).format(identifier))
    conn.execute(sql.SQL(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON chase_active_policies TO {}"
    ).format(identifier))
    conn.execute(sql.SQL(
        "GRANT SELECT, UPDATE, DELETE ON chase_cadence_states TO {}"
    ).format(identifier))

    if conn.execute("SELECT to_regclass('public.chase_communication_senders')").fetchone()[0]:
        conn.execute(sql.SQL(
            "GRANT SELECT, INSERT ON chase_communication_senders, "
            "chase_communication_bindings, chase_communication_operations, "
            "chase_communication_events, chase_communication_suppressions TO {}"
        ).format(identifier))
        conn.execute(sql.SQL(
            "GRANT USAGE, SELECT ON SEQUENCE chase_communication_events_sequence_no_seq TO {}"
        ).format(identifier))
    if conn.execute("SELECT to_regclass('public.billing_headers')").fetchone()[0]:
        conn.execute(sql.SQL(
            "GRANT SELECT, INSERT ON billing_headers, billing_header_revisions, "
            "billing_header_operations TO {}"
        ).format(identifier))
        conn.execute(sql.SQL(
            "GRANT EXECUTE ON FUNCTION cosmaneura_billing_header_editor_allowed(uuid,uuid,text) TO {}"
        ).format(identifier))

    if conn.execute("SELECT to_regclass('public.billing_header_signoffs')").fetchone()[0]:
        conn.execute(sql.SQL("GRANT SELECT, INSERT ON billing_header_signoffs TO {}").format(identifier))

    if conn.execute("SELECT to_regclass('public.chase_communication_policy_decisions')").fetchone()[0]:
        conn.execute(sql.SQL(
            "GRANT SELECT, INSERT ON chase_communication_policy_decisions, "
            "chase_communication_consent_decisions, chase_communication_authorizations TO {}"
        ).format(identifier))
        conn.execute(sql.SQL(
            "GRANT SELECT ON chase_communication_dispatch_starts, "
            "chase_communication_dispatch_receipts, chase_communication_provider_events, "
            "chase_communication_dispatch_checks TO {}"
        ).format(identifier))
        conn.execute(sql.SQL(
            "GRANT USAGE, SELECT ON SEQUENCE chase_communication_policy_decisions_sequence_no_seq, "
            "chase_communication_consent_decisions_sequence_no_seq TO {}"
        ).format(identifier))


    if conn.execute("SELECT to_regclass('public.chase_communication_cancellations')").fetchone()[0]:
        conn.execute(sql.SQL("GRANT SELECT, INSERT ON chase_communication_cancellations TO {}").format(identifier))
    if conn.execute("SELECT to_regclass('public.chase_communication_unknown_resolutions')").fetchone()[0]:
        conn.execute(sql.SQL("GRANT SELECT, INSERT ON chase_communication_unknown_resolutions TO {}").format(identifier))
    if conn.execute("SELECT to_regclass('public.chase_communication_event_reviews')").fetchone()[0]:
        conn.execute(sql.SQL("GRANT SELECT, INSERT ON chase_communication_event_reviews TO {}").format(identifier))
        conn.execute(sql.SQL("GRANT SELECT ON chase_communication_review_sources TO {}").format(identifier))
    if conn.execute("SELECT to_regclass('public.chase_communication_unassigned_sources')").fetchone()[0]:
        conn.execute(sql.SQL("GRANT SELECT ON chase_communication_unassigned_sources TO {}").format(identifier))

    if conn.execute("SELECT to_regclass('public.provider_handoff_heads')").fetchone()[0]:
        conn.execute(sql.SQL("GRANT SELECT ON provider_handoff_heads, provider_handoff_operations TO {}").format(identifier))
        conn.execute(sql.SQL("GRANT EXECUTE ON FUNCTION cosmaneura_commit_provider_handoff(jsonb) TO {}").format(identifier))
    if conn.execute("SELECT to_regclass('public.provider_source_connections')").fetchone()[0]:
        conn.execute(sql.SQL(
            "GRANT SELECT ON provider_source_connections, provider_source_admissions, "
            "provider_source_bindings, provider_source_acceptances TO {}"
        ).format(identifier))

    if conn.execute("SELECT to_regprocedure('public.cosmaneura_provider_model_source(uuid,uuid,text,text)')").fetchone()[0]:
        conn.execute(sql.SQL(
            "GRANT EXECUTE ON FUNCTION public.cosmaneura_provider_model_source(uuid,uuid,text,text) TO {}"
        ).format(identifier))
    if conn.execute("SELECT to_regclass('public.athena_case_review_events')").fetchone()[0]:
        conn.execute(sql.SQL("GRANT SELECT, INSERT ON athena_case_review_events TO {}").format(identifier))
        conn.execute(sql.SQL(
            "GRANT EXECUTE ON FUNCTION cosmaneura_athena_reviewer_allowed(uuid,uuid,text) TO {}"
        ).format(identifier))
    if conn.execute("SELECT to_regclass('public.athena_staff_connections')").fetchone()[0]:
        conn.execute(sql.SQL(
            "GRANT EXECUTE ON FUNCTION cosmaneura_athena_staff_scope(uuid,uuid,text,text) TO {}"
        ).format(identifier))
    if conn.execute("SELECT to_regprocedure('public.cosmaneura_athena_read_admission(uuid,uuid,text,text,text,text)')").fetchone()[0]:
        conn.execute(sql.SQL(
            "GRANT EXECUTE ON FUNCTION public.cosmaneura_athena_read_admission(uuid,uuid,text,text,text,text) TO {}"
        ).format(identifier))


def _provision_chase_worker_role(conn, settings: Settings) -> None:
    """Create the practice-bound scheduler login without API/identity authority."""
    if not settings.chase_worker_database_password:
        return
    sql, identifier = _prepare_bound_login(
        conn,
        role=settings.chase_worker_database_role,
        password_value=settings.chase_worker_database_password,
        practice_id=settings.practice_id,
        description="chase worker role",
    )
    # The worker can inspect chase-domain inputs and immutable authorization
    # evidence, but it cannot create or change them. It cannot read encounter,
    # identity, feedback, or audit tables.
    conn.execute(sql.SQL(
        "GRANT SELECT ON chase_items, chase_pending_tasks, "
        "chase_contact_snapshots, chase_contact_routes, chase_policy_revisions, "
        "chase_policy_steps, "
        "chase_policy_approval_events, chase_message_releases, "
        "chase_review_releases, chase_active_policies, chase_cadence_states, "
        "chase_delivery_outbox, chase_delivery_events TO {}"
    ).format(identifier))
    # Only scheduling effects are writable. Human detection/contact, policy,
    # approval, review-release, and message-release inputs remain API work.
    conn.execute(sql.SQL(
        "GRANT INSERT ON chase_pending_tasks, chase_cadence_states, "
        "chase_delivery_outbox, chase_delivery_events TO {}"
    ).format(identifier))
    conn.execute(sql.SQL(
        "GRANT UPDATE (state, closed_at, review_reason, version, updated_at) "
        "ON chase_items TO {}"
    ).format(identifier))
    conn.execute(sql.SQL(
        "GRANT UPDATE ON chase_cadence_states, chase_delivery_outbox TO {}"
    ).format(identifier))
    conn.execute(sql.SQL(
        "GRANT DELETE ON chase_cadence_states TO {}"
    ).format(identifier))
    conn.execute(sql.SQL(
        "GRANT EXECUTE ON FUNCTION cosmaneura_practice_allowed(text), "
        "cosmaneura_record_accepted_delivery_attempt(uuid) TO {}"
    ).format(identifier))

    if conn.execute("SELECT to_regclass('public.chase_communication_policy_decisions')").fetchone()[0]:
        conn.execute(sql.SQL(
            "GRANT SELECT ON chase_communication_senders, chase_communication_bindings, "
            "chase_communication_operations, chase_communication_events, chase_communication_suppressions, "
            "chase_communication_policy_decisions, chase_communication_consent_decisions, "
            "chase_communication_authorizations, chase_communication_dispatch_starts, "
            "chase_communication_dispatch_receipts, chase_communication_provider_events, "
            "chase_communication_dispatch_checks, chase_attempts TO {}"
        ).format(identifier))
        conn.execute(sql.SQL(
            "GRANT INSERT ON chase_communication_dispatch_starts, chase_communication_dispatch_receipts, "
            "chase_communication_provider_events, chase_communication_dispatch_checks TO {}"
        ).format(identifier))
        conn.execute(sql.SQL(
            "GRANT USAGE, SELECT ON SEQUENCE chase_communication_provider_events_sequence_no_seq, "
            "chase_communication_dispatch_checks_sequence_no_seq TO {}"
        ).format(identifier))
        conn.execute(sql.SQL(
            "GRANT EXECUTE ON FUNCTION cosmaneura_staff_assignee_allowed(uuid,uuid), "
            "cosmaneura_chase_admin_allowed(uuid,uuid) TO {}"
        ).format(identifier))


    if conn.execute("SELECT to_regclass('public.chase_communication_cancellations')").fetchone()[0]:
        conn.execute(sql.SQL("GRANT SELECT ON chase_communication_cancellations TO {}").format(identifier))
    if conn.execute("SELECT to_regclass('public.chase_communication_unknown_resolutions')").fetchone()[0]:
        conn.execute(sql.SQL("GRANT SELECT ON chase_communication_unknown_resolutions TO {}").format(identifier))
    if conn.execute("SELECT to_regclass('public.chase_communication_event_reviews')").fetchone()[0]:
        conn.execute(sql.SQL("GRANT SELECT ON chase_communication_event_reviews, chase_communication_review_sources TO {}").format(identifier))


def _provision_athena_import_role(conn, settings: Settings) -> None:
    """Create the practice/source-bound Athena importer after migration 009."""
    if not settings.athena_import_database_password:
        return
    sql, identifier = _prepare_bound_login(
        conn,
        role=settings.athena_import_database_role,
        password_value=settings.athena_import_database_password,
        practice_id=settings.practice_id,
        description="Athena import role",
    )
    source_system = "athena_preview"
    provenance = "athena_preview_synthetic"
    existing_role_binding = conn.execute(
        """SELECT practice_id, source_system, provenance
             FROM public.source_import_role_bindings
            WHERE role_name = %s""",
        (settings.athena_import_database_role,),
    ).fetchone()
    if existing_role_binding and tuple(existing_role_binding) != (
        settings.practice_id,
        source_system,
        provenance,
    ):
        raise RuntimeError(
            "Athena import role is already bound to another source or practice"
        )
    existing_source_binding = conn.execute(
        """SELECT role_name, provenance
             FROM public.source_import_role_bindings
            WHERE practice_id = %s AND source_system = %s""",
        (settings.practice_id, source_system),
    ).fetchone()
    if existing_source_binding and tuple(existing_source_binding) != (
        settings.athena_import_database_role,
        provenance,
    ):
        raise RuntimeError(
            "Athena Preview source is already bound to another import role"
        )
    existing_provenance_binding = conn.execute(
        """SELECT role_name, source_system
             FROM public.source_import_role_bindings
            WHERE practice_id = %s AND provenance = %s""",
        (settings.practice_id, provenance),
    ).fetchone()
    if existing_provenance_binding and tuple(existing_provenance_binding) != (
        settings.athena_import_database_role,
        source_system,
    ):
        raise RuntimeError(
            "Athena Preview provenance is already bound to another import role"
        )
    conn.execute(
        """INSERT INTO public.source_import_role_bindings (
               role_name, practice_id, source_system, provenance
           ) VALUES (%s, %s, %s, %s)
           ON CONFLICT (role_name) DO NOTHING""",
        (
            settings.athena_import_database_role,
            settings.practice_id,
            source_system,
            provenance,
        ),
    )

    # The importer can read only its bounded continuation state. All encounter,
    # receipt, and checkpoint writes go through one database-owned atomic page
    # primitive; direct table DML would permit a receipt/checkpoint bypass.
    conn.execute(sql.SQL(
        "GRANT SELECT ON public.source_import_checkpoints TO {}"
    ).format(identifier))
    conn.execute(sql.SQL(
        "GRANT EXECUTE ON FUNCTION public.cosmaneura_practice_allowed(text), "
        "public.cosmaneura_source_import_owner(), "
        "public.cosmaneura_source_import_session(), "
        "public.cosmaneura_source_import_allowed(text, text, text), "
        "public.cosmaneura_commit_source_encounter_page("
        "text, text, text, text, text, bigint, jsonb, text, text) TO {}"
    ).format(identifier))


def _bootstrap_practice_admin(conn, settings: Settings) -> None:
    """Provision only an explicitly configured immutable Entra administrator."""
    if not settings.bootstrap_admin_oid:
        return
    if not settings.entra_tenant_id or not settings.practice_id:
        raise RuntimeError(
            "bootstrap administrator requires tenant and practice configuration"
        )
    # 003 forces RLS even for the table owner. The locked migration connection
    # sets the one explicitly configured practice for these bootstrap writes;
    # it is closed immediately after the migration run.
    conn.execute(
        "SELECT set_config('cosmaneura.practice_id', %s, false)",
        (settings.practice_id,),
    )
    conn.execute(
        """INSERT INTO practices (
               practice_id, entra_tenant_id, display_name
           ) VALUES (%s, %s::uuid, %s)
           ON CONFLICT (practice_id) DO UPDATE SET
               entra_tenant_id = EXCLUDED.entra_tenant_id,
               display_name = EXCLUDED.display_name,
               updated_at = now()
             WHERE practices.entra_tenant_id IS NULL""",
        (settings.practice_id, settings.entra_tenant_id, settings.practice_name),
    )
    practice = conn.execute(
        "SELECT entra_tenant_id FROM practices WHERE practice_id = %s",
        (settings.practice_id,),
    ).fetchone()
    if practice and str(practice[0]) != settings.entra_tenant_id:
        raise RuntimeError("configured practice is bound to another tenant")
    conn.execute(
        """INSERT INTO users (tenant_id, user_id)
           VALUES (%s::uuid, %s::uuid)
           ON CONFLICT (tenant_id, user_id) DO NOTHING""",
        (settings.entra_tenant_id, settings.bootstrap_admin_oid),
    )
    conn.execute(
        """INSERT INTO practice_memberships (
               practice_id, tenant_id, user_id, roles
           ) VALUES (%s, %s::uuid, %s::uuid, ARRAY['Admin']::text[])
           ON CONFLICT (practice_id, tenant_id, user_id) DO NOTHING""",
        (
            settings.practice_id, settings.entra_tenant_id,
            settings.bootstrap_admin_oid,
        ),
    )


def migration_directory(settings: Settings) -> Path:
    if settings.migrations_dir:
        return Path(settings.migrations_dir)
    return Path(__file__).resolve().parents[1] / "migrations"


def discover_migrations(directory: Path) -> list[Path]:
    if not directory.is_dir():
        raise RuntimeError(f"migration directory is unavailable: {directory}")
    return sorted(
        path for path in directory.iterdir()
        if path.is_file() and _UP_MIGRATION.fullmatch(path.name)
    )


def migration_body(sql: str) -> str:
    """Remove one conventional outer transaction for runner-owned atomicity."""
    lines = sql.splitlines()
    significant = [
        index for index, line in enumerate(lines)
        if line.strip() and not line.lstrip().startswith("--")
    ]
    controls = [
        index for index in significant
        if lines[index].strip().upper() in {"BEGIN;", "COMMIT;"}
    ]
    if not controls:
        return sql
    if (
        len(controls) != 2
        or controls[0] != significant[0]
        or controls[1] != significant[-1]
        or lines[controls[0]].strip().upper() != "BEGIN;"
        or lines[controls[1]].strip().upper() != "COMMIT;"
    ):
        raise RuntimeError(
            "migration transaction control must be one outer BEGIN/COMMIT pair"
        )
    return "\n".join(
        line for index, line in enumerate(lines) if index not in controls
    )


def apply_migrations(
    settings: Settings | None = None,
    *,
    connect: Callable[..., object] | None = None,
) -> list[str]:
    """Apply every pending up migration and return the names applied now."""
    config = settings or Settings.from_env()
    if not config.database_url:
        raise RuntimeError("DATABASE_URL is required to apply migrations")
    if connect is None:
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover - image wiring check
            raise RuntimeError(
                "database migrations require the 'postgres' package extra"
            ) from exc
        connect = psycopg.connect

    paths = discover_migrations(migration_directory(config))
    if not paths:
        raise RuntimeError("no up migrations were found")

    applied_now: list[str] = []
    with connect(config.database_url, autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (_MIGRATION_LOCK,))
        try:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS cosmaneura_schema_migrations (
                       name text PRIMARY KEY,
                       checksum_sha256 text,
                       applied_at timestamptz NOT NULL DEFAULT now()
                   )"""
            )
            conn.execute(
                "ALTER TABLE cosmaneura_schema_migrations "
                "ADD COLUMN IF NOT EXISTS checksum_sha256 text"
            )
            applied = {
                row[0]: row[1] for row in conn.execute(
                    "SELECT name, checksum_sha256 FROM cosmaneura_schema_migrations"
                ).fetchall()
            }
            for path in paths:
                checksum = _checksum(path)
                if path.name in applied:
                    recorded = applied[path.name]
                    if recorded and recorded != checksum:
                        raise RuntimeError(
                            f"applied migration checksum changed: {path.name}"
                        )
                    if not recorded:
                        conn.execute(
                            """UPDATE cosmaneura_schema_migrations
                                  SET checksum_sha256 = %s WHERE name = %s""",
                            (checksum, path.name),
                        )
                    continue
                # The schema change and its ledger row commit together. Checked-in
                # SQL keeps an outer transaction for safe manual psql use; the
                # runner removes only that wrapper and owns the transaction here.
                with conn.transaction():
                    conn.execute(
                        migration_body(path.read_text(encoding="utf-8")),
                        prepare=False,
                    )
                    conn.execute(
                        """INSERT INTO cosmaneura_schema_migrations
                               (name, checksum_sha256) VALUES (%s, %s)""",
                        (path.name, checksum),
                    )
                applied_now.append(path.name)
            with conn.transaction():
                _bootstrap_practice_admin(conn, config)
                _provision_runtime_role(conn, config)
                _provision_chase_worker_role(conn, config)
                _provision_athena_import_role(conn, config)
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (_MIGRATION_LOCK,))
    return applied_now


def main() -> int:
    for name in apply_migrations():
        print(f"applied {name}")
    return 0


if __name__ == "__main__":  # pragma: no cover - deployment entrypoint
    raise SystemExit(main())
