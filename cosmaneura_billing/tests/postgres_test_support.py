"""Owned test databases for historical migration round-trips."""
from contextlib import contextmanager
from uuid import uuid4

@contextmanager
def isolated_database(owner_dsn, prefix):
    """Create and remove only a new unique database on the configured test server."""
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    with psycopg.connect(owner_dsn, autocommit=True) as owner:
        if "test" not in owner.info.dbname.casefold():
            raise ValueError("migration rehearsal requires a disposable test database")
        # No existing database, schema, role, guard or row is removed to reset state.
        name = "cab_" + prefix + "_test_" + uuid4().hex
        owner.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        try:
            yield make_conninfo(owner_dsn, dbname=name)
        finally:
            owner.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))
