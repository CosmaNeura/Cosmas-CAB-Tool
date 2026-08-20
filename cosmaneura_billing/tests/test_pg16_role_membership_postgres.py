"""Live PostgreSQL 16 regression for idempotent runtime-role provisioning."""
from __future__ import annotations

import os
from uuid import uuid4

import pytest

psycopg = pytest.importorskip("psycopg")
from psycopg import sql
from psycopg.conninfo import make_conninfo

from cosmaneura_billing.migrations import _prepare_bound_login


TEST_DSN = os.environ.get("COSMANEURA_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not TEST_DSN, reason="COSMANEURA_TEST_DATABASE_URL is not configured",
)


def test_pg16_creator_admin_edge_allows_repeat_provisioning():
    suffix = uuid4().hex[:16]
    creator = f"pg16_creator_{suffix}"
    runtime = f"pg16_runtime_{suffix}"
    database = f"cosmaneura_test_rolefix_{suffix}"
    creator_password = f"Creator-{uuid4().hex}"
    runtime_password = f"Runtime-{uuid4().hex}"

    with psycopg.connect(TEST_DSN, autocommit=True) as admin:
        if admin.info.server_version < 160000:
            pytest.skip("PostgreSQL 16 membership-option semantics are required")
        admin.execute(
            sql.SQL(
                "CREATE ROLE {} LOGIN PASSWORD {} CREATEROLE "
                "NOSUPERUSER NOCREATEDB NOINHERIT NOBYPASSRLS"
            ).format(sql.Identifier(creator), sql.Literal(creator_password))
        )
        admin.execute(
            sql.SQL("CREATE DATABASE {} OWNER {}").format(
                sql.Identifier(database), sql.Identifier(creator)
            )
        )

    creator_dsn = make_conninfo(
        TEST_DSN,
        dbname=database,
        user=creator,
        password=creator_password,
    )
    try:
        with psycopg.connect(creator_dsn, autocommit=True) as conn:
            conn.execute(
                """CREATE TABLE runtime_practice_bindings (
                       role_name text PRIMARY KEY,
                       practice_id text NOT NULL
                   )"""
            )
            for _ in range(2):
                _prepare_bound_login(
                    conn,
                    role=runtime,
                    password_value=runtime_password,
                    practice_id="synthetic-pg16",
                    description="runtime role",
                )

            runtime_oid = conn.execute(
                "SELECT oid FROM pg_roles WHERE rolname = %s", (runtime,)
            ).fetchone()[0]
            creator_oid = conn.execute(
                "SELECT oid FROM pg_roles WHERE rolname = %s", (creator,)
            ).fetchone()[0]
            creator_edges = conn.execute(
                """SELECT member = %s, admin_option, inherit_option, set_option
                     FROM pg_auth_members
                    WHERE roleid = %s""",
                (creator_oid, runtime_oid),
            ).fetchall()
            assert creator_edges == [(True, True, False, False)]
            assert conn.execute(
                "SELECT count(*) FROM pg_auth_members WHERE member = %s",
                (runtime_oid,),
            ).fetchone()[0] == 0
            assert conn.execute(
                """SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole,
                          rolinherit, rolreplication, rolbypassrls
                     FROM pg_roles WHERE oid = %s""",
                (runtime_oid,),
            ).fetchone() == (True, False, False, False, False, False, False)
    finally:
        with psycopg.connect(TEST_DSN, autocommit=True) as admin:
            admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (database,),
            )
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {}").format(
                    sql.Identifier(database)
                )
            )
            admin.execute(
                sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(runtime))
            )
            admin.execute(
                sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(creator))
            )
