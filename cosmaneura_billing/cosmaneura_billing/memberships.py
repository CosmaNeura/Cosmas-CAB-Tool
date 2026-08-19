"""Owner-only CLI for explicit Entra practice membership provisioning."""
from __future__ import annotations

import argparse
from uuid import UUID

from .settings import Settings

_ROLES = frozenset({"Admin", "BillingStaff", "Coder", "Provider"})


def provision_membership(
    settings: Settings,
    *,
    user_id: str,
    roles: tuple[str, ...],
    active: bool = True,
) -> None:
    """Upsert one reviewed immutable OID using the migration-owner DSN."""
    tenant_id = str(UUID(settings.entra_tenant_id))
    object_id = str(UUID(user_id))
    normalized_roles = tuple(dict.fromkeys(roles))
    if not normalized_roles or not set(normalized_roles).issubset(_ROLES):
        raise ValueError("roles must be one or more configured application roles")
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - deployment wiring
        raise RuntimeError("membership provisioning requires psycopg") from exc
    with psycopg.connect(settings.database_url) as conn:
        conn.execute(
            "SELECT set_config('cosmaneura.practice_id', %s, true)",
            (settings.practice_id,),
        )
        practice = conn.execute(
            """SELECT 1 FROM practices
                WHERE practice_id = %s AND entra_tenant_id = %s::uuid""",
            (settings.practice_id, tenant_id),
        ).fetchone()
        if practice is None:
            raise ValueError("configured practice and tenant are not provisioned")
        conn.execute(
            """INSERT INTO users (tenant_id, user_id, active)
               VALUES (%s::uuid, %s::uuid, true)
               ON CONFLICT (tenant_id, user_id) DO UPDATE SET
                   last_seen_at = now()""",
            (tenant_id, object_id),
        )
        conn.execute(
            """INSERT INTO practice_memberships (
                   practice_id, tenant_id, user_id, roles, active
               ) VALUES (%s, %s::uuid, %s::uuid, %s, %s)
               ON CONFLICT (practice_id, tenant_id, user_id) DO UPDATE SET
                   roles = EXCLUDED.roles, active = EXCLUDED.active,
                   updated_at = now()""",
            (
                settings.practice_id, tenant_id, object_id,
                list(normalized_roles), active,
            ),
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Provision or disable one reviewed Entra practice member",
    )
    parser.add_argument("user_id", help="immutable Microsoft Entra object ID")
    parser.add_argument("--role", action="append", dest="roles", default=[])
    parser.add_argument("--disable", action="store_true")
    args = parser.parse_args()
    provision_membership(
        Settings.from_env(), user_id=args.user_id,
        roles=tuple(args.roles), active=not args.disable,
    )
    print("membership updated")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    raise SystemExit(main())
